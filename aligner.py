"""引擎无关的词级时间戳兜底对齐（R1 P0-2 Tier3，任务 D3）

D2 的 TTS 时间戳通道（StepFun WS 词级 / MiniMax 句级）未产出词级时间戳时
（TTS 提供商不支持、时间戳参数未开、或断点续跑复用的旧音频），本模块用
本地 Whisper 系引擎对「已生成的配音 mp3」做语音识别，补齐词级时间戳。
纯本地推理，不需要任何 API Key。

通道契约（与 D2 的 TTS 时间戳通道共用同一字段，见 tts.generate_all_speech）：

    seg["words"] = [{"word": str, "start": float, "end": float}, ...]
    seg["words_source"] = "tts"（D2 通道）| "aligner"（本模块）

- 时间均为「段内相对秒」，与 seg["audio_path"] 音频时间轴一致；
  成片级偏移（片头卡 offset、段间 crossfade）由下游按段起点自行平移；
- 本模块只补缺：已有 seg["words"] 的段一律原样保留，绝不覆盖 TTS 时间戳。

环境变量（调用时读取，改 env 无需重启进程；配置模板见 .env.example）：

    ALIGNER=auto（默认，自动检测已装引擎）| off（关闭）
           | faster-whisper / whisper-timestamped（强制指定引擎）
    ALIGNER_MODEL=small        whisper 模型尺寸（tiny/base/small/medium/large-v3）
    ALIGNER_LANGUAGE=zh        识别语言；设 auto/空 = 让引擎自动检测

两个可选引擎（任装其一即可，本程序绝不自动安装任何依赖）：

    pip install faster-whisper                        # 推荐：ctranslate2 CPU int8，无 torch
    pip install openai-whisper whisper-timestamped    # 较重（引入 torch）

引擎检测用 importlib.util.find_spec 只查「是否安装」、不真正 import
（whisper 系引擎一 import 就拉起 torch/ctranslate2，检测阶段绝不加载大依赖）；
模型也只在真正需要对齐时才惰性加载，并按 (引擎, 尺寸) 进程内缓存。
"""
import importlib.util
import json
import math
import os

import config

# 引擎注册表：名称 → (python 模块名, 转写函数名)。列表顺序即 auto 模式优先级：
# faster-whisper 在前——ctranslate2 CPU int8 推理比 openai-whisper 路线的
# whisper-timestamped 更轻更快，且不引入 torch（两者都装了时默认用轻的，
# 可用 ALIGNER=whisper-timestamped 强制切换）。
_ENGINES: tuple[tuple[str, str, str], ...] = (
    ("faster-whisper", "faster_whisper", "_transcribe_faster_whisper"),
    ("whisper-timestamped", "whisper_timestamped", "_transcribe_whisper_timestamped"),
)

# ALIGNER=off 的等价写法（与 LOUDNESS_NORM 等开关的取值约定一致）
_OFF_VALUES = {"off", "0", "false", "no"}

# 模型进程内缓存：{(引擎名, 模型尺寸): 模型对象}——web 常驻进程下逐段加载
# whisper 模型（数百 MB）不可接受；测试用例可直接清空本表隔离。
_MODEL_CACHE: dict[tuple[str, str], object] = {}

_HINT_SHOWN = False  # 安装指引每进程只打一次，避免逐段刷屏


def _env(name: str, default: str) -> str:
    """读 env：缺失/空白回退默认（config._env_int 同思路，str 版）"""
    return (os.getenv(name) or "").strip() or default


def _find_spec(module_name: str):
    """importlib.util.find_spec 的薄封装（可测试替身注入点）。

    只探测「模块是否已安装」，不执行模块代码；探测器自身出错一律视为未安装。
    """
    try:
        return importlib.util.find_spec(module_name)
    except (ImportError, ValueError, AttributeError):
        return None


def _normalize_engine_name(raw: str) -> str:
    """引擎名归一：大小写、下划线/连杠混写都接受（faster_whisper = faster-whisper）"""
    return (raw or "").strip().lower().replace("_", "-")


def _aligner_mode() -> str:
    """解析 ALIGNER env → "off" / "auto" / 归一后的引擎名。

    非法值打警告按 auto 处理（与 SUBTITLE_STYLE 的兜底策略一致，不中断生成）。
    """
    raw = _normalize_engine_name(_env("ALIGNER", "auto"))
    if raw in _OFF_VALUES:
        return "off"
    if raw in ("", "auto"):
        return "auto"
    if raw in {name for name, _, _ in _ENGINES}:
        return raw
    print(f"[Align] 警告: ALIGNER={raw!r} 无法识别（支持 auto / off / "
          f"{' / '.join(name for name, _, _ in _ENGINES)}），按 auto 处理")
    return "auto"


def enabled() -> bool:
    """ALIGNER 开关是否放行（off 族取值 → False）"""
    return _aligner_mode() != "off"


