"""视频合成（MoviePy 2.x）"""
import datetime
import glob
import json
import os
import random
import re
import shutil
import subprocess

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from moviepy import (
    AudioFileClip,
    CompositeAudioClip,
    CompositeVideoClip,
    ImageClip,
    VideoFileClip,
    afx,
    concatenate_videoclips,
    vfx,
)

import config
import subtitles as subtitle_mod

# ---------- 片头/片尾卡 ----------
INTRO_CARD_DURATION = 1.8  # 片头卡时长（秒）：短视频黄金前 3 秒是完播率关键，不宜久留
OUTRO_CARD_DURATION = 1.5  # 片尾卡时长（秒）

# INTRO_TEXT/OUTRO_TEXT 与主题都为空时的文案兜底
_DEFAULT_INTRO_TEXT = "精彩视频马上开始"
_DEFAULT_OUTRO_TEXT = "喜欢这个视频的话，请点赞、关注，看更多精彩故事！"

# 卡片渐变底色（顶部 RGB → 底部 RGB）
_INTRO_GRADIENT = ((23, 42, 94), (94, 32, 128))
_OUTRO_GRADIENT = ((94, 32, 128), (23, 42, 94))


def intro_offset() -> float:
    """片头卡把正片时间轴整体后移的秒数；关闭片头片尾卡时为 0。

    供 generate_srt 做同步偏移，保证字幕与成片对齐。
    """
    return INTRO_CARD_DURATION if config.INTRO_OUTRO else 0.0


def cards_duration() -> float:
    """片头卡 + 片尾卡总时长；关闭片头片尾卡时为 0（用于成片时长统计）"""
    return (INTRO_CARD_DURATION + OUTRO_CARD_DURATION) if config.INTRO_OUTRO else 0.0


def crossfade_seconds() -> float:
    """段间交叉淡化秒数（CROSSFADE_SECONDS，负值视为 0）。

    供 generate_srt 同步时间轴与成片时长估算使用。
    """
    return max(float(config.CROSSFADE_SECONDS), 0.0)


def _crossfade_layout(durations: list[float], xfade: float) -> tuple[list[float], list[float]]:
    """计算段间交叉淡化布局：返回 (相邻段有效重叠列表, 各段在正片内的起点)。

    xfade<=0 或不足两段时无重叠，起点退化为顺序前缀和（与硬切拼接一致）。
    每对重叠按 min(xfade, 前段长, 后段长) 截断，避免段长不足时后段起点早于前段。
    最终正片时长 = sum(durations) - sum(overlaps)，即「各段之和 - 重叠」。
    """
    xfade = max(float(xfade), 0.0)
    overlaps: list[float] = []
    if xfade > 0 and len(durations) >= 2:
        overlaps = [
            min(xfade, durations[i], durations[i + 1])
            for i in range(len(durations) - 1)
        ]
    starts = [0.0]
    for i in range(len(durations) - 1):
        ov = overlaps[i] if i < len(overlaps) else 0.0
        starts.append(starts[-1] + durations[i] - ov)
    return overlaps, starts


def crossfade_overlap_total(durations: list[float]) -> float:
    """交叉淡化从成片总时长中扣除的重叠总和；关闭或不足两段时为 0。

    供成片时长估算（main.py 的提示文案）使用。
    """
    return sum(_crossfade_layout(durations, crossfade_seconds())[0])


def _get_system_font(size: int):
    """找系统中文字体：FONT_PATH → 常见路径 → fc-match → 目录扫描 → 默认字体"""
    candidates = [
        config.FONT_PATH,
        # Windows
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        # macOS
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        # Linux
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/arphic/uming.ttc",
        "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass

    # fontconfig 兜底：让系统自己找中文字体
    if shutil.which("fc-match"):
        try:
            out = subprocess.run(
                ["fc-match", "-f", "%{file}", ":lang=zh"],
                capture_output=True, text=True, timeout=5,
            )
            path = out.stdout.strip()
            if path and os.path.exists(path):
                return ImageFont.truetype(path, size)
        except Exception:
            pass

    # 目录扫描兜底（覆盖非标准安装位置）
    for pattern in (
        "/usr/share/fonts/**/*CJK*.tt*",
        "/usr/share/fonts/**/*Noto*Sans*.tt*",
    ):
        for path in glob.glob(pattern, recursive=True):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue

    print("[Video] 警告: 未找到中文字体，字幕可能显示为方块。"
          "可在 .env 中设置 FONT_PATH 指定字体文件")
    return ImageFont.load_default()


def _wrap_text(text: str, font: ImageFont.ImageFont, max_width: int, draw: ImageDraw.ImageDraw) -> list[str]:
    """按宽度自动换行"""
    lines = []
    current = ""
    for char in text:
        test = current + char
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] - bbox[0] > max_width and current:
            lines.append(current)
            current = char
        else:
            current = test
    if current:
        lines.append(current)
    return lines if lines else [text]


