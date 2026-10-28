"""TTS 语音生成（支持 StepFun / MiniMax / 火山引擎 volc）"""
import os
import json
import math
import re
import time
import uuid
import base64
import binascii
import hashlib
import subprocess
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote

import config
import aligner
from openai import OpenAI

# 响度归一参数：真峰值上限（dBTP）防止归一后削波；LRA 为响度范围目标（语音常用值）
LOUDNORM_TP = -1.5
LOUDNORM_LRA = 11

# 单段生成失败后的自动重试退避（秒）：首次失败等 2s 重试、第二次失败等 8s 再试，
# 共尝试 3 次，仍失败才标记该段跳过（无配音）。测试可 monkeypatch 本常量加速。
TTS_RETRY_DELAYS: tuple[float, ...] = (2.0, 8.0)


def _is_ready_file(path: str) -> bool:
    """断点续跑判断：文件已存在且非空，视为上次运行已完成、可直接复用。"""
    try:
        return bool(path) and os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def _audio_text_snapshot_path(audio_path: str) -> str:
    """音频的「按哪段文本合成」快照边车文件（segment_XXX.mp3.sha1）"""
    return audio_path + ".sha1"


def _audio_matches_text(audio_path: str, text: str) -> bool:
    """断点续跑复用判据（文本一致性）：音频存在且边车哈希与本次文本一致。

    项目目录按「日期_主题」命名，同主题同一天重跑会落在同一目录，而 LLM
    每次生成的文稿措辞不同——旧音频与新文本错配时，字幕按新文本的真实
    时间片切块、配音却是旧文稿，声画各说各话（质量循环实测复现）。
    合法续跑（中断重跑）文本不变、哈希一致，照常复用不浪费已花的调用；
    本修复之前生成的音频没有快照，保守起见不复用、重新合成一次。
    """
    if not _is_ready_file(audio_path):
        return False
    try:
        with open(_audio_text_snapshot_path(audio_path), "r", encoding="utf-8") as f:
            saved = f.read().strip()
    except OSError:
        return False
    return saved == hashlib.sha1((text or "").encode("utf-8")).hexdigest()


def _write_audio_text_snapshot(audio_path: str, text: str) -> None:
    """生成成功后写文本快照，供续跑判据比对；写失败只损失续跑判据，不阻塞出片"""
    try:
        with open(_audio_text_snapshot_path(audio_path), "w", encoding="utf-8") as f:
            f.write(hashlib.sha1((text or "").encode("utf-8")).hexdigest())
    except OSError as e:
        print(f"[TTS] 音频文本快照写入失败（只影响续跑判据，不影响成片）: {e}")