def available_engines() -> list[str]:
    """已安装（但不加载）的对齐引擎列表，按注册表优先级排序"""
    return [name for name, module, _ in _ENGINES if _find_spec(module) is not None]


def _print_install_hint() -> None:
    global _HINT_SHOWN
    if _HINT_SHOWN:
        return
    _HINT_SHOWN = True
    print("[Align] 本地对齐引擎未安装，词级时间戳兜底不可用（已跳过，不影响出片）。"
          "如需词级时间戳，任选其一手动安装（本程序绝不自动安装）：")
    print("          pip install faster-whisper                       # 推荐：较轻，无 torch")
    print("          pip install openai-whisper whisper-timestamped   # 较重（含 torch）")
    print("        安装后无需其他配置（ALIGNER=auto 自动检测）；设 ALIGNER=off 关闭本提示。")


def resolve_engine() -> str | None:
    """按 ALIGNER 配置解析出本次可用的引擎名；不可用返回 None（指引只打一次）。

    - auto：返回已装引擎中优先级最高者（注册表顺序）；
    - 强制引擎：未安装时同样优雅跳过（不自动安装、不报错中断）。
    """
    mode = _aligner_mode()
    if mode == "off":
        return None
    installed = available_engines()
    if mode != "auto":
        if mode in installed:
            return mode
        _print_install_hint()
        return None
    if installed:
        return installed[0]
    _print_install_hint()
    return None


# ---------- 词记录归一 ----------
# 两个引擎的词结构不同：
# - whisper-timestamped: dict {"text": str, "start": float|None, "end": float|None,
#   "confidence": float}（低置信词的 start/end 可能是 None）
# - faster-whisper: Word 对象（属性 .word/.start/.end，段.words 可能为 None）
# 统一归一为通道契约 {"word", "start", "end"}，不合规则的记录直接丢弃。


def _norm_word(raw) -> dict | None:
    if isinstance(raw, dict):
        text = raw.get("word", raw.get("text"))
        start, end = raw.get("start"), raw.get("end")
    else:
        text = getattr(raw, "word", None) or getattr(raw, "text", None)
        start = getattr(raw, "start", None)
        end = getattr(raw, "end", None)
    text = str(text).strip() if text is not None else ""
    if not text:
        return None
    try:
        start = float(start)
        end = float(end)
    except (TypeError, ValueError):
        return None  # None / 非数值（如 whisper-timestamped 低置信词）
    if not (math.isfinite(start) and math.isfinite(end)):
        return None
    if start < 0 or end <= 0:
        return None
    return {"word": text, "start": start, "end": max(end, start)}


def normalize_words(raw_words) -> list[dict]:
    """把引擎返回的词列表归一为通道契约结构；空入参返回 []"""
    words: list[dict] = []
    for raw in raw_words or []:
        w = _norm_word(raw)
        if w:
            words.append(w)
    words.sort(key=lambda w: (w["start"], w["end"]))
    return words


# ---------- 引擎适配（惰性 import + 模型缓存）----------


def _load_model(engine: str, model_size: str):
    """加载并缓存 whisper 模型；只在对齐真正发生时才 import 引擎。"""
    key = (engine, model_size)
    if key not in _MODEL_CACHE:
        if engine == "whisper-timestamped":
            import whisper_timestamped as wts
            _MODEL_CACHE[key] = wts.load_model(model_size)
        elif engine == "faster-whisper":
            from faster_whisper import WhisperModel
            # CPU + int8：无 GPU 环境的可预测默认值（量化后精度损失对词级
            # 时间戳可忽略）；有 GPU 的用户可自行改造此处
            _MODEL_CACHE[key] = WhisperModel(model_size, device="cpu", compute_type="int8")
        else:  # 注册表与适配器表失配属编程错误，直接暴露
            raise ValueError(f"未知对齐引擎: {engine}")
    return _MODEL_CACHE[key]


def _transcribe_whisper_timestamped(audio_path: str, language: str,
                                    model_size: str) -> list[dict]:
    """whisper-timestamped：transcribe 返回 result["segments"][].words（dict 词）"""
    import whisper_timestamped as wts
    model = _load_model("whisper-timestamped", model_size)
    result = wts.transcribe(model, audio_path, language=language or None)
    raw: list = []
    for seg in result.get("segments", []):
        raw.extend(seg.get("words") or [])
    return normalize_words(raw)


def _transcribe_faster_whisper(audio_path: str, language: str,
                               model_size: str) -> list[dict]:
    """faster-whisper：transcribe 返回 (segments 生成器, info)，须遍历才真正推理"""
    model = _load_model("faster-whisper", model_size)
    segments, _info = model.transcribe(
        audio_path, language=language or None, word_timestamps=True)
    raw: list = []
    for seg in segments:  # 生成器：不遍历就不执行
        raw.extend(getattr(seg, "words", None) or [])
    return normalize_words(raw)


_TRANSCRIBERS = {
    name: globals()[fn_name]
    for name, _, fn_name in _ENGINES
}


