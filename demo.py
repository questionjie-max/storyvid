"""演示模式（--demo / DEMO=1）：零 Key 全离线出片

给没配任何 API Key 的新用户一条半分钟看到成品效果的路径：
- 故事稿：内置示例文稿 DEMO_STORY（也可 --script-file 换成用户自己的文稿），
  跳过搜索 / 内容审核 / LLM 写稿，全程不调 llm.chat；
- 配音：跳过 TTS API，每段生成固定 3 秒静音 mp3（ffmpeg anullsrc 离线合成，
  复用 imageio_ffmpeg 自带的 ffmpeg——视频合成本身就依赖它，不新增依赖）；
- 配图：每段生成 PIL 渐变底 + 大字占位图（复用 video._build_card_frame 的
  片头卡渲染，逐段轮换配色），不调任何配图 API。

断点续跑语义与正式管线一致：已存在且非空的 segment_XXX.mp3 直接复用，
中断重跑不重复生成；image_XXX.jpg 还要求能完整解码（坏图重新生成）。
"""
import os
import re
import subprocess

import config
from images import _is_valid_image_file
from tts import _get_ffmpeg, _is_ready_file, get_audio_duration
from video import _build_card_frame

# 每段配音/展示时长（秒）：演示模式固定值，字幕与合成据此对齐。
# 读值发生在调用时（duration=0 再回退本常量），测试可 monkeypatch 本常量
# 缩短端到端出片用时。
DEMO_SEGMENT_DURATION = 3.0

DEMO_TOPIC = "深夜的一碗热汤面"

# 内置示例文稿：空行分段（split_story_into_segments 按 [段落] 或空行切），
# 每段一行、长度≥10 字，覆盖句末标点（字幕逐句切块的演示也覆盖到）。
DEMO_STORY = """\
加班到深夜，写字楼下的面馆还亮着一盏暖黄的灯。
推门进去，热气混着葱花的香气扑面而来，一整天的疲惫忽然就有了去处。

老板话不多，下面、冲汤、撒葱花，动作快得像排练过一千遍。
三分钟后，一碗热汤面端上桌：汤色清亮，面条雪白，卧着一颗溏心蛋。

第一口热汤下肚，胃先醒了，人也跟着活了过来。
原来治愈一个成年人，有时候只需要这么简单的一碗面。

邻座加班的姑娘小口喝着汤，眼眶有点红，嘴角却是弯的。
没有人说话，只有吸面的声音和锅里的热气，安静又热闹。

结账的时候，老板忽然说了句：「慢走，明天见。」
你看，深夜的城市并不冷，总有一盏灯和一碗面在等你。

回家的路上风有点凉，胃里却是暖的。
明天再难，也先睡个好觉吧。"""

# 占位图逐段轮换的渐变配色（深色系，保证白字可读；前四组与 cover._PALETTES 同源）
_DEMO_GRADIENTS = [
    ((23, 42, 94), (94, 32, 128)),    # 深蓝 → 紫
    ((8, 58, 84), (5, 116, 148)),     # 墨绿 → 海蓝
    ((78, 26, 48), (140, 44, 88)),    # 酒红 → 玫红
    ((18, 50, 40), (48, 110, 94)),    # 松绿 → 苔绿
    ((46, 36, 100), (100, 68, 142)),  # 靛蓝 → 藤紫
    ((96, 52, 16), (150, 88, 24)),    # 赭石 → 暖棕
]


def fill_demo_keywords(segments: list[dict], topic: str = "") -> list[dict]:
    """演示模式配图关键词：取每段文案开头 12 字作占位图大字（不调 LLM）。

    每段大字取自本段开头（去空白截 12 字），比统一用主题更有「分镜」感；
    空段兜底用主题，再兜底「示例画面 N」。与 fill_fallback_keywords 一样
    只改 image_keyword 字段，不动其他键。
    """
    fallback_topic = (topic or "").strip()
    for i, seg in enumerate(segments):
        head = re.sub(r"\s+", "", seg.get("text", ""))[:12]
        seg["image_keyword"] = head or fallback_topic or f"示例画面{i + 1}"
    return segments