def _make_gradient_image(
    width: int,
    height: int,
    top: tuple[int, int, int],
    bottom: tuple[int, int, int],
) -> Image.Image:
    """生成垂直渐变底图：先画 1×h 色带再拉伸，避免逐像素画大图"""
    strip = Image.new("RGB", (1, max(height, 1)))
    for y in range(strip.height):
        t = y / max(strip.height - 1, 1)
        strip.putpixel(
            (0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        )
    return strip.resize((max(width, 1), max(height, 1)))


def _fit_card_text(
    text: str,
    width: int,
    height: int,
    draw: ImageDraw.ImageDraw,
    base_size: int,
) -> tuple[ImageFont.ImageFont, list[str], int]:
    """选一个能把文字放进卡片的字号，返回 (字体, 换行结果, 字号)"""
    max_width = int(width * 0.82)
    max_block_height = int(height * 0.62)
    size = max(int(base_size), 24)
    while size > 24:
        font = _get_system_font(size)
        lines = _wrap_text(text, font, max_width, draw)
        if len(lines) * int(size * 1.5) <= max_block_height:
            return font, lines, size
        size = int(size * 0.85)
    font = _get_system_font(24)
    return font, _wrap_text(text, font, max_width, draw), 24


def _build_card_frame(
    text: str,
    width: int,
    height: int,
    frame_path: str,
    gradient: tuple[tuple[int, int, int], tuple[int, int, int]],
    base_size: int,
) -> str:
    """画一张卡片帧：渐变底 + 居中大字（黑描边）+ 文字下方装饰线"""
    img = _make_gradient_image(width, height, *gradient)
    draw = ImageDraw.Draw(img)
    font, lines, size = _fit_card_text(text, width, height, draw, base_size)

    line_height = int(size * 1.5)
    block_height = len(lines) * line_height
    margin = max((height - block_height) // 2, 0)
    y = margin
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        x = (width - (bbox[2] - bbox[0])) // 2
        for dx, dy in [(-3, -3), (-3, 0), (-3, 3), (0, -3), (0, 3), (3, -3), (3, 0), (3, 3)]:
            draw.text((x + dx, y + dy), line, font=font, fill=(0, 0, 0))
        draw.text((x, y), line, font=font, fill=(255, 255, 255))
        y += line_height

    # 文字块与底边之间的居中短装饰线
    bar_w = max(int(width * 0.12), 24)
    bar_h = max(height // 270, 3)
    bar_y = min(y + max(margin // 4, 8), height - bar_h - 1)
    draw.rectangle(
        [(width - bar_w) // 2, bar_y, (width + bar_w) // 2, bar_y + bar_h],
        fill=(255, 205, 90),
    )

    img.save(frame_path, quality=95)
    return frame_path


def _build_intro_card(width: int, height: int, proj_dir: str, text: str) -> ImageClip:
    """片头卡：渐变底 + 主题大字，时长固定 INTRO_CARD_DURATION 秒"""
    title = text.strip() or _DEFAULT_INTRO_TEXT
    frame_path = os.path.join(proj_dir, "_intro_card.jpg")
    _build_card_frame(title, width, height, frame_path, _INTRO_GRADIENT, height // 8)
    return ImageClip(frame_path).with_duration(INTRO_CARD_DURATION)


def _build_outro_card(width: int, height: int, proj_dir: str, text: str) -> ImageClip:
    """片尾卡：渐变底 + 引导关注文案，时长固定 OUTRO_CARD_DURATION 秒"""
    copy = text.strip() or _DEFAULT_OUTRO_TEXT
    frame_path = os.path.join(proj_dir, "_outro_card.jpg")
    _build_card_frame(copy, width, height, frame_path, _OUTRO_GRADIENT, height // 12)
    return ImageClip(frame_path).with_duration(OUTRO_CARD_DURATION)


def _resolve_card_texts(topic: str) -> tuple[str, str]:
    """片头/片尾文案：INTRO_TEXT/OUTRO_TEXT 优先，其次视频主题，最后内置兜底"""
    intro = (config.INTRO_TEXT or topic or _DEFAULT_INTRO_TEXT).strip() or _DEFAULT_INTRO_TEXT
    outro = (config.OUTRO_TEXT or _DEFAULT_OUTRO_TEXT).strip() or _DEFAULT_OUTRO_TEXT
    return intro, outro


def _highlight_pattern() -> "re.Pattern[str]":
    """card 模式的高亮规则：阿拉伯数字（含小数/百分号）与成对引号内的关键词。

    引号内容超过 config.SUBTITLE_HIGHLIGHT_QUOTE_MAX 视为整句引用，不高亮。
    """
    cap = max(int(config.SUBTITLE_HIGHLIGHT_QUOTE_MAX), 1)
    return re.compile(
        r"[0-9]+(?:[.,][0-9]+)*%?"
        + r'|[“"][^”"]{1,' + str(cap) + r'}[”"]'
        + r"|「[^」]{1," + str(cap) + r"}」"
        + r"|『[^』]{1," + str(cap) + r"}』"
    )


def _tokenize_highlight_runs(text: str) -> list[tuple[str, bool]]:
    """把一行字幕切成 (文本, 是否主题色高亮) 序列（card 模式用）。

    高亮对象：阿拉伯数字串（如 3、102.5、30%）与引号内关键词
    （“…”、"…"、「…」、『…』，连同引号一起高亮）。
    """
    runs: list[tuple[str, bool]] = []
    pos = 0
    for m in _highlight_pattern().finditer(text):
        if m.start() > pos:
            runs.append((text[pos:m.start()], False))
        runs.append((m.group(0), True))
        pos = m.end()
    if pos < len(text):
        runs.append((text[pos:], False))
    return runs or [(text, False)]


def _subtitle_bottom_margin(width: int, height: int) -> int:
    """字幕条距画面底边（像素）：SUBTITLE_BOTTOM_MARGIN 显式配置优先；
    0 = 自动：竖屏 240（平台底部 UI 安全区，约 12.5%）/ 横屏 60。

    竖屏旧值 80（约 4%）会被抖音/视频号的文案栏与进度条盖住，质量循环
    R5 基线实测后调整；三种字幕样式（plain/card/karaoke）共用。
    """
    if config.SUBTITLE_BOTTOM_MARGIN > 0:
        return config.SUBTITLE_BOTTOM_MARGIN
    return 240 if height > width else 60


def _card_geometry(n_lines: int, width: int, height: int) -> tuple[int, int, int, int]:
    """card 样式底部卡片条的位置尺寸 (x, y, w, h)，供绘制与测试共用"""
    font_size = 38 if height > width else 42
    line_height = font_size + 12
    padding = max(config.SUBTITLE_CARD_PADDING, 0)
    side_margin = max(config.SUBTITLE_CARD_MARGIN, 0)
    card_h = max(n_lines, 1) * line_height + padding * 2
    card_w = width - side_margin * 2
    bottom = config.SUBTITLE_CARD_BOTTOM_MARGIN or _subtitle_bottom_margin(width, height)
    card_y = max(height - card_h - bottom, 0)
    return side_margin, card_y, card_w, card_h


def _render_subtitle_style(img: Image.Image, text: str, width: int, height: int,
                           active_word: int = 0) -> Image.Image:
    """按 config.SUBTITLE_STYLE 在给定底图上渲染字幕（烘焙与透明叠加共用）。

    active_word 仅 karaoke 样式使用：当前高亮词序号（token 序号，0 起；
    -1 = 开唱前静音，全白）。plain/card 忽略该参数（默认 0 保持旧签名兼容）。
    """
    if config.SUBTITLE_STYLE == "card":
        return _draw_card_subtitle(img, text, width, height)
    if config.SUBTITLE_STYLE == "karaoke":
        return _draw_karaoke_subtitle(img, text, width, height, active_word)
    return _draw_plain_subtitle(img, text, width, height)


def fit_image_cover(img: Image.Image, width: int, height: int) -> Image.Image:
    """底图等比适配（cover）：按目标宽高比取源图最大居中裁剪框，再缩放到目标画幅。

    修复 P0-1（R1_competitive.md §五）：旧逻辑 `img.resize((width, height))`
    把非同画幅素材（如横图进竖屏）直接拉变形。cover 语义与 images.crop_to_fill
    （images.py:39-56）/ cover._cover_crop 一致（等比铺满 + 中心裁切），
    但实现走「先裁后缩」（ImageOps.fit 思路）而非「先缩后裁」：

    - 内存上界与源画幅比无关：唯一一次 resize 的目标恒为输出画幅，
      裁剪框 ≤ 源图尺寸。B7 初版「先缩后裁」在极端画幅源（如 1×5000 进
      480×270）会先放大出 ≈3.46GB 中间图（B 队实测 13s / 峰值 RSS 4.72GB），
      单段坏比例图即可拖垮整次渲染——先裁后缩根除此回归面；
    - 同画幅输入（宽高整数积相等）裁剪框 = 整图，resize 与旧纯 resize
      逐像素一致（零回归锚点）。
    """
    sw, sh = img.size
    if sw <= 0 or sh <= 0:
        return img.resize((width, height), Image.Resampling.LANCZOS)
    # 整数交叉积判源/目标宽高比，免浮点误差
    if sw * height > sh * width:  # 源相对更宽 → 保留全高、按比例裁宽
        crop_w, crop_h = min(sw, round(sh * width / height)), sh
    else:                         # 等比或更高 → 保留全宽、按比例裁高
        crop_w, crop_h = sw, min(sh, round(sw * height / width))
    left, top = (sw - crop_w) // 2, (sh - crop_h) // 2
    region = img.crop((left, top, left + crop_w, top + crop_h))
    return region.resize((width, height), Image.Resampling.LANCZOS)


def prepare_base_image(image_path: str, width: int, height: int) -> Image.Image:
    """打开底图并等比适配到目标画幅（open + convert + cover 缩放裁切一次到位）。

    独立拆出供测试与复用：同一段落的多块字幕共用同一份适配后的底图
    （原先每块都重新 Image.open + resize，一晚上重复几百次磁盘 IO）。
    """
    img = Image.open(image_path).convert("RGB")
    return fit_image_cover(img, width, height)


def render_subtitle_frame(
    base: Image.Image,
    text: str,
    output_path: str,
    width: int,
    height: int,
    active_word: int = 0,
) -> str:
    """在底图的副本上渲染一块字幕并落盘，不修改传入的底图。

    复制后再绘制，保证同一底图能连续服务多块字幕（每块都从干净底图起步，
    与旧逻辑「每块重新打开底图再画」逐像素等价）。
    active_word：karaoke 样式的当前高亮词序号（plain/card 忽略）。
    """
    img = base.copy()
    img = _render_subtitle_style(img, text, width, height, active_word)
    img.save(output_path, quality=95)
    return output_path


def _draw_subtitle_on_image(
    image_path: str,
    text: str,
    output_path: str,
    width: int = 0,
    height: int = 0,
) -> str:
    """用 Pillow 在图片上绘制字幕。

    样式由 config.SUBTITLE_STYLE 决定：
    plain（默认）= 底部整宽半透明黑条 + 白字黑描边（旧行为）；
    card = 底部圆角半透明卡片条 + 主题色描边，数字与引号内关键词主题色高亮；
    karaoke = plain 同款黑条，已唱/当前词主题色高亮（词级时间戳驱动，D5）。

    底图准备与渲染已拆分为 prepare_base_image / render_subtitle_frame，
    本函数保留原签名，逐像素行为与拆分前完全一致。
    """
    width = width or config.video_size()[0]
    height = height or config.video_size()[1]

    base = prepare_base_image(image_path, width, height)
    return render_subtitle_frame(base, text, output_path, width, height)


def _draw_plain_subtitle(img: Image.Image, text: str, width: int, height: int) -> Image.Image:
    """plain 样式（默认，保持旧行为）：底部整宽半透明黑条 + 白字黑描边"""
    draw = ImageDraw.Draw(img)

    # 竖屏时字体稍小，边距调整
    font_size = 38 if height > width else 42
    font = _get_system_font(font_size)

    # 换行
    margin = 60 if height > width else 100
    max_text_width = width - margin * 2
    lines = _wrap_text(text, font, max_text_width, draw)

    # 计算文字区域高度
    line_height = font_size + 12
    text_block_height = len(lines) * line_height + 40

    # 底部半透明背景条（竖屏更靠下）
    bottom_margin = _subtitle_bottom_margin(width, height)
    bar_y = height - text_block_height - bottom_margin
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    overlay_draw.rectangle(
        [(0, bar_y), (width, height)],
        fill=(0, 0, 0, 160)
    )
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(img)

    # 绘制文字（带描边）
    y = bar_y + 20
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        text_w = bbox[2] - bbox[0]
        x = (width - text_w) // 2

        # 黑色描边
        for dx, dy in [(-2, -2), (-2, 0), (-2, 2), (0, -2), (0, 2), (2, -2), (2, 0), (2, 2)]:
            draw.text((x + dx, y + dy), line, font=font, fill=(0, 0, 0))
        # 白色文字
        draw.text((x, y), line, font=font, fill=(255, 255, 255))
        y += line_height

    return img


def _draw_card_subtitle(img: Image.Image, text: str, width: int, height: int) -> Image.Image:
    """card 样式：底部圆角半透明卡片条 + 主题色描边，数字与引号内关键词主题色高亮"""
    draw = ImageDraw.Draw(img)

    # 竖屏时字体稍小（与 plain 一致）
    font_size = 38 if height > width else 42
    line_height = font_size + 12
    font = _get_system_font(font_size)

    side_margin = max(config.SUBTITLE_CARD_MARGIN, 0)
    padding = max(config.SUBTITLE_CARD_PADDING, 0)
    max_text_width = width - side_margin * 2 - padding * 2
    lines = _wrap_text(text, font, max_text_width, draw)

    card_x, card_y, card_w, card_h = _card_geometry(len(lines), width, height)

    # 圆角半透明卡片条：画在 RGBA 蒙层上再与底图合成
    radius = max(min(config.SUBTITLE_CARD_RADIUS, card_h // 2, card_w // 2), 0)
    opacity = max(0, min(config.SUBTITLE_CARD_OPACITY, 255))
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    overlay_draw.rounded_rectangle(
        [card_x, card_y, card_x + card_w, card_y + card_h],
        radius=radius,
        fill=(0, 0, 0, opacity),
    )
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(img)

    # 逐行绘制：先铺主题色描边，再按高亮分段填色（数字/引号关键词用主题色）
    theme = config.subtitle_theme_color()
    stroke_w = max(config.SUBTITLE_CARD_STROKE_WIDTH, 0)
    ring = [
        (dx, dy)
        for dx in range(-stroke_w, stroke_w + 1)
        for dy in range(-stroke_w, stroke_w + 1)
        if (dx, dy) != (0, 0)
    ]

    y = card_y + padding
    for line in lines:
        runs = _tokenize_highlight_runs(line)
        widths = [draw.textlength(tok, font=font) for tok, _ in runs]
        x = (width - sum(widths)) / 2
        for (tok, highlighted), w in zip(runs, widths):
            for dx, dy in ring:
                draw.text((x + dx, y + dy), tok, font=font, fill=theme)
            draw.text((x, y), tok, font=font, fill=theme if highlighted else (255, 255, 255))
            x += w
        y += line_height

    return img


def _draw_karaoke_subtitle(img: Image.Image, text: str, width: int, height: int,
                           active_word: int = 0) -> Image.Image:
    """karaoke 样式（D5）：plain 同款底部半透明黑条 + 逐词主题色高亮。

    token 序号 ≤ active_word 的词（已唱 + 当前词）画主题色、未唱画白色，
    全部带黑描边保证任意底图可读；active_word<0（开唱前静音）整句全白。
    换行按 token 整词移行（不拆词），其余几何（字号/黑条高度/边距）与
    plain 完全一致。词时间表由 subtitles.karaoke_chunk_timings 产出，
    本函数只按序号上色——同一句 + 同一序号逐像素确定（差分叠加层与
    烘焙渲染共用，Ken Burns 下视觉一致）。
    """
    draw = ImageDraw.Draw(img)

    # 与 plain 一致的字号/边距
    font_size = 38 if height > width else 42
    line_height = font_size + 12
    font = _get_system_font(font_size)
    margin = 60 if height > width else 100
    max_text_width = width - margin * 2

    tokens = subtitle_mod.karaoke_tokens(text) or [text]
    widths = [draw.textlength(tok, font=font) for tok in tokens]

    # 按 token 换行（整词移行，不拆 token）
    lines: list[list[int]] = []  # 每行是 token 下标列表
    current: list[int] = []
    current_w = 0.0
    for i, w in enumerate(widths):
        if current and current_w + w > max_text_width:
            lines.append(current)
            current, current_w = [], 0.0
        current.append(i)
        current_w += w
    if current:
        lines.append(current)

    # plain 同款底部半透明黑条
    text_block_height = len(lines) * line_height + 40
    bottom_margin = _subtitle_bottom_margin(width, height)
    bar_y = height - text_block_height - bottom_margin
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    overlay_draw.rectangle([(0, bar_y), (width, height)], fill=(0, 0, 0, 160))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(img)

    theme = config.subtitle_theme_color()
    stroke = [(-2, -2), (-2, 0), (-2, 2), (0, -2), (0, 2), (2, -2), (2, 0), (2, 2)]

    y = bar_y + 20
    idx = 0
    for line in lines:
        line_w = sum(widths[i] for i in line)
        x = (width - line_w) / 2
        for i in line:
            fill = theme if (active_word >= 0 and idx <= active_word) else (255, 255, 255)
            for dx, dy in stroke:
                draw.text((x + dx, y + dy), tokens[i], font=font, fill=(0, 0, 0))
            draw.text((x, y), tokens[i], font=font, fill=fill)
            x += widths[i]
            idx += 1
        y += line_height

    return img


def _subtitle_overlay_rgba(text: str, width: int, height: int,
                           active_word: int = 0) -> Image.Image:
    """生成透明底字幕叠加层（RGBA，Ken Burns 动效背景上叠加用）。

    做法：同一字幕在纯黑、纯白两张底图上各渲染一次（沿用当前 SUBTITLE_STYLE
    渲染器，plain/card/karaoke 通吃），按 source-over 合成公式
        渲染值 = a*F + (1-a)*底色
    逐像素差分反解出字幕层自身的 alpha 与前景色 F：
        白黑差 = (1-a)*255 → a；F = 黑底渲染值 / a
    这样叠加到任意动效背景上，视觉与烘焙字幕完全一致（仅 ±1 整数舍入差）。
    active_word 仅 karaoke 样式使用（当前高亮词序号，-1=开唱前全白）。
    """
    black = _render_subtitle_style(
        Image.new("RGB", (width, height), (0, 0, 0)), text, width, height, active_word
    )
    white = _render_subtitle_style(
        Image.new("RGB", (width, height), (255, 255, 255)), text, width, height, active_word
    )

    b = np.asarray(black, dtype=np.int16)
    w = np.asarray(white, dtype=np.int16)

    # (1-a)*255 在三个通道上理论相同（同一 alpha 的 source-over），取首通道求 a
    alpha = np.clip(255 - (w - b)[..., 0], 0, 255).astype(np.float32)
    safe_a = np.where(alpha > 1e-6, alpha, 1.0)
    # F = 黑底渲染值 / (a/255) = 黑底渲染值 * 255 / a
    fg = np.clip(b.astype(np.float32) * (255.0 / safe_a)[..., None], 0.0, 255.0)
    fg = np.where(alpha[..., None] > 1e-6, fg, 0.0)

    rgba = np.dstack([fg.astype(np.uint8), alpha.astype(np.uint8)])
    return Image.fromarray(rgba, mode="RGBA")


# ---------- Ken Burns 动效（静态图片段缓推/横移，KEN_BURNS / KEN_BURNS_STRENGTH） ----------

_KEN_BURNS_STRENGTH_MAX = 0.5  # 幅度上限保护，过大易放大糊边


def _ken_burns_strength() -> float:
    """动效幅度：限制在 [0, 0.5]，负值按 0（静止画面）处理"""
    return max(0.0, min(float(config.KEN_BURNS_STRENGTH), _KEN_BURNS_STRENGTH_MAX))


def _ken_burns_effect(index: int) -> str:
    """按段落序号交替选动效：偶数段缓推（zoom），奇数段横移（pan）"""
    return "zoom" if index % 2 == 0 else "pan"


def _image_readable(path: str) -> bool:
    """图片能否被 PIL 完整解码（open + load）。

    半截/损坏的 jpg 只查「文件存在」拦不住：Ken Burns 路径的 Image.open
    与非 Ken Burns 路径的 prepare_base_image 都无兜底，一张断点续跑留下的
    坏图会让整个 create_video 崩溃（音频侧有 5s 兜底，图片侧此前没有，
    见 overnight_reports/A12_resume.md「遗留 1」，B 队复核确认的缺陷）。
    返回 False 时调用方回退黑底帧，单段坏图只影响该段画面。
    """
    try:
        with Image.open(path) as img:
            img.load()
        return True
    except Exception as e:
        print(f"[Video] 配图无法解码 ({path}): {e}")
        return False


def _file_usable(path: str) -> bool:
    """缓存帧/叠加层复用判据：存在、非空、且能被 PIL 完整解码。

    与 A12 的坏图防线同一哲学：中断可能留下写到一半的帧文件，只查
    「存在」会把坏帧当缓存复用，合成阶段才崩、整次重合成白费；
    校验不过就回退该段重渲，单段损失、不拖垮整次合成。
    """
    try:
        if not path or not os.path.isfile(path) or os.path.getsize(path) <= 0:
            return False
        with Image.open(path) as img:
            img.load()
        return True
    except Exception:
        return False


def _ken_burns_background(
    img_path: str,
    width: int,
    height: int,
    duration: float,
    strength: float,
    effect: str,
):
    """静态图 → Ken Burns 动效背景片段（画面全程 ≥ 目标尺寸，铺满不露边）。

    zoom：从原尺寸居中缓推放大到 1+strength（ImageClip.resized(lambda t)）；
    pan：固定放大 1+strength，用余量从左向右缓缓横移（位置随时间滑动）。
    片段需放进 CompositeVideoClip(size=(width, height)) 使用。
    """
    safe_dur = duration if duration > 0 else 0.001  # 防 0 除
    base = Image.open(img_path).convert("RGB")
    # P0-1：底图同样走 cover 等比适配，非同画幅素材在运镜下不变形
    base = fit_image_cover(base, width, height)
    clip = ImageClip(np.asarray(base))

    if effect == "pan":
        slack_x = width * strength
        y_off = -(height * strength) / 2.0
        return (
            clip.resized(1.0 + strength)
            .with_position(lambda t: (-slack_x * min(t / safe_dur, 1.0), y_off))
            .with_duration(duration)
        )

    def scale_at(t: float) -> float:
        return 1.0 + strength * min(t / safe_dur, 1.0)

    def center_pos(t: float) -> tuple[float, float]:
        s = scale_at(t)
        return (-(width * (s - 1.0)) / 2.0, -(height * (s - 1.0)) / 2.0)

    return clip.resized(scale_at).with_position(center_pos).with_duration(duration)


# ---------- 上屏单元（块 → 逐帧；karaoke 按词时间表再切） ----------

def _karaoke_highlight_segments(timings: list[tuple[str, float, float]],
                                chunk_dur: float) -> list[tuple[float, float, int]]:
    """把一块的词时间表切成「高亮集合变化点」区间 [(开始, 结束, 高亮词序号)]。

    高亮集合 = 已唱+当前（token.start ≤ t），只在某词开口时刻变化；
    区间无缝铺满 [0, chunk_dur]：开唱前静音段序号 -1（全白），末词之后
    停在末词（pad 进块窗口的尾部静音不闪断）。相邻变化点过近（<1ms）
    时合并，防病态时间片切出微秒级片段。
    """
    if chunk_dur <= 0:
        return []
    points = {0.0, chunk_dur}
    for _tok, start, _end in timings:
        if 0.0 < start < chunk_dur:
            points.add(start)
    ordered = sorted(points)
    segments: list[tuple[float, float, int]] = []
    prev = ordered[0]
    for p in ordered[1:]:
        if p - prev < 1e-3:
            continue
        segments.append((prev, p, subtitle_mod.karaoke_active_index(timings, prev)))
        prev = p
    if not segments:
        return [(0.0, chunk_dur, subtitle_mod.karaoke_active_index(timings, 0.0))]
    if segments[-1][1] < chunk_dur:
        # 末尾变化点因过近被并入：末段延长钉住块尾，保证无缝铺满
        a, _b, active = segments[-1]
        segments[-1] = (a, chunk_dur, active)
    return segments


def _segment_display_units(
    chunks: list[tuple[str, float]],
    karaoke: "list[list[tuple[str, float, float]]] | None",
) -> list[tuple[str, float, "int | None", str]]:
    """段落切块 → 上屏单元 [(文本, 时长, 高亮词序号, 帧名后缀)]。

    非 karaoke 样式（karaoke=None）：每块一条、序号 None、后缀 ""——
    单元与旧版块级布局一一对应，帧名逐字节兼容（缓存互通）。
    karaoke：每块按高亮集合变化点再切成逐词单元——文本仍是整句
    （整句常驻、当前词变色），序号 -1=开唱前全白 / ≥0=已唱到该词，
    后缀 _kKK 与块级帧名区分（同一目录多样式缓存互不覆盖）。
    """
    units: list[tuple[str, float, "int | None", str]] = []
    for j, (chunk_text, chunk_dur) in enumerate(chunks):
        if karaoke is None:
            units.append((chunk_text, chunk_dur, None, ""))
            continue
        timings = karaoke[j] if j < len(karaoke) else []
        segments = _karaoke_highlight_segments(timings, chunk_dur)
        if not segments:
            units.append((chunk_text, chunk_dur, None, ""))
            continue
        for k, (a, b, active) in enumerate(segments):
            units.append((chunk_text, b - a, active, f"_k{k:02d}"))
    return units


def _trim_units_to_window(
    units: list[tuple[str, float, "int | None", str]],
    clear_tail: float,
) -> list[tuple[str, float, "int | None", str]]:
    """截短上屏单元，让字幕在段尾 clear_tail 秒内不再出现。

    交叉淡化重叠区窗口化（与 generate_srt 的 SRT 重叠区窗口化同语义，
    见 subtitles.py 同名逻辑）：重叠区里下一段正带着自己的首句字幕淡入，
    前段字幕若挂屏到段尾，两条字幕会在画面同一几何位置叠画成两层。
    从最后一个单元往前扣除 clear_tail，扣空的单元丢弃；clear_tail<=0
    原样返回。只动时长不动文本/高亮序号，帧内容与缓存帧完全一致。
    """
    if clear_tail <= 1e-6:
        return units
    trimmed = [list(u) for u in units]
    remaining = float(clear_tail)
    for i in range(len(trimmed) - 1, -1, -1):
        if remaining <= 1e-6:
            break
        if trimmed[i][1] - remaining >= 0.05:
            trimmed[i][1] -= remaining
            remaining = 0.0
        else:
            remaining -= trimmed[i][1]
            trimmed[i][1] = 0.0
    return [(t, d, a, s) for t, d, a, s in trimmed if d > 1e-6]


def _segment_overlays(
    units: list[tuple[str, float, "int | None", str]],
    index: int,
    proj_dir: str,
    width: int,
    height: int,
    reuse_cached: bool = False,
    tag_suffix: str = "",
) -> list[ImageClip]:
    """上屏单元 → 透明字幕叠加层列表（Ken Burns 动效背景 / 本地视频背景共用）。

    单元 = 块（plain/card，每句一层）或 karaoke 的逐词帧（每词一层，文本为
    整句、按高亮序号上色）。叠加层 PNG 按单元序号落盘项目目录；reuse_cached
    时优先复用已落盘的叠加层（叠加层内容只依赖字幕文本与样式、不依赖底图，
    _file_usable 拦坏文件），损坏/缺失回退重渲。单句绘制失败只跳过该句，
    不拖垮整段。返回的叠加层按单元顺序在时间轴上无缝铺开。
    """
    overlays = []
    start = 0.0
    for j, (unit_text, unit_dur, active, suffix) in enumerate(units):
        overlay = None
        overlay_path = os.path.join(
            proj_dir, f"overlay_{index:03d}{tag_suffix}_{j:02d}{suffix}.png")
        if reuse_cached and _file_usable(overlay_path):
            try:
                with Image.open(overlay_path) as im:
                    overlay = im.convert("RGBA")
            except Exception as e:
                print(f"[Video] 叠加层缓存读取失败，重新渲染 ({overlay_path}): {e}")
                overlay = None
        if overlay is None:
            try:
                if active is None:
                    overlay = _subtitle_overlay_rgba(unit_text, width, height)
                else:
                    overlay = _subtitle_overlay_rgba(
                        unit_text, width, height, active_word=active)
                overlay.save(overlay_path)
            except Exception as e:
                print(f"[Video] 字幕绘制失败，该句无叠加字幕: {e}")
                start += unit_dur
                continue
        overlays.append(
            ImageClip(np.asarray(overlay))
            .with_start(start)
            .with_duration(unit_dur)
        )
        start += unit_dur
    return overlays


def _ken_burns_segment_clip(
    img_path: str,
    units: list[tuple[str, float, "int | None", str]],
    duration: float,
    index: int,
    proj_dir: str,
    width: int,
    height: int,
    reuse_cached: bool = False,
    tag_suffix: str = "",
):
    """Ken Burns 版段落片段：动效背景 + 逐上屏单元静止字幕叠加层。

    字幕不再烘焙进整帧（否则每句切换会打断缩放连续性、文字还会随画面放大），
    而是转成透明叠加层按单元上屏：背景全程连续缓推/横移，字幕保持静止。

    tag_suffix 为多画幅帧名后缀（如 "_16x9"）：多画幅批量渲染共用同一项目
    目录，不同画幅的缓存帧必须分开命名，否则互相覆盖后换图重合成会复用到
    错误画幅的帧。
    """
    strength = _ken_burns_strength()
    effect = _ken_burns_effect(index)
    bg = _ken_burns_background(img_path, width, height, duration, strength, effect)

    overlays = _segment_overlays(units, index, proj_dir, width, height,
                                 reuse_cached=reuse_cached, tag_suffix=tag_suffix)

    # 尾块字幕可能因 0.05s 下限保护略超段落时长（见 subtitles.build_chunk_durations），
    # 背景一并延长到叠加层结束，避免末句被裁掉
    total = max(duration, sum(u[1] for u in units))
    bg = bg.with_duration(total)
    return CompositeVideoClip([bg] + overlays, size=(width, height)).with_duration(total)


def _video_background(video_path: str, width: int, height: int, duration: float):
    """用户自备视频片段 → 段落背景：截取到段时长（不够则循环），
    cover 等比适配铺满居中裁切，原声一律静音（旁白为主，配音由调用方统一挂载）。

    视频自带运动，不做 Ken Burns。解码/适配任何一步失败返回 None，
    调用方回退该段的图片路径（本地素材分配时视频段也照常配了图）。
    返回的 clip 时长恒等于 duration、尺寸恒等于 width×height。
    """
    try:
        src = VideoFileClip(video_path).without_audio()
    except Exception as e:
        print(f"[Video] 本地视频无法解码，该段回退配图 ({video_path}): {e}")
        return None
    try:
        if not src.duration or src.duration <= 0:
            raise ValueError("视频无有效时长")
        if src.duration < duration:
            src = src.with_effects([vfx.Loop(duration=duration)])
        src = src.subclipped(0, duration)
        # cover 等比适配（与 images.fit_image_cover 同一数学，这版作用于视频帧尺寸）
        w, h = src.size
        if w <= 0 or h <= 0:
            raise ValueError(f"无效视频尺寸 {w}×{h}")
        scale = max(width / w, height / h)
        nw, nh = max(width, round(w * scale)), max(height, round(h * scale))
        src = src.resized((nw, nh)).cropped(
            x_center=nw / 2, y_center=nh / 2, width=width, height=height)
        return src.with_position(("center", "center")).with_duration(duration)
    except Exception as e:
        print(f"[Video] 本地视频适配失败，该段回退配图 ({video_path}): {e}")
        try:
            src.close()
        except Exception:
            pass
        return None


def _prepare_segment_image(img_path: str, index: int, proj_dir: str, width: int, height: int) -> str:
    """底图准备：缺失或损坏（半截文件无法解码）一律回退黑底帧。

    从 _build_segment_clip 抽出（换图局部重渲也要走同一兜底），保证
    单段坏图只影响该段画面，不拖垮整次合成。
    """
    if img_path and os.path.exists(img_path) and not _image_readable(img_path):
        print(f"[Video] 第 {index+1} 段配图文件损坏，该段回退黑底帧: {img_path}")
        img_path = ""
    if not img_path or not os.path.exists(img_path):
        black = Image.new("RGB", (width, height), "black")
        img_path = os.path.join(proj_dir, f"_black_{index:03d}.jpg")
        black.save(img_path)
    return img_path


def _chunks_meta_path(index: int, tag_suffix: str, proj_dir: str) -> str:
    """段内字幕切块布局的落盘快照（_chunks_XXX[_tag].json）。

    缓存帧复用前必须证明「当时渲染这些帧用的切块」与本次重算的切块完全一致
    （文本/时长/词级时间片任一变化都会改变布局，块数相同也会错位），
    布局对不上就整段重渲，宁可慢不可错。
    """
    return os.path.join(proj_dir, f"_chunks_{index:03d}{tag_suffix}.json")


def _chunks_layout(chunks: list[tuple[str, float]]) -> list[list]:
    return [[str(t), float(d)] for t, d in chunks]


def _karaoke_layout(karaoke) -> "list[list[list]] | None":
    """词时间表的落盘序列化形态；karaoke 关闭（None）时返回 None 不写入快照"""
    if karaoke is None:
        return None
    return [[[str(tok), float(s), float(e)] for tok, s, e in chunk]
            for chunk in karaoke]


def _cached_chunks_match(index: int, tag_suffix: str, proj_dir: str,
                         chunks: list[tuple[str, float]],
                         karaoke=None,
                         style: str = "",
                         size: tuple[int, int] = (0, 0)) -> bool:
    try:
        with open(_chunks_meta_path(index, tag_suffix, proj_dir), "r", encoding="utf-8") as f:
            saved = json.load(f)
        if saved.get("chunks") != _chunks_layout(chunks):
            return False
        # 样式/分辨率变了帧内容就变（plain/card 共用帧名，换样式不清缓存会把
        # 旧样式帧原样复用，成片前后段字幕样式混排），必须一并比对；
        # 旧版快照无这两个字段 → 不匹配 → 整段重渲（宁可慢不可错）
        if saved.get("style", "") != (style or config.SUBTITLE_STYLE):
            return False
        if saved.get("size", [0, 0]) != [int(size[0]), int(size[1])]:
            return False
        if config.SUBTITLE_STYLE == "karaoke":
            # karaoke 帧按词时间表逐词渲染，词表变了帧内容就变了——必须连词表
            # 一起比对；旧版快照无 karaoke 字段 → 不匹配 → 整段重渲（宁可慢不可错）
            return saved.get("karaoke") == _karaoke_layout(karaoke)
        return True
    except Exception:
        return False


def _save_chunks_meta(index: int, tag_suffix: str, proj_dir: str,
                      chunks: list[tuple[str, float]], karaoke=None,
                      style: str = "",
                      size: tuple[int, int] = (0, 0)) -> None:
    try:
        payload: dict = {
            "chunks": _chunks_layout(chunks),
            "style": style or config.SUBTITLE_STYLE,
            "size": [int(size[0]), int(size[1])],
        }
        if config.SUBTITLE_STYLE == "karaoke" and karaoke is not None:
            payload["karaoke"] = _karaoke_layout(karaoke)
        with open(_chunks_meta_path(index, tag_suffix, proj_dir), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except Exception as e:
        print(f"[Video] 字幕切块快照写入失败（只影响缓存帧复用，不影响合成）: {e}")


def _build_segment_clip(
    seg: dict,
    index: int,
    proj_dir: str,
    width: int,
    height: int,
    reuse_cached: bool = False,
    frame_tag: str = "",
    tail_clearance: float = 0.0,
):
    """为一个段落构建视频片段：底图 + 按句切块的字幕 + 配音

    config.KEN_BURNS 开启（默认）时走 Ken Burns 路径：
    底图带缓推/横移动效，字幕为逐句透明叠加层（静止不随画面缩放）；
    关闭时沿用旧路径：字幕烘焙进整帧后按句拼接。

    SUBTITLE_STYLE=karaoke（D5）时，块级时间窗内再用词级时间表
    （subtitles.karaoke_chunk_timings：seg["words"] 真实投影 / 字符比例兜底）
    把每块切成逐词上屏单元，两条合成路径都按单元出帧。

    tail_clearance>0（段间交叉淡化，create_video 传入本段与下一段的重叠秒数）
    时做重叠区窗口化：本段字幕在段尾提前 tail_clearance 秒收起，重叠区里只有
    下一段淡入的字幕，两层字幕不再同框（Ken Burns 路径截短末尾叠加层；烘焙
    路径补一帧无字幕底图垫住时长，段总时长与配音时间轴不变）。

    reuse_cached=True（换图局部重渲，D4）时复用本段已落盘的字幕帧/叠加层：
    前提是切块布局快照一致且全部块文件可解码（_file_usable），任一不满足
    回退整段重渲。烘焙路径复用成功时连底图都不必打开；Ken Burns 路径的
    动效背景始终由底图重建（只有叠加层可复用）。frame_tag 为多画幅帧名
    后缀，防不同画幅的缓存帧互相覆盖（空串 = 单画幅旧命名，完全兼容）。
    """
    duration = float(seg.get("duration") or 5.0)
    img_path = seg.get("image_path")
    audio_path = seg.get("audio_path")
    text = seg.get("text", "")

    # 按句切块字幕：优先用真实时间片（seg["words"]，D2 TTS 通道 / D3 本地
    # 对齐通道产出）按发音边界取时长，拿不到时回退字符比例估算；
    # 再把块时长补齐到段时长（真实路径尾部静音会短于音频，画面须兜住整段），
    # 避免段落片段比音频短导致成片时间轴漂移（无切块能力时整段一条）
    chunks = (subtitle_mod.build_chunk_durations(text, duration, words=seg.get("words"))
              or [(text, duration)])
    chunks = subtitle_mod.pad_chunk_durations(chunks, duration)
    # karaoke：块级窗口内再切词级高亮时间表（真实时间片投影 / 字符比例兜底，
    # 与块级同一套护栏，两边同时可用或同时回退，时间轴永不矛盾）
    karaoke = (subtitle_mod.karaoke_chunk_timings(chunks, words=seg.get("words"))
               if config.SUBTITLE_STYLE == "karaoke" else None)
    units = _segment_display_units(chunks, karaoke)
    # 交叉淡化重叠区窗口化：只截单元时长，帧内容不变，缓存快照仍按完整布局比对
    units = _trim_units_to_window(units, tail_clearance)
    tag_suffix = f"_{frame_tag}" if frame_tag else ""
    layout_ok = (reuse_cached
                 and _cached_chunks_match(index, tag_suffix, proj_dir, chunks, karaoke,
                                          size=(width, height)))

    # 本地视频素材（seg["video_path"]，images.assign_local_media 记入）：
    # 解码/适配成功就用视频当背景（自带运动，不做 Ken Burns），失败自动
    # 落回图片路径——视频段在素材分配时也照常配了图，兜底链完整
    bg_video = None
    if seg.get("video_path"):
        if os.path.isfile(seg["video_path"]):
            bg_video = _video_background(seg["video_path"], width, height, duration)
        else:
            print(f"[Video] 第 {index+1} 段本地视频文件不存在，回退配图: {seg['video_path']}")

    if bg_video is not None:
        print(f"[Video] 第 {index+1} 段使用本地视频画面: {os.path.basename(seg['video_path'])}")
        overlays = _segment_overlays(units, index, proj_dir, width, height,
                                     reuse_cached=layout_ok, tag_suffix=tag_suffix)
        seg_clip = CompositeVideoClip(
            [bg_video] + overlays, size=(width, height)).with_duration(duration)
        if not layout_ok:
            _save_chunks_meta(index, tag_suffix, proj_dir, chunks, karaoke,
                              size=(width, height))
    elif config.KEN_BURNS:
        img_path = _prepare_segment_image(img_path, index, proj_dir, width, height)
        seg_clip = _ken_burns_segment_clip(
            img_path, units, duration, index, proj_dir, width, height,
            reuse_cached=layout_ok, tag_suffix=tag_suffix,
        )
        if not layout_ok:
            _save_chunks_meta(index, tag_suffix, proj_dir, chunks, karaoke,
                              size=(width, height))
    else:
        def frame_file(j: int, suffix: str = "") -> str:
            return os.path.join(proj_dir, f"frame_{index:03d}{tag_suffix}_{j:02d}{suffix}.jpg")

        cached_ok = layout_ok and all(
            _file_usable(frame_file(j, suffix))
            for j, (_t, _d, _a, suffix) in enumerate(units)
        )
        base = None
        if cached_ok:
            print(f"[Video] 第 {index+1} 段复用已渲染字幕帧（{len(units)} 帧，"
                  f"{os.path.basename(frame_file(0))} 等）")
        else:
            img_path = _prepare_segment_image(img_path, index, proj_dir, width, height)
            _save_chunks_meta(index, tag_suffix, proj_dir, chunks, karaoke,
                              size=(width, height))
            # 同段只 open/resize 一次底图，各块在副本上绘制
            # （原先每块都走 _draw_subtitle_on_image 重复打开+缩放同一底图）
            base = prepare_base_image(img_path, width, height)

        chunk_clips = []
        for j, (unit_text, unit_dur, active, suffix) in enumerate(units):
            frame_path = frame_file(j, suffix)
            if not cached_ok:
                try:
                    if active is None:
                        render_subtitle_frame(base, unit_text, frame_path, width, height)
                    else:
                        render_subtitle_frame(base, unit_text, frame_path, width, height,
                                              active_word=active)
                except Exception as e:
                    print(f"[Video] 字幕绘制失败，使用原图: {e}")
                    frame_path = img_path
            clip = ImageClip(frame_path).with_duration(unit_dur)
            chunk_clips.append(clip)

        # 交叉淡化重叠区：字幕提前收起后补一帧无字幕底图，把段时长垫回原长
        # ——烘焙路径的段片段若真的变短，重叠区里正在淡出的配音会被一起裁掉，
        # 时间轴漂移。垫帧只在整段重渲时重写（换图后旧底图垫帧必须失效），
        # 纯复用时直接沿用旧文件
        if tail_clearance > 1e-6:
            tail_frame = os.path.join(
                proj_dir, f"frame_{index:03d}{tag_suffix}_tail.jpg")
            if not (cached_ok and _file_usable(tail_frame)):
                if base is None:
                    base = prepare_base_image(
                        _prepare_segment_image(img_path, index, proj_dir, width, height),
                        width, height)
                base.save(tail_frame)
            chunk_clips.append(
                ImageClip(tail_frame).with_duration(tail_clearance))

        seg_clip = concatenate_videoclips(chunk_clips, method="compose")

    if audio_path and os.path.exists(audio_path):
        try:
            audio = AudioFileClip(audio_path)
            if audio.duration and duration and audio.duration > duration + 0.01:
                audio = audio.subclipped(0, duration)
            seg_clip = seg_clip.with_audio(audio)
        except Exception as e:
            print(f"[Video] 第 {index+1} 段音频挂载失败，该段静音: {e}")

    return seg_clip


# ---------- 背景音乐（BGM_VOLUME/BGM_TEMP 合成垫乐 + BGM_DIR 用户自备曲库） ----------

_BGM_SAMPLE_RATE = 44100  # 垫乐合成采样率（写 wav，混音时 moviepy 自动适配输出）

# BGM_TEMP 风格参数表：每档一组「温度感」不同的分层配方。
# 配方 = 低通噪声层（空气感底） + 三层低频正弦（协和音程：根音 + 纯五度 + 八度/微失谐），
# 每层经极慢 LFO 音量起伏（volume 表达式，呼吸感）→ amix 合成 → 主低通滤掉毛刺（防刺耳）。
# 层增益合计按峰值 ~-10dBFS 校准（实测见 overnight_reports/A4_bgm.md），混音时再乘 BGM_VOLUME 压低。
# lfo 字段 = (周期秒, 相位弧度)：三层周期/相位错开，避免叠加成规律性泵动。
_BGM_STYLES: dict[str, dict] = {
    "warm": {
        "noise_color": "brown", "noise_lowpass": 320, "noise_gain": 0.85, "noise_seed": 2026,
        "chord": [110.0, 164.81, 220.5],      # A2 + E3 + 微失谐 A3（较纯八度 220Hz 高约 3.9 音分，拓宽八度避免纯音死板；纯音无谐波，不产生拍频）
        "chord_gains": [0.55, 0.35, 0.22],
        "lfo": [(9.0, 0.0), (11.0, 2.1), (13.0, 4.2)],
        "master_lowpass": 1200, "master_gain": 0.95,
    },
    "cool": {
        "noise_color": "pink", "noise_lowpass": 500, "noise_gain": 0.70, "noise_seed": 2027,
        "chord": [146.83, 220.0, 293.66],     # D3 + A3 + D4（稍高稍亮）
        "chord_gains": [0.50, 0.32, 0.20],
        "lfo": [(8.0, 0.0), (10.0, 2.1), (12.0, 4.2)],
        "master_lowpass": 2200, "master_gain": 0.90,
    },
    "deep": {
        "noise_color": "brown", "noise_lowpass": 180, "noise_gain": 0.95, "noise_seed": 2028,
        "chord": [55.0, 82.5, 110.4],         # A1 + E2 + 微失谐 A2（更低更暗）
        "chord_gains": [0.60, 0.35, 0.20],
        "lfo": [(10.0, 0.0), (12.0, 2.1), (14.0, 4.2)],
        "master_lowpass": 700, "master_gain": 0.95,
    },
}


def _bgm_volume() -> float:
    """BGM_VOLUME 钳制到 [0, 1]：<=0 视为关闭，>1 压回 1（防止垫乐盖过配音）"""
    return max(0.0, min(float(config.BGM_VOLUME), 1.0))


# 用户上传 BGM 的默认混音音量：全局 BGM_VOLUME=0（默认关）时上传 BGM 仍按此音量混入
# ——上传即代表想用，不能被全局开关静默忽略；BGM_VOLUME>0 时仍跟随全局值
_USER_BGM_DEFAULT_VOLUME = 0.25


# ---------- 用户自备曲库（BGM_DIR，任务 D7）：随机选曲、当日不重复 ----------

_BGM_LIB_EXTENSIONS = (".mp3", ".wav")  # 曲库支持的音频格式（按扩展名，大小写不敏感）
_BGM_USED_NAME = "_bgm_used.json"       # 项目目录内的选曲记忆文件


def _bgm_today() -> str:
    """当天日期（ISO 格式）。独立成函数便于测试模拟跨天重选。"""
    return datetime.date.today().isoformat()


def bgm_library_tracks() -> list[str]:
    """扫描 config.BGM_DIR 曲库目录，返回全部 .mp3/.wav 曲目（绝对路径、排序稳定）。

    递归子目录（真实曲库常按歌手/专辑分文件夹）；BGM_DIR 未配置、不是目录、
    或没有可用曲目时返回空列表，调用方据此回退合成垫乐。
    """
    root = (config.BGM_DIR or "").strip()
    if not root or not os.path.isdir(root):
        return []
    tracks: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()  # 目录与文件名都排序：扫描结果可复现，随机选曲池稳定
        for name in sorted(filenames):
            if name.lower().endswith(_BGM_LIB_EXTENSIONS):
                tracks.append(os.path.join(dirpath, name))
    return sorted(tracks)  # 跨子目录全局排序（os.walk 按目录分块，块内有序≠全局有序）


def _load_bgm_used(proj_dir: str) -> dict:
    """读项目目录里的选曲记忆文件，返回 {"date", "picked", "used"}。

    文件缺失/损坏/结构不符一律视为空记忆（重新随机选曲，宁可可能重复，
    也不让记忆问题阻塞出片）。
    """
    empty = {"date": "", "picked": "", "used": []}
    try:
        with open(os.path.join(proj_dir, _BGM_USED_NAME), "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return empty
        return {
            "date": str(data.get("date") or ""),
            "picked": str(data.get("picked") or ""),
            "used": [str(p) for p in data["used"]] if isinstance(data.get("used"), list) else [],
        }
    except (OSError, ValueError):
        return empty


def _save_bgm_used(proj_dir: str, date: str, picked: str, used: list[str]) -> None:
    """原子写选曲记忆（先写 .tmp 再 os.replace，与 segments.json 同法）。

    写盘失败只损失「当日固定/不重复」保证（下次可能重新随机），不影响出片。
    """
    try:
        path = os.path.join(proj_dir, _BGM_USED_NAME)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"date": date, "picked": picked, "used": used}, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        print(f"[Video] BGM 曲库记忆文件写入失败（忽略，可能重复选曲）: {e}")


def pick_bgm_from_library(proj_dir: str) -> str:
    """从 BGM_DIR 曲库为该项目选一首曲子；无可用曲库返回 ""。

    选择规则（记忆文件 项目目录/_bgm_used.json）：
    - 当日已选（date == 今天且 picked 仍在曲库）→ 直接复用，不再随机：
      同一项目一天内多次合成（多画幅批量渲染、web 换图/改词局部重渲、
      CLI 断点续跑）BGM 保持一致——同一支视频不该换配乐；当天内由此
      保证「不重复选曲」；
    - 跨天重选 → 在「历史已用（used）之外的曲目」里随机；历史把曲库
      用尽则清空历史开始新一轮，保证永远有曲可选。跨天也避开历史是
      「当日不重复」的加强：同一项目听到的曲子直到整库轮完一圈才轮回。
    """
    tracks = bgm_library_tracks()
    if not tracks:
        return ""
    today = _bgm_today()
    mem = _load_bgm_used(proj_dir)
    if mem["date"] == today and mem["picked"] in tracks:
        return mem["picked"]
    used = mem["used"]  # 历史已用跨天保留（整库轮完一圈才清空重来）
    fresh = [t for t in tracks if t not in set(used)]
    if not fresh:
        fresh, used = tracks, []  # 整库用尽：开始新一轮
    pick = random.choice(fresh)
    _save_bgm_used(proj_dir, today, pick, used + [pick])
    return pick


def _bgm_style(style: str = "") -> dict:
    """取指定风格（默认读 config.BGM_TEMP）的分层参数；未知值回退 warm（与 config 兜底一致）"""
    key = (style or config.BGM_TEMP).strip().lower()
    return _BGM_STYLES.get(key) or _BGM_STYLES["warm"]


def _ffmpeg_exe() -> str:
    """imageio_ffmpeg 自带的 ffmpeg 可执行文件（离线合成垫乐，不依赖系统安装 ffmpeg）"""
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def _bgm_lfo_expr(period: float, phase: float) -> str:
    """单层正弦垫乐的慢呼吸音量表达式：[0.1, 1.0] 区间内起伏（均值 0.55、深度 0.45）"""
    return f"0.55+0.45*sin(2*PI*t/{period}+{phase})"


def _bgm_filter_complex(st: dict) -> str:
    """构造垫乐合成滤镜图：噪声层低通 + 各正弦层慢 LFO → amix → 主低通 → 总增益"""
    parts = [
        f"[0:a]lowpass=f={st['noise_lowpass']},volume={st['noise_gain']}[l0]"
    ]
    for i, (freq, gain) in enumerate(zip(st["chord"], st["chord_gains"]), start=1):
        period, phase = st["lfo"][i - 1]
        parts.append(
            f"[{i}:a]volume=volume={_bgm_lfo_expr(period, phase)}:eval=frame,"
            f"volume={gain}[l{i}]"
        )
    inputs = "".join(f"[l{i}]" for i in range(len(st["chord"]) + 1))
    parts.append(
        f"{inputs}amix=inputs={len(st['chord']) + 1}:normalize=0,"
        f"lowpass=f={st['master_lowpass']},volume={st['master_gain']}[out]"
    )
    return ";".join(parts)


def generate_bgm(output_path: str, duration: float, style: str = "") -> str:
    """用 imageio_ffmpeg 自带 ffmpeg 离线合成柔和氛围垫乐 wav（anoisesrc/sine 分层 + 低通）。

    直接按成片时长一次生成整段垫乐（sine/anoisesrc 是参数化合成，速度远高于实时），
    效果等同「循环铺满全片」且彻底避免短样本循环接缝处的爆音；若产物仍短于成片
    （极端情况），混音侧再用 moviepy AudioLoop 循环补齐。合成失败抛异常，
    由调用方降级为无垫乐继续出片，不中断生成。
    """
    import subprocess

    st = _bgm_style(style)
    dur = max(float(duration), 0.1)
    cmd = [_ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error"]
    cmd += [
        "-f", "lavfi",
        "-i", f"anoisesrc=color={st['noise_color']}:amplitude=0.7:seed={st['noise_seed']}"
              f":duration={dur:.3f}:sample_rate={_BGM_SAMPLE_RATE}",
    ]
    for freq in st["chord"]:
        cmd += [
            "-f", "lavfi",
            "-i", f"sine=frequency={freq}:duration={dur:.3f}:sample_rate={_BGM_SAMPLE_RATE}",
        ]
    cmd += [
        "-filter_complex", _bgm_filter_complex(st),
        "-map", "[out]",
        "-ac", "2",
        "-c:a", "pcm_s16le",
        output_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 合成垫乐失败: {proc.stderr.strip()[:400]}")
    if not os.path.exists(output_path):
        raise RuntimeError(f"ffmpeg 合成垫乐未产出文件: {output_path}")
    return output_path


def _mix_bgm(final_video, proj_dir: str, user_bgm: str = "") -> tuple[object, object]:
    """把背景音乐混入成片音轨（CompositeAudioClip），配音优先。

    音源优先级：用户上传 BGM > BGM_DIR 曲库 > 合成垫乐——
    - 用户上传的 BGM（user_bgm，web 上传后拷入项目目录）存在时最高优先，
      不再看曲库与合成垫乐（config 优先已上传素材）；全局 BGM_VOLUME=0
      （默认关）时按 _USER_BGM_DEFAULT_VOLUME 混入——上传即代表想用，
      BGM_VOLUME>0 时仍跟随全局值
    - 无用户上传时若 config.BGM_DIR 曲库（递归扫描 .mp3/.wav）有可用曲目，
      用 pick_bgm_from_library 选曲（同一项目当日固定一首、跨天避开历史
      已用，记忆文件 项目目录/_bgm_used.json）；音量语义与上传相同
      （BGM_VOLUME=0 时按 _USER_BGM_DEFAULT_VOLUME 混入——配置曲库即代表
      想用）；曲库文件解码失败回退合成垫乐
    - 无可用曲库时行为不变：BGM_VOLUME>0 才用 imageio_ffmpeg 离线合成
      柔和垫乐；因曲库而自动开启时合成垫乐同样按默认音量混入（不让
      回退产物变成无声轨）
    - 音源先经 MultiplyVolume(vol) 整体压低（推荐 0.15~0.35，垫乐音量低）
    - 首尾自适应淡入淡出（≤1.5s/2.0s，短视频按时长比例缩短），避免突入突切
      （曲库曲目与合成垫乐走同一段淡入淡出管线，天然共存）
    - 音源文件比成片长截到成片时长，比成片短用 AudioLoop 循环铺满
      （垫乐正常路径按成片时长直接生成，不会触发）
    - 任何一步失败都降级为原音轨继续出片；返回 (混音后视频, 音源clip或None)
    """
    vol = _bgm_volume()
    has_user_bgm = bool(user_bgm) and os.path.isfile(user_bgm)
    lib_track = "" if has_user_bgm else pick_bgm_from_library(proj_dir)
    if vol <= 0 and not has_user_bgm and not lib_track:
        return final_video, None

    duration = float(final_video.duration or 0.0)
    if duration <= 0:
        return final_video, None

    bgm_path = os.path.join(proj_dir, "_bgm_pad.wav")
    bgm = None
    src = ""
    try:
        if has_user_bgm:
            bgm = AudioFileClip(user_bgm)
            if vol <= 0:
                vol = _USER_BGM_DEFAULT_VOLUME
            src = f"用户上传 {os.path.basename(user_bgm)}"
            print(f"[Video] 使用用户上传的背景音乐: {user_bgm}（音量 {vol}）")
        else:
            if lib_track:
                try:
                    bgm = AudioFileClip(lib_track)
                    if vol <= 0:
                        vol = _USER_BGM_DEFAULT_VOLUME
                    src = f"BGM_DIR 曲库 {os.path.basename(lib_track)}"
                    print(f"[Video] 使用 BGM_DIR 曲库选曲: {lib_track}（音量 {vol}）")
                except Exception as e:
                    # 坏文件只损失这一首：回退合成垫乐，不放弃整个 BGM
                    print(f"[Video] 曲库文件无法解码，回退合成垫乐 ({lib_track}): {e}")
                    bgm = None
            if bgm is None:
                generate_bgm(bgm_path, duration)
                bgm = AudioFileClip(bgm_path)
                if vol <= 0:
                    vol = _USER_BGM_DEFAULT_VOLUME  # 曲库触发的自动开启延续到合成垫乐
                src = f"风格={config.BGM_TEMP}"
        if bgm.duration and bgm.duration > duration + 0.01:
            bgm = bgm.subclipped(0, duration)
        elif bgm.duration and bgm.duration < duration - 0.01:
            bgm = bgm.with_effects([afx.AudioLoop(duration=duration)])
        bgm = bgm.with_effects([
            afx.MultiplyVolume(vol),
            afx.AudioFadeIn(min(1.5, duration / 4)),
            afx.AudioFadeOut(min(2.0, duration / 4)),
        ])
        if final_video.audio is not None:
            mixed = CompositeAudioClip([final_video.audio, bgm])
        else:
            mixed = bgm
        print(f"[Video] 背景音乐已混入（音量={vol}，{src}）")
        return final_video.with_audio(mixed), bgm
    except Exception as e:
        print(f"[Video] 背景音乐生成/混音失败，跳过 BGM 继续合成: {e}")
        return final_video, None


def create_video(
    segments: list[dict],
    output_path: str,
    width: int = 0,
    height: int = 0,
    project_dir: str = "",
    topic: str = "",
    bgm_path: str = "",
    reuse_frames: bool = False,
    rerender_segments: "set[int] | None" = None,
    frame_tag: str = "",
) -> str:
    """将图片、音频、字幕合成为视频，所有中间文件直接放在项目目录

    project_dir 为空时回退全局项目目录；Web 多任务并发时应显式传入各自目录。
    topic 用于片头卡大字（INTRO_TEXT 配置优先）。
    INTRO_OUTRO 开启（默认）时正片前插 2.5s 片头卡、末尾插 2s 片尾卡，
    正片时间轴整体后移 INTRO_CARD_DURATION 秒——SRT 需用 intro_offset() 同步。
    CROSSFADE_SECONDS>0 时相邻故事片段交叉淡化（画面蒙版淡入 + 音频同步淡入淡出），
    成片时长 = 卡片 + 各段之和 - 段间重叠；SRT 需用 generate_srt(crossfade=...) 同步。
    交叉淡化只作用于故事片段之间，卡片与正片之间仍是硬切，保证 intro_offset()
    的偏移约定不变。
    bgm_path 非空且文件存在时优先作为背景音乐音源（web 用户上传的 BGM，
    config 优先已上传素材）；为空时依次回退：config.BGM_DIR 曲库（递归扫描
    .mp3/.wav，pick_bgm_from_library 随机选曲、同一项目当日固定一首不重复，
    记忆文件 项目目录/_bgm_used.json；BGM_VOLUME=0 时按默认音量自动开启）→
    BGM_VOLUME>0（默认 0=关）才用 imageio_ffmpeg 自带 ffmpeg 离线合成柔和
    氛围垫乐（BGM_TEMP 选风格）。
    CompositeAudioClip 混入音轨铺满全片（含片头片尾卡），音量低且首尾淡入淡出，
    配音始终在前；合成/混音失败自动跳过 BGM 不中断。

    换图局部重渲（D4，web /api/select 用）：
    reuse_frames=True 时各段优先复用项目目录里上一轮已渲染的字幕帧/叠加层
    （切块布局快照一致 + 文件可解码才复用，否则该段整段重渲，见
    _build_segment_clip 注释），不再逐段重画字幕——换一张配图只重画选中段的
    字幕帧，其余段落直接拿缓存帧重新合成整片；rerender_segments 为强制重渲的
    段索引集合（选中的段），frame_tag 为多画幅缓存帧名后缀（如 "16x9"）。
    默认（False/None/""）行为与旧版逐段重渲完全一致。
    """
    if width <= 0 or height <= 0:
        width, height = config.video_size()
        if config.PREVIEW:
            print(f"[Video] 预览模式: 以 {width}×{height} 快速渲染"
                  f"（PREVIEW=1 / --preview；正式出片请关闭）")
    proj_dir = project_dir or config.get_project_dir()
    os.makedirs(proj_dir, exist_ok=True)
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)

    clips = []
    intro_clip = None
    outro_clip = None
    bgm_clip = None
    try:
        # 段间交叉淡化布局提前算（CROSSFADE_SECONDS>0 且至少两段时启用）：
        # 段构建需要知道自己尾部的重叠秒数，把重叠区里的字幕提前收起
        # （否则前段末句与后段首句在重叠区叠画成两层字幕）。时长口径用
        # segments 的标称时长，与 generate_srt(crossfade=...) 完全一致
        seg_durations = [float(s.get("duration") or 5.0) for s in segments]
        overlaps, seg_starts = _crossfade_layout(seg_durations, crossfade_seconds())
        crossfade_on = bool(overlaps)

        for i, seg in enumerate(segments):
            # rerender_segments 里的段强制重渲（换图后画面已变），其余段允许吃缓存帧
            rerender = bool(rerender_segments and i in rerender_segments)
            clips.append(_build_segment_clip(
                seg, i, proj_dir, width, height,
                reuse_cached=reuse_frames and not rerender,
                frame_tag=frame_tag,
                tail_clearance=(overlaps[i] if i < len(overlaps) else 0.0),
            ))

        if not clips:
            raise ValueError("没有可合成的片段")

        # 片头/片尾卡（INTRO_OUTRO=0/false/off/no 时跳过）
        if config.INTRO_OUTRO:
            intro_text, outro_text = _resolve_card_texts(topic)
            intro_clip = _build_intro_card(width, height, proj_dir, intro_text)
            outro_clip = _build_outro_card(width, height, proj_dir, outro_text)

        if crossfade_on:
            # 后段淡入：画面用蒙版 CrossFadeIn（只在 CompositeVideoClip 中生效），
            # 音频用 AudioFadeIn；前段音频 AudioFadeOut。重叠区两层叠放，
            # 声音经 CompositeAudioClip 混音，实现声画同步的交叉淡化。
            for i, clip in enumerate(clips):
                effects = []
                if i > 0:
                    effects.append(vfx.CrossFadeIn(overlaps[i - 1]))
                    effects.append(afx.AudioFadeIn(overlaps[i - 1]))
                if i < len(clips) - 1:
                    effects.append(afx.AudioFadeOut(overlaps[i]))
                clips[i] = clip.with_effects(effects)

        # 拼接：片头卡 + 正片片段 + 片尾卡
        if crossfade_on:
            # 淡化路径：CompositeVideoClip 按绝对起点摆放各片段（重叠区叠放），
            # 成片时长 = 片头卡 + 各段之和 - 段间重叠 + 片尾卡
            intro_len = INTRO_CARD_DURATION if config.INTRO_OUTRO else 0.0
            positioned = []
            if intro_clip is not None:
                positioned.append(intro_clip.with_start(0.0))
            for i, clip in enumerate(clips):
                positioned.append(clip.with_start(intro_len + seg_starts[i]))
            if outro_clip is not None:
                feature_end = intro_len + seg_starts[-1] + seg_durations[-1]
                positioned.append(outro_clip.with_start(feature_end))
            final_video = CompositeVideoClip(positioned, size=(width, height))
        else:
            ordered = list(clips)
            if intro_clip:
                ordered.insert(0, intro_clip)
            if outro_clip:
                ordered.append(outro_clip)
            final_video = concatenate_videoclips(ordered, method="compose")
        final_video = final_video.with_fps(config.VIDEO_FPS)

        # 背景音乐：用户上传 BGM（bgm_path）优先；否则 BGM_VOLUME>0 时用
        # imageio_ffmpeg 合成柔和垫乐并混入音轨
        # （配音优先：垫乐音量低 + 首尾淡入淡出；失败自动跳过不中断）
        final_video, bgm_clip = _mix_bgm(final_video, proj_dir, user_bgm=bgm_path)

        # 写入文件（临时音频也放项目目录）
        final_video.write_videofile(
            output_path,
            fps=config.VIDEO_FPS,
            codec="libx264",
            audio_codec="aac",
            temp_audiofile=os.path.join(proj_dir, "_temp_audio.m4a"),
            remove_temp=True,
        )
    finally:
        for clip in clips + [
            c for c in (intro_clip, outro_clip, bgm_clip) if c is not None
        ]:
            try:
                clip.close()
            except Exception:
                pass

    return output_path