# ---------- 对外接口 ----------

def align_segment(audio_path: str, text: str = "", engine: str = "",
                  model_size: str = "", language: str | None = None) -> list[dict]:
    """对单段配音做词级对齐，返回通道契约词列表；任何失败都返回 []，绝不抛异常。

    text 仅作记录用途（本实现对配音直接做 ASR 取词级时间戳——音频本就是
    该段文稿的 TTS 产物，识别出的词时间轴即真实发音位置；不做 wav2vec2
    强制对齐是为了不引入第二类大模型依赖）。
    """
    if not audio_path or not os.path.isfile(audio_path) or os.path.getsize(audio_path) <= 0:
        return []
    engine = _normalize_engine_name(engine) or resolve_engine()
    if not engine:
        return []  # 未安装 / 已关闭：安装指引已由 resolve_engine 打印
    transcriber = _TRANSCRIBERS.get(engine)
    if transcriber is None:
        print(f"[Align] 警告: 引擎 {engine} 没有可用适配器，跳过对齐")
        return []
    model_size = model_size or _env("ALIGNER_MODEL", "small")
    if language is None:
        language = _env("ALIGNER_LANGUAGE", "zh")
    if language.lower() in ("auto", "none", "null"):
        language = ""  # 引擎自动检测
    try:
        return transcriber(audio_path, language, model_size)
    except Exception as e:  # 引擎崩溃/模型下载失败等一律优雅跳过该段
        print(f"[Align] 对齐失败（跳过，不影响出片）: engine={engine} "
              f"audio={os.path.basename(audio_path)}: {e}")
        return []


def _words_cache_path(project_dir: str, index: int) -> str:
    """词级时间戳缓存文件：与 segment_XXX.mp3 同目录同序号（断点续跑配套）"""
    return os.path.join(project_dir, f"segment_{index:03d}.words.json")


def _load_words_cache(path: str, text: str, engine: str, model_size: str) -> list[dict]:
    """读缓存；文案/引擎/模型任一不匹配（或文件损坏）都视为过期"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    if data.get("text") != text or data.get("engine") != engine \
            or data.get("model") != model_size:
        return []  # 文案已改或引擎/模型换了：旧缓存作废，重新对齐
    words = data.get("words")
    return normalize_words(words) if isinstance(words, list) else []


def _save_words_cache(path: str, text: str, engine: str, model_size: str,
                      words: list[dict]) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"text": text, "engine": engine, "model": model_size,
                       "words": words}, f, ensure_ascii=False)
    except OSError as e:
        print(f"[Align] 词级时间戳缓存写入失败（忽略，不影响出片）: {e}")


def fill_missing_words(segments: list[dict], project_dir: str = "") -> list[dict]:
    """通道接入点：给没有词级时间戳的段补 seg["words"]（就地修改并返回）。

    - 已有 seg["words"] 的段（D2 的 TTS 时间戳通道产出）：原样保留，不重算；
    - 无配音段（audio_path 缺失/文件不存在，TTS 失败兜底段）：跳过；
    - ALIGNER=off 或引擎未安装：整体优雅跳过（引擎缺失时首次打印安装指引）；
    - 断点续跑：对齐结果缓存 segment_XXX.words.json（含 text/engine/model
      校验），复用段直接读缓存，不重复跑 Whisper 推理。

    本函数绝不抛异常、绝不阻塞出片——对齐是锦上添花，失败只损失词级精度。
    """
    try:
        return _fill_missing_words(segments, project_dir)
    except Exception as e:  # 双保险：任何意外都不影响配音主流程
        print(f"[Align] 词级时间戳兜底意外失败（忽略，不影响出片）: {e}")
        return segments


def _fill_missing_words(segments: list[dict], project_dir: str) -> list[dict]:
    if not segments or not enabled():
        return segments
    engine = resolve_engine()
    if not engine:
        return segments
    proj_dir = project_dir or config.get_project_dir()
    model_size = _env("ALIGNER_MODEL", "small")
    total = len(segments)
    aligned = 0
    for i, seg in enumerate(segments):
        if seg.get("words"):
            continue  # D2 通道已有词级时间戳，不覆盖
        audio_path = seg.get("audio_path") or ""
        if not audio_path or not os.path.isfile(audio_path):
            continue  # 无配音段（TTS 失败被跳过的段）无从对齐
        text = seg.get("text", "")
        cache_path = _words_cache_path(proj_dir, i)
        words = _load_words_cache(cache_path, text, engine, model_size)
        if not words:
            words = align_segment(audio_path, text=text, engine=engine,
                                  model_size=model_size)
            if words:
                _save_words_cache(cache_path, text, engine, model_size, words)
        if words:
            seg["words"] = words
            seg["words_source"] = "aligner"
            aligned += 1
    if aligned:
        print(f"[Align] {aligned}/{total} 段已用本地引擎 {engine} "
              f"(model={model_size}) 补齐词级时间戳")
    return segments