def generate_silence(output_path: str, duration: float = 0.0) -> str:
    """离线合成固定时长静音 mp3（ffmpeg anullsrc，不出网、不需要任何 Key）。

    imageio_ffmpeg 自带的 ffmpeg 是本项目的硬依赖（视频合成本身就要用），
    演示模式的静音配音直接复用它。失败抛异常，由调用方决定是否中断。
    """
    dur = float(duration or DEMO_SEGMENT_DURATION)
    cmd = [
        _get_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
        "-t", f"{max(dur, 0.1):.3f}",
        "-c:a", "libmp3lame", "-q:a", "7",
        output_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 生成静音段失败: {proc.stderr.strip()[:300]}")
    if not os.path.isfile(output_path) or os.path.getsize(output_path) == 0:
        raise RuntimeError(f"ffmpeg 生成静音段未产出文件: {output_path}")
    return output_path


def fill_demo_speech(
    segments: list[dict],
    provider: str = "",
    voice: str = "",
    project_dir: str = "",
    duration: float = 0.0,
) -> list[dict]:
    """演示模式配音：跳过 TTS API，每段固定 3 秒静音段（离线 ffmpeg 合成）。

    与 generate_all_speech 调用点签名兼容（provider/voice 仅占位保留）；
    断点续跑语义一致：已存在且非空的 segment_XXX.mp3 直接复用，不再重写。
    段落 duration 固定 DEMO_SEGMENT_DURATION（音频若因编码器填充略长，
    合成层 video._build_segment_clip 会按 duration 截断，不影响对齐）。
    project_dir 为空时回退全局项目目录（与 generate_all_speech 相同）。
    """
    dur = float(duration or DEMO_SEGMENT_DURATION)
    proj_dir = project_dir or config.get_project_dir()
    os.makedirs(proj_dir, exist_ok=True)
    total = len(segments)
    for i, seg in enumerate(segments):
        audio_path = os.path.join(proj_dir, f"segment_{i:03d}.mp3")
        if _is_ready_file(audio_path):
            print(f"[Demo] 第 {i + 1}/{total} 段静音配音已存在，直接复用")
        else:
            print(f"[Demo] 生成第 {i + 1}/{total} 段静音配音（{dur:g}s）...")
            generate_silence(audio_path, dur)
        seg["audio_path"] = audio_path
        seg["duration"] = dur
    return segments


def fetch_demo_images(
    segments: list[dict],
    image_source: str = "",
    aspect_ratio: str = "",
    project_dir: str = "",
) -> list[dict]:
    """演示模式配图：PIL 渐变底 + 大字占位图，不调任何配图 API。

    与 fetch_all_images 调用点签名兼容（image_source 仅占位保留）；
    尺寸跟随 config.video_size(aspect_ratio)，批量画幅时首画幅原生生成、
    其余画幅仍走 images.derive_crops_for_aspect 智能裁切（与正式管线一致）。
    断点续跑：已存在、非空且能完整解码的 image_XXX.jpg 直接复用（损坏的非空
    坏图重新生成——演示占位图离线零成本，不沿用会让合成崩溃的坏图，
    与 images._is_valid_image_file 同一判据）；逐段轮换 _DEMO_GRADIENTS
    配色，大字用该段的 image_keyword（fill_demo_keywords 填充的段首文案）。
    """
    proj_dir = project_dir or config.get_project_dir()
    os.makedirs(proj_dir, exist_ok=True)
    width, height = config.video_size(aspect_ratio)
    total = len(segments)
    for i, seg in enumerate(segments):
        img_path = os.path.join(proj_dir, f"image_{i:03d}.jpg")
        if _is_valid_image_file(img_path):
            print(f"[Demo] 第 {i + 1}/{total} 段占位图已存在，直接复用")
        else:
            if _is_ready_file(img_path):
                print(f"[Demo] 第 {i + 1}/{total} 段占位图已损坏，删除后重新生成")
                try:
                    os.remove(img_path)
                except OSError as e:
                    print(f"[Demo] 坏图删除失败（重新生成将覆盖它）: {e}")
            keyword = seg.get("image_keyword") or f"示例画面 {i + 1}"
            gradient = _DEMO_GRADIENTS[i % len(_DEMO_GRADIENTS)]
            print(f"[Demo] 生成第 {i + 1}/{total} 段占位图 (text: {keyword})...")
            _build_card_frame(keyword, width, height, img_path, gradient, height // 8)
        seg["image_path"] = img_path
    return segments