def _get_ffmpeg() -> str:
    """获取 ffmpeg 可执行文件路径（imageio-ffmpeg 自带，无需系统安装）"""
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _extract_loudnorm_stats(stderr_text: str) -> dict | None:
    """从 ffmpeg stderr 解析 loudnorm print_format=json 的测量结果。

    返回包含 input_i / input_tp / input_lra / input_thresh / target_offset
    的字典；静音段或解析失败返回 None。
    """
    # loudnorm 的 JSON 无嵌套大括号；取最后一个含 input_i 的块（防多次打印）
    for block in reversed(re.findall(r"\{[^{}]*\}", stderr_text, re.DOTALL)):
        if "input_i" not in block:
            continue
        try:
            stats = json.loads(block)
            # 全部字段必须是有限数值（纯静音时可能是 -inf / nan，直接放弃归一）
            values = {key: float(stats[key]) for key in
                      ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")}
            if all(math.isfinite(v) for v in values.values()):
                return stats
        except (ValueError, KeyError, TypeError):
            continue
    return None


def _input_sample_rate(stderr_text: str) -> int:
    """从 ffmpeg -i 的 stderr 里解析输入采样率，失败返回 0"""
    m = re.search(r"Audio:.*?, (\d{4,6}) Hz", stderr_text)
    return int(m.group(1)) if m else 0


def normalize_loudness(path: str, target_lufs: float | None = None) -> str:
    """把单段 mp3 归一到目标响度（ffmpeg loudnorm 两遍法，线性模式不改变语音动态）。

    - loudnorm 输出采样率固定 192kHz，需用 -ar 恢复原采样率；
    - 任何一步失败都保留原文件并返回原路径，绝不阻塞生成流程。
    """
    if not path or not os.path.exists(path):
        return path
    if not getattr(config, "LOUDNESS_NORM", True):
        return path
    if target_lufs is None:
        target_lufs = config.TARGET_LUFS

    tmp_out = f"{path}.loudnorm.mp3"
    try:
        ffmpeg = _get_ffmpeg()

        # 第一遍：测量本段响度（输出到 null，不写文件）
        probe = subprocess.run(
            [ffmpeg, "-hide_banner", "-nostats", "-i", path,
             "-af", f"loudnorm=I={target_lufs}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}:print_format=json",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=300,
        )
        stats = _extract_loudnorm_stats(probe.stderr)
        if stats is None:
            raise RuntimeError("loudnorm 测量结果解析失败（可能为静音或损坏音频）")
        sample_rate = _input_sample_rate(probe.stderr) or 44100  # loudnorm 输出固定 192k，须还原

        # 第二遍：按测量值线性增益（linear=true 不压缩动态，听感自然）
        af = (
            f"loudnorm=I={target_lufs}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}"
            f":measured_I={stats['input_i']}:measured_TP={stats['input_tp']}"
            f":measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}"
            f":offset={stats['target_offset']}:linear=true"
        )
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-y", "-i", path, "-af", af,
             "-ar", str(sample_rate), "-b:a", "128k", tmp_out],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            raise RuntimeError(f"loudnorm 第二遍失败: {result.stderr[-300:]}")
        if not os.path.exists(tmp_out) or os.path.getsize(tmp_out) == 0:
            raise RuntimeError("loudnorm 输出文件为空")

        os.replace(tmp_out, path)  # 原子替换，归一成功才覆盖
        return path
    except Exception as e:
        print(f"[TTS] 响度归一失败，保留原文件 {path}: {e}")
        try:
            if os.path.exists(tmp_out):
                os.remove(tmp_out)
        except OSError:
            pass
        return path


def _get_stepfun_client():
    return OpenAI(api_key=config.STEPFUN_API_KEY, base_url=config.STEPFUN_BASE_URL)


def generate_speech_stepfun(text: str, output_path: str, voice: str = "") -> str:
    """StepFun TTS 生成单段语音"""
    import json

    if len(text) > 1000:
        text = text[:990] + "..."

    extra_body = {
        "volume": config.TTS_VOLUME,
        "sample_rate": config.TTS_SAMPLE_RATE,
    }

    if config.TTS_VOICE_LABEL:
        try:
            extra_body["voice_label"] = json.loads(config.TTS_VOICE_LABEL)
        except json.JSONDecodeError:
            print(f"[TTS] voice_label JSON 解析失败，忽略: {config.TTS_VOICE_LABEL}")

    if config.TTS_INSTRUCTION:
        extra_body["instruction"] = config.TTS_INSTRUCTION

    response = _get_stepfun_client().audio.speech.create(
        model=config.TTS_MODEL,
        voice=voice or config.TTS_VOICE,
        input=text,
        speed=config.TTS_SPEED,
        response_format="mp3",
        extra_body=extra_body,
    )

    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
    # openai>=1.26 提供 write_to_file；旧版本回退到 stream_to_file
    write = getattr(response, "write_to_file", None) or response.stream_to_file
    write(output_path)
    return output_path


def _minimax_payload(text: str, voice: str, with_subtitles: bool = False) -> dict:
    """组装 MiniMax t2a_v2 请求体（音频主路径与句级时间戳通道共用）。

    with_subtitles=True 时附带官方字幕参数（文档：
    https://platform.minimax.io/docs/api-reference/speech-t2a-http）：
    subtitle_enable 开启后非流式响应在 data.subtitle_file 返回字幕 JSON
    下载链接；subtitle_type=sentence 取句级时间戳（任务 D2 口径，词级可换
    "word"）。适用模型：speech-2.8/2.6/02/01 系列 hd/turbo。
    """
    payload = {
        "model": config.MINIMAX_TTS_MODEL,
        "text": text,
        "stream": False,
        "voice_setting": {
            "voice_id": voice or config.MINIMAX_TTS_VOICE_ID,
            "speed": config.MINIMAX_TTS_SPEED,
            "vol": 1,
            "pitch": 0,
        },
        "audio_setting": {
            "sample_rate": 32000,
            "bitrate": 128000,
            "format": "mp3",
            "channel": 1,
        },
    }
    if with_subtitles:
        payload["subtitle_enable"] = True
        payload["subtitle_type"] = "sentence"
    return payload


def generate_speech_minimax(text: str, output_path: str, voice: str = "") -> str:
    """MiniMax TTS 生成单段语音"""
    if not config.MINIMAX_API_KEY:
        raise ValueError("MiniMax API Key 未配置")

    payload = _minimax_payload(text, voice, with_subtitles=False)

    resp = requests.post(
        f"{config.MINIMAX_BASE_URL}/v1/t2a_v2",
        headers={
            "Authorization": f"Bearer {config.MINIMAX_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60,
    )
    data = resp.json()
    base_resp = data.get("base_resp", {})
    if base_resp.get("status_code") != 0:
        raise RuntimeError(f"MiniMax TTS 错误: {base_resp.get('status_msg')}")

    audio_hex = data["data"]["audio"]
    audio_bytes = binascii.unhexlify(audio_hex)
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(audio_bytes)
    return output_path


def generate_speech_volc(text: str, output_path: str, voice: str = "") -> str:
    """火山引擎（豆包）TTS 生成单段语音。

    对接官方「HTTP 非流式合成接口」（POST /api/v1/tts，JSON in / JSON out）：
    https://www.volcengine.com/docs/6561/79820
    鉴权 header 为 Bearer 加分号的特殊格式：`Authorization: Bearer;<token>`；
    响应 JSON 的 data 字段为 base64 编码音频（注意不是 MiniMax 那种 hex）。
    注意：本实现按公开文档结构编写，尚未用真实 key 实测。
    """
    if not config.VOLC_TTS_APPID or not config.VOLC_TTS_TOKEN:
        raise ValueError(
            "火山引擎 TTS 未配置：需要 VOLC_TTS_APPID 和 VOLC_TTS_TOKEN"
            "（火山引擎控制台「语音技术-应用管理」获取）"
        )

    # Web 前端音色下拉是 StepFun 音色名（cixingnansheng 等，不含下划线），
    # web/app.py 会把它原样透传进本函数；volc 的 voice_type 体系完全不同
    # （如 BV001_streaming / zh_female_*_mars_bigtts，均含下划线），直接发送
    # 必报「音色无效/未授权」类错误（见 overnight_reports/A5_volc_tts.md「遗留」）。
    # 识别为明显非 volc 格式时告警并回退 VOLC_TTS_VOICE_TYPE，而不是让
    # 每次合成都失败；含下划线的音色视为 volc 格式原样透传（CLI 不传 voice，
    # 走 config 默认，不受影响）。
    if voice and "_" not in voice:
        print(f"[TTS] 警告: 音色 '{voice}' 不是火山引擎 voice_type 格式"
              f"（如 BV001_streaming），已回退 VOLC_TTS_VOICE_TYPE="
              f"'{config.VOLC_TTS_VOICE_TYPE}'（Web 音色下拉为 StepFun 音色，"
              f"切 TTS_PROVIDER=volc 时请在 .env 配置 VOLC_TTS_VOICE_TYPE）")
        voice = ""

    # 官方限制单次合成约 1024 字节（UTF-8），留余量截断，避免超限报错
    raw = text.encode("utf-8")
    if len(raw) > 1000:
        text = raw[:990].decode("utf-8", errors="ignore") + "..."

    payload = _volc_payload(text, voice, with_timestamp=False)

    resp = requests.post(
        config.VOLC_TTS_API_URL,
        headers={
            # 官方文档明确：Bearer 与 token 之间用分号分隔（不是常见的空格）
            "Authorization": f"Bearer; {config.VOLC_TTS_TOKEN}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60,
    )
    data = resp.json()
    # 3000 = 成功；其余为错误码（如 3001 参数错误、3005 并发超限、3006 音色未授权）
    if data.get("code") != 3000:
        raise RuntimeError(
            f"火山引擎 TTS 错误 code={data.get('code')}: {data.get('message')}"
        )

    audio_b64 = data.get("data")
    if not audio_b64:
        raise RuntimeError("火山引擎 TTS 返回成功但缺少音频 data 字段")
    try:
        audio_bytes = base64.b64decode(audio_b64)
    except (binascii.Error, ValueError) as e:
        raise RuntimeError(f"火山引擎 TTS 音频 base64 解码失败: {e}")

    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(audio_bytes)
    return output_path

def _volc_payload(text: str, voice: str, with_timestamp: bool = False) -> dict:
    """组装火山引擎 HTTP 非流式合成请求体（音频主路径与时间戳通道共用）。

    with_timestamp=True 时在 request 块附带官方 with_timestamp=1（新版时间戳
    参数，见官方「参数基本说明」/豆包语音文档，开启后响应 addition 字段
    携带原文本时间戳）。参数结构未用真实 key 实测（同下方音频接入的注记）。
    """
    payload = {
        "app": {
            "appid": config.VOLC_TTS_APPID,
            "token": config.VOLC_TTS_TOKEN,
            "cluster": config.VOLC_TTS_CLUSTER,
        },
        "user": {"uid": "storyvid"},
        "audio": {
            "voice_type": voice or config.VOLC_TTS_VOICE_TYPE,
            "encoding": config.VOLC_TTS_ENCODING,
            "speed_ratio": config.VOLC_TTS_SPEED_RATIO,
            "volume_ratio": config.VOLC_TTS_VOLUME_RATIO,
        },
        "request": {
            "reqid": str(uuid.uuid4()),  # 官方要求每次合成唯一
            "text": text,
            "text_type": "plain",
            "operation": "query",  # 非流式：一次性返回完整音频
        },
    }
    if with_timestamp:
        payload["request"]["with_timestamp"] = 1
    return payload


# ---------------------------------------------------------------------
# Edge TTS（微软神经语音）：免费无 Key 的第四 provider（TTS_PROVIDER=edge）。
# 音频流与词级时间戳（WordBoundary）在同一次流式请求里一并拿到——这是它
# 相对其他 provider 的独特优势：时间戳不需要额外请求。词级时间片按 D2
# 通道契约 [{"word", "start", "end"}]（段内相对秒）暂存于 _EDGE_WORDS
# （以输出路径为键，段与段天然隔离），由 fetch_segment_timestamps("edge")
# 取走写入 seg["words"]（words_source="tts"）。
# ---------------------------------------------------------------------
_EDGE_WORDS: dict[str, list[dict]] = {}


def generate_speech_edge(text: str, output_path: str, voice: str = "") -> str:
    """Edge TTS 生成单段语音（免费无 Key），顺带收集词级时间戳

    依赖 edge-tts 包（仅装在 storyvid/.venv，未装时报错并给出安装指引）。
    任何失败都抛异常，由 generate_all_speech 的重试/跳过机制兜底。
    """
    import asyncio

    try:
        import edge_tts
    except ImportError as e:
        raise RuntimeError(
            "edge-tts 未安装：请在 storyvid 目录执行 "
            ".venv/bin/pip install edge-tts"
        ) from e

    voice = voice or config.EDGE_TTS_VOICE
    # TTS_SPEED 1.0 → "+0%"，1.15 → "+15%"（edge 的 rate 语法）
    rate_pct = int(round((config.TTS_SPEED - 1.0) * 100))
    rate = f"{rate_pct:+d}%"
    words: list[dict] = []

    async def _run() -> None:
        # boundary 默认是 SentenceBoundary，必须显式要词级
        communicate = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")
        with open(output_path, "wb") as f:
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    f.write(chunk["data"])
                elif chunk["type"] == "WordBoundary":
                    # offset/duration 单位是 100 纳秒（×1e7 = 秒）
                    start = (chunk.get("offset") or 0) / 1e7
                    duration = (chunk.get("duration") or 0) / 1e7
                    if chunk.get("text"):
                        words.append(
                            {"word": chunk["text"], "start": start, "end": start + duration}
                        )

    asyncio.run(_run())

    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise RuntimeError("Edge TTS 未返回音频数据")
    # dict 单键写入/弹出在 CPython 下原子（GIL），线程安全足够；
    # 键是输出路径，段与段天然不冲突
    _EDGE_WORDS[output_path] = words
    return output_path


def _pop_edge_words(audio_path: str) -> "list[dict] | None":
    """取走（并清除）edge 输出路径对应的词级时间片；无则 None"""
    return _EDGE_WORDS.pop(audio_path, None)


def generate_speech(text: str, output_path: str, provider: str = "", voice: str = "") -> str:
    """根据配置选择 TTS 提供商生成语音"""
    provider = provider or config.TTS_PROVIDER
    if provider == "minimax":
        return generate_speech_minimax(text, output_path, voice)
    if provider == "volc":
        return generate_speech_volc(text, output_path, voice)
    if provider == "edge":
        return generate_speech_edge(text, output_path, voice)
    return generate_speech_stepfun(text, output_path, voice)


# =====================================================================
# TTS 时间戳通道（任务 D2 / R1 §五 P0-2 / R1 §2.6）
#
# 目标：字幕不再按字符比例估时（subtitles.build_chunk_durations 的旧行为），
# 而是按 TTS 真实发音边界切块。三个 provider 各给一条可选通道：
#   ① stepfun：流式 WS 词级时间戳（tts_ws.py 标准库 WS 客户端，见下）；
#   ② minimax：t2a_v2 开字幕参数取句级时间戳（subtitle_file JSON）；
#   ③ volc：HTTP 非流式接口 with_timestamp 参数取字级（文档结构未实测，
#      宽松解析，真实 Key 验证待办）。
#
# 产出统一为通道契约列表（与 aligner.py 的 seg["words"] 同构）：
#       [{"word": str, "start": 段内相对秒, "end": 段内相对秒}, ...]
# 由 generate_all_speech 写入 seg["words"]（words_source="tts"），下游
# generate_srt / video._build_segment_clip 经 subtitles.build_chunk_durations
# 消费；拿不到的段由 D3 的本地对齐兜底（aligner.fill_missing_words）补齐。
#
# 设计取舍：三个通道都是「独立请求、丢弃音频」——时间戳参数属于未实测的
# 新请求结构，绝不挂在已验证的音频主请求上冒险；代价是开启 TTS_TIMESTAMPS
# 后每段多一次对应 provider 的调用（见 overnight_reports/D2_timestamps.md）。
# =====================================================================


def _to_float(value) -> "float | None":
    """宽容数值解析：数字/数值字符串 → float；其余（None/非有限数）→ None"""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


# 字幕/时间戳 JSON 的时间字段名候选（按优先级）：MiniMax T2ASubtitle schema
# 为 time_begin/time_end；StepFun subtitle 事件为 start_time/end_time；
# 常见变体 begin_time/end_time 也一并兼容（官方完整结构未实测，见 D2 报告）
_TIME_FIELD_PAIRS: tuple[tuple[str, str], ...] = (
    ("time_begin", "time_end"),
    ("begin_time", "end_time"),
    ("start_time", "end_time"),
)
# 时间片文本字段候选
_TEXT_FIELDS: tuple[str, ...] = ("text", "word", "char", "sentence")


def parse_timestamp_file_json(data) -> "list[dict] | None":
    """把字幕/时间戳 JSON 数据宽松解析为通道契约词/句列表（毫秒 → 秒）。

    为什么宽松：MiniMax subtitle_file 下载文件的完整结构与火山 addition 的
    结构官方文档均未给出（文档页 JS 渲染抓不到、本机无 Key 不能实测，见
    D2 报告遗留），解析按多形状兼容：
    - 顶层是「元素全为 dict 的列表」，或 dict 里嵌着这样的数组（最多向下
      探两层，任意键名）；
    - 文本取 _TEXT_FIELDS 中第一个非空字段；时间取 _TIME_FIELD_PAIRS 中
      第一对齐全的字段（毫秒，兼容数值字符串）；
    - 单条不合规则跳过；找不到任何合法时间片返回 None（调用方回退字符
      比例，绝不抛异常）。
    """
    for candidate in _find_item_lists(data):
        items = _items_from(candidate)
        if items:
            return items
    return None


def _find_item_lists(data, depth: int = 0) -> list:
    """收集数据里所有「元素全为 dict 的列表」（最多向下探两层）"""
    found: list = []
    if isinstance(data, list):
        if data and all(isinstance(x, dict) for x in data):
            found.append(data)
        return found
    if isinstance(data, dict) and depth < 2:
        for value in data.values():
            found.extend(_find_item_lists(value, depth + 1))
    return found


def _items_from(raw_items: list) -> "list[dict] | None":
    items: list[dict] = []
    for raw in raw_items:
        word = ""
        for key in _TEXT_FIELDS:
            v = raw.get(key)
            if isinstance(v, str) and v.strip():
                word = v.strip()
                break
        if not word:
            continue
        for begin_key, end_key in _TIME_FIELD_PAIRS:
            begin = _to_float(raw.get(begin_key))
            end = _to_float(raw.get(end_key))
            if begin is None or end is None or begin < 0 or end < begin:
                continue
            items.append({"word": word, "start": begin / 1000.0, "end": end / 1000.0})
            break
    return items or None


def _stepfun_ws_url() -> str:
    """StepFun 流式 WS 入口 + model 查询参数（官方：model 经 URL 查询参数传入）"""
    base = (getattr(config, "STEPFUN_WS_URL", "") or
            "wss://api.stepfun.com/v1/realtime/audio").strip()
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}model={quote(str(config.TTS_MODEL))}"


def _stepfun_subtitle_items(event: dict) -> list[dict]:
    """解析 tts.response.subtitle 事件 → 通道契约词列表（毫秒 → 秒）。

    官方结构（platform.stepfun.com/docs/zh/api-reference/audio/ws-audio）：
        {"type": "tts.response.subtitle",
         "data": {"items": [{"text": "今", "start_time": 0, "end_time": 160},
                            {"text": "天", "start_time": 160, "end_time": 320}]}}
    start_time/end_time 为毫秒，且已累加前序切句时长——所有 subtitle 事件的
    items 直接顺序拼接即得整段时间轴（官方保证 subtitle 在 audio.done 前
    下发完毕，异步乱序只影响到达顺序不影响拼接，这里按到达顺序收集）。
    """
    items: list[dict] = []
    data = event.get("data") if isinstance(event, dict) else None
    for raw in (data or {}).get("items") or []:
        if not isinstance(raw, dict):
            continue
        word = str(raw.get("text") or "").strip()
        start = _to_float(raw.get("start_time"))
        end = _to_float(raw.get("end_time"))
        if not word or start is None or end is None or start < 0 or end < start:
            continue
        items.append({"word": word, "start": start / 1000.0, "end": end / 1000.0})
    return items


def fetch_stepfun_word_timestamps(text: str, voice: str = "",
                                  overall_timeout: float = 60.0) -> "list[dict] | None":
    """StepFun 流式 WS 词级时间戳（D2 ①，按官方 WS 文档结构实现）。

    官方流程（platform.stepfun.com/docs/zh/api-reference/audio/ws-audio）：
      连接 wss://api.stepfun.com/v1/realtime/audio?model=<model>
      （Authorization: Bearer <key>）→ 等服务端 tts.connection.done 拿
      session_id → 发 tts.create（timestamp=true 开词级时间戳，配
      mode=sentence 按句切分为官方建议组合；speed/volume/voice_label/
      instruction 与 HTTP 音频路径对齐，保证两路合成参数一致）→ 等
      tts.response.created → 发 tts.text.delta（≤1000 字符，与 HTTP 路径
      同一截断规则）+ tts.text.done → 收集 tts.response.subtitle 词级
      时间戳，直到 tts.response.audio.done / tts.response.error / 连接关闭。

    本函数只取时间戳、丢弃音频（官方保证 subtitle 在 audio.done 前发完）；
    配音仍走原 HTTP 接口。WS 全链路音频合并（省一次调用）列为后续优化，
    避免在无法实测的机器上替换已验证的音频路径。

    Key 缺失 / 连接失败 / 任何一步失败都返回 None（优雅跳过，字幕回退
    字符比例），绝不抛异常阻塞出片。
    """
    if not config.STEPFUN_API_KEY:
        print("[TTS] StepFun 词级时间戳跳过：未配置 STEPFUN_API_KEY")
        return None
    if len(text) > 1000:
        text = text[:990] + "..."

    try:
        from tts_ws import WsClient  # 惰性导入：WS 客户端出问题不拖累 import tts
        ws = WsClient(
            _stepfun_ws_url(),
            headers={"Authorization": f"Bearer {config.STEPFUN_API_KEY}"},
            timeout=15.0,
        )
    except Exception as e:
        print(f"[TTS] StepFun 词级时间戳连接失败（跳过）: {e}")
        return None

    items: list[dict] = []
    try:
        deadline = time.monotonic() + max(overall_timeout, 5.0)

        def _next_event() -> dict:
            if time.monotonic() > deadline:
                raise RuntimeError("StepFun WS 总超时")
            raw = ws.recv_text()
            if raw is None:
                raise RuntimeError("StepFun WS 连接在完成前被对端关闭")
            try:
                event = json.loads(raw)
            except ValueError as e:
                raise RuntimeError(f"StepFun WS 事件不是合法 JSON: {e}") from None
            if not isinstance(event, dict):
                raise RuntimeError("StepFun WS 事件不是 JSON 对象")
            if event.get("type") == "tts.response.error":
                err = event.get("data") or {}
                raise RuntimeError(
                    f"StepFun WS 错误 {err.get('code')}: {err.get('message')}")
            return event

        def _session_id(event: dict) -> str:
            return str(((event.get("data") or {}).get("session_id")) or "")

        # 1) 建联事件（含 session_id）
        session_id = _session_id(_next_event())
        while not session_id:
            session_id = _session_id(_next_event())

        # 2) 创建会话：词级时间戳 + 按句切分；合成参数与 HTTP 音频路径对齐
        create_data: dict = {
            "session_id": session_id,
            "voice_id": voice or config.TTS_VOICE,
            "response_format": "mp3",
            "sample_rate": config.TTS_SAMPLE_RATE,
            "speed_ratio": config.TTS_SPEED,
            "volume_ratio": config.TTS_VOLUME,
            "mode": "sentence",
            "timestamp": True,
        }
        if config.TTS_VOICE_LABEL:
            try:
                create_data["voice_label"] = json.loads(config.TTS_VOICE_LABEL)
            except json.JSONDecodeError:
                pass
        if config.TTS_INSTRUCTION:
            create_data["instruction"] = config.TTS_INSTRUCTION
        ws.send_text(json.dumps({"type": "tts.create", "data": create_data},
                                ensure_ascii=False))

        # 3) 等会话创建确认（期间到来的其它事件只查错）
        while _next_event().get("type") != "tts.response.created":
            pass

        # 4) 送文本 + 结束标记
        ws.send_text(json.dumps(
            {"type": "tts.text.delta",
             "data": {"session_id": session_id, "text": text}},
            ensure_ascii=False))
        ws.send_text(json.dumps(
            {"type": "tts.text.done", "data": {"session_id": session_id}}))

        # 5) 收集词级时间戳直到完成
        while True:
            event = _next_event()
            etype = event.get("type")
            if etype == "tts.response.subtitle":
                items.extend(_stepfun_subtitle_items(event))
            elif etype == "tts.response.audio.done":
                break
        return items or None
    except Exception as e:
        print(f"[TTS] StepFun 词级时间戳获取失败（跳过，字幕回退字符比例）: {e}")
        return None
    finally:
        try:
            ws.close()
        except Exception:
            pass


def fetch_minimax_sentence_timestamps(text: str, voice: str = "") -> "list[dict] | None":
    """MiniMax t2a_v2 句级时间戳（D2 ②，payload 按官方文档结构，解析走 mock）。

    字幕文件必须由同一次带 subtitle_enable 的合成请求产出（响应
    data.subtitle_file 是字幕 JSON 的下载链接，毫秒级、句级单句 ≤50 字），
    因此这里独立发起一次带字幕参数的合成、丢弃音频——正式音频请求不加
    该参数（参数未实测，不能冒险挂在主路径上）。

    Key 缺失 / 接口报错 / 未返回 subtitle_file / 下载解析失败 → 返回 None
    （优雅跳过，字幕回退字符比例），绝不抛异常。
    """
    if not config.MINIMAX_API_KEY:
        print("[TTS] MiniMax 句级时间戳跳过：未配置 MINIMAX_API_KEY")
        return None
    try:
        resp = requests.post(
            f"{config.MINIMAX_BASE_URL}/v1/t2a_v2",
            headers={
                "Authorization": f"Bearer {config.MINIMAX_API_KEY}",
                "Content-Type": "application/json",
            },
            json=_minimax_payload(text, voice, with_subtitles=True),
            timeout=60,
        )
        data = resp.json()
        base_resp = data.get("base_resp", {})
        if base_resp.get("status_code") != 0:
            raise RuntimeError(f"MiniMax TTS 错误: {base_resp.get('status_msg')}")
        subtitle_url = (data.get("data") or {}).get("subtitle_file") or ""
        if not subtitle_url:
            print("[TTS] MiniMax 未返回 subtitle_file"
                  "（当前模型可能不支持字幕参数），跳过时间戳")
            return None
        sub = requests.get(subtitle_url, timeout=30)
        words = parse_timestamp_file_json(sub.json())
        return words or None
    except Exception as e:
        print(f"[TTS] MiniMax 句级时间戳获取失败（跳过，字幕回退字符比例）: {e}")
        return None


def fetch_volc_word_timestamps(text: str, voice: str = "") -> "list[dict] | None":
    """火山引擎字/词级时间戳（D2 ③，按搜索到的官方参数结构 best-effort）。

    官方「参数基本说明」列有 with_timestamp 参数（传 1 启用，返回原文本的
    时间戳，数据在响应 addition 字段）。与另两个 provider 同策略：独立发起
    一次带时间戳参数的合成、丢弃音频，正式音频请求不加该参数。
    注意：文档页为 JS 渲染抓不到完整响应结构，addition 解析走宽松共享解析
    （parse_timestamp_file_json，毫秒口径），真实 Key 验证标注待办。

    未配置凭证 / 接口报错 / 未解析出时间戳 → 返回 None（优雅跳过）。
    """
    if not config.VOLC_TTS_APPID or not config.VOLC_TTS_TOKEN:
        print("[TTS] 火山引擎时间戳跳过：未配置 VOLC_TTS_APPID / VOLC_TTS_TOKEN")
        return None
    raw = text.encode("utf-8")
    if len(raw) > 1000:
        text = raw[:990].decode("utf-8", errors="ignore") + "..."
    try:
        resp = requests.post(
            config.VOLC_TTS_API_URL,
            headers={
                "Authorization": f"Bearer; {config.VOLC_TTS_TOKEN}",
                "Content-Type": "application/json",
            },
            json=_volc_payload(text, voice, with_timestamp=True),
            timeout=60,
        )
        data = resp.json()
        if data.get("code") != 3000:
            raise RuntimeError(
                f"火山引擎 TTS 错误 code={data.get('code')}: {data.get('message')}")
        words = parse_timestamp_file_json(data.get("addition"))
        if not words:
            print("[TTS] 火山引擎响应中未解析出时间戳"
                  "（addition 结构待真机核对），跳过")
            return None
        return words
    except Exception as e:
        print(f"[TTS] 火山引擎时间戳获取失败（跳过，字幕回退字符比例）: {e}")
        return None


def fetch_segment_timestamps(text: str, provider: str = "",
                             voice: str = "", audio_path: str = "") -> "list[dict] | None":
    """TTS 时间戳通道入口（D2）：按 provider 获取真实时间片，供 seg["words"]。

    返回通道契约列表 [{"word": str, "start": 段内相对秒, "end": 段内相对秒}]；
    不支持 / 未配置 / 失败一律返回 None（调用方回退字符比例；该段交给 D3
    本地对齐兜底）。config.TTS_TIMESTAMPS=0/off/false/no 全局关闭。
    edge 例外：词级时间片随音频流免费产出（generate_speech_edge 暂存），
    用 audio_path 取走即可，不发额外请求。
    """
    if not getattr(config, "TTS_TIMESTAMPS", True):
        return None
    provider = (provider or config.TTS_PROVIDER or "").strip().lower()
    if provider == "stepfun":
        return fetch_stepfun_word_timestamps(text, voice)
    if provider == "minimax":
        return fetch_minimax_sentence_timestamps(text, voice)
    if provider == "volc":
        return fetch_volc_word_timestamps(text, voice)
    if provider == "edge":
        return _pop_edge_words(audio_path) if audio_path else None
    return None


def get_audio_duration(path: str) -> float:
    """获取音频文件时长（秒）。mutagen 快且准，失败再用 moviepy，仍失败按 5 秒兜底"""
    if not path or not os.path.exists(path):
        return 5.0  # 文件不存在（如 TTS 失败段），直接兜底，避免 moviepy 读不到文件报错

    try:
        from mutagen import File as MutagenFile
        info = MutagenFile(path)
        if info is not None and info.info and info.info.length and info.info.length > 0:
            return float(info.info.length)
    except Exception:
        pass

    try:
        from moviepy import AudioFileClip
        clip = AudioFileClip(path)
        duration = clip.duration
        clip.close()
        if duration and duration > 0:
            return float(duration)
    except Exception:
        pass

    return 5.0  # 默认 5 秒


def generate_all_speech(
    segments: list[dict],
    provider: str = "",
    voice: str = "",
    project_dir: str = "",
) -> list[dict]:
    """为所有段落生成语音，并记录时长（并行，最多 3 并发）。

    断点续跑：项目目录里已存在且非空的 segment_XXX.mp3 直接复用，
    不再调 TTS API、不再做响度归一（中断重跑不浪费已花钱的调用）；
    复用判据含文本一致性（边车快照 segment_XXX.mp3.sha1 与本次段落文本
    哈希比对）——同主题同日重跑时 LLM 会生成不同措辞的新文稿，旧音频
    与新文本错配（字幕按新文本切、配音是旧文稿）必须重新合成；
    复用段会打上 audio_reused=True 标记（main.py 据此输出复用提示）。
    单段失败自动重试 2 次（退避 2s/8s，见 TTS_RETRY_DELAYS），仍失败才
    标记跳过（该段无音频，合成时静音跳过），不中断整个流程。
    每段生成后做响度归一（LOUDNESS_NORM / TARGET_LUFS 控制），失败保留原文件。
    D2 时间戳通道（TTS_TIMESTAMPS 开，默认开）：新生成段按 provider 取真实
    词/句级时间片写入 seg["words"]（words_source="tts"，契约见
    fetch_segment_timestamps / aligner.py）；复用段与拿不到的段不带，
    由末尾 aligner.fill_missing_words 用本地引擎兜底补缺——字幕侧
    （generate_srt / video）据此按真实发音边界切块，拿不到自动回退字符比例。
    project_dir 为空时回退到全局项目目录（向后兼容 CLI 旧用法）；
    Web 多任务并发时应显式传入各自的目录，避免互相覆盖。
    """
    proj_dir = project_dir or config.get_project_dir()
    os.makedirs(proj_dir, exist_ok=True)
    total = len(segments)

    def _task(args):
        i, seg = args
        audio_path = os.path.join(proj_dir, f"segment_{i:03d}.mp3")
        if _audio_matches_text(audio_path, seg.get("text", "")):
            # 断点续跑：上次已生成成功的段（存在且非空、文本哈希一致）直接复用，
            # 省掉重复 API 调用；同日重跑文稿变了哈希不符 → 重新合成，不错配
            print(f"[TTS] 第 {i+1}/{total} 段已存在（{os.path.basename(audio_path)}），"
                  f"跳过生成直接复用")
            # 复用段没有本次合成的时间戳（旧音频与本次请求无对应关系），
            # 词级兜底交给末尾的 aligner.fill_missing_words（读缓存补齐）
            return i, audio_path, get_audio_duration(audio_path), None, True, None

        print(f"[TTS] 开始生成第 {i+1}/{total} 段语音...")
        last_error: Exception | None = None
        for attempt in range(len(TTS_RETRY_DELAYS) + 1):
            try:
                generate_speech(seg["text"], audio_path, provider=provider, voice=voice)
                last_error = None
                break
            except Exception as e:
                last_error = e
                if attempt < len(TTS_RETRY_DELAYS):
                    delay = TTS_RETRY_DELAYS[attempt]
                    print(f"[TTS] 第 {i+1} 段第 {attempt+1} 次生成失败: {e}，"
                          f"{delay:g}s 后重试")
                    time.sleep(delay)
        if last_error is not None:
            raise last_error
        _write_audio_text_snapshot(audio_path, seg.get("text", ""))
        normalize_loudness(audio_path)  # 归一失败时内部回退原文件，不会抛异常
        duration = get_audio_duration(audio_path)

        # D2 时间戳通道：按 provider 取真实词/句级时间片（fetch_segment_timestamps
        # 内部已优雅跳过：无 Key/失败/关闭都返回 None）。音频已生成成功，
        # 时间戳任何意外都只损失对齐精度，绝不影响配音主流程
        words: list[dict] | None = None
        try:
            words = fetch_segment_timestamps(seg["text"], provider=provider, voice=voice,
                                             audio_path=audio_path)
        except Exception as e:
            print(f"[TTS] 第 {i+1} 段时间戳通道意外失败（忽略，字幕回退字符比例）: {e}")
        return i, audio_path, duration, None, False, words

    def _fallback(i, error):
        print(f"[TTS] 第 {i+1} 段语音生成失败（已重试 {len(TTS_RETRY_DELAYS)} 次），"
              f"该段将无配音: {error}")
        return i, "", 5.0, str(error), False, None

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(_task, (i, seg)): i for i, seg in enumerate(segments)}
        for future in as_completed(futures):
            i = futures[future]
            try:
                idx, audio_path, duration, error, reused, words = future.result()
            except Exception as e:
                idx, audio_path, duration, error, reused, words = _fallback(i, e)
            segments[idx]["audio_path"] = audio_path
            segments[idx]["duration"] = duration
            segments[idx]["audio_error"] = error
            if reused:
                segments[idx]["audio_reused"] = True
            if words:
                # D2 时间戳通道契约（与 aligner.py 共用同一字段）：
                # seg["words"] = 真实时间片 + words_source 标记来源；
                # 末尾 aligner.fill_missing_words 只补缺，不会覆盖这里的产出
                segments[idx]["words"] = words
                segments[idx]["words_source"] = "tts"
            if not error:
                print(f"[TTS] 第 {idx+1}/{total} 段语音完成，时长 {duration:.1f}s"
                      + ("（复用）" if reused else ""))

    # P0-2 Tier3（词级时间戳引擎无关兜底，见 aligner.py）：TTS 未返回词级
    # 时间戳的段（含断点续跑复用段）用本地对齐引擎补 seg["words"]。
    # D2 的 TTS 时间戳通道已写入 seg["words"] 的段原样保留（只补缺不覆盖）；
    # 引擎未安装 / ALIGNER=off 时内部优雅跳过并打印安装指引，绝不自动安装
    # 大依赖（如 torch），对齐失败也绝不阻塞出片。
    aligner.fill_missing_words(segments, project_dir=proj_dir)

    return segments
