"""视频封面生成（Pillow，纯离线）

视频合成完成后自动输出封面 cover.jpg：
- 竖版/横版自适应：不传宽高时跟随 config.video_size()，字号/边距按画幅比例缩放
- 大号主题标题：自动换行，装不下时逐级缩小字号，白色描边保证可读
- 渐变底色：按主题确定性配色（同主题封面风格稳定）；有配图时把第一张
  配图半透明混进渐变底，再叠加垂直半透明遮罩，进一步提升文字可读性
- 底部副标题：生成日期 + 视频时长

环境变量：
  COVER_TITLE=自定义标题文案（覆盖主题作为封面标题）
"""
import datetime
import hashlib
import os

from PIL import Image, ImageDraw, ImageFont

import config
from video import _get_system_font, _wrap_text

# 渐变底色配色（深色系，保证白字可读），按主题哈希确定性选取
_PALETTES = [
    ((24, 42, 104), (104, 54, 148)),   # 深蓝 → 紫
    ((8, 58, 84), (5, 116, 148)),      # 墨绿 → 海蓝
    ((78, 26, 48), (140, 44, 88)),     # 酒红 → 玫红
    ((18, 50, 40), (48, 110, 94)),     # 松绿 → 苔绿
    ((46, 36, 100), (100, 68, 142)),   # 靛蓝 → 藤紫
]


def resolve_title(topic: str) -> str:
    """封面标题文案：env COVER_TITLE 优先，其次视频主题"""
    custom = os.getenv("COVER_TITLE", "").strip()
    return custom or (topic or "").strip() or "未命名视频"


def format_duration(seconds: float) -> str:
    """把秒数格式化成可读文案：65 → '1 分 05 秒'，42 → '42 秒'"""
    s = max(0, int(round(seconds or 0)))
    if s >= 60:
        return f"{s // 60} 分 {s % 60:02d} 秒"
    return f"{s} 秒"


def _gradient_base(width: int, height: int, topic: str) -> Image.Image:
    """按主题生成垂直渐变底色（1px 竖列插值后放大，避免逐像素画全图）"""
    digest = hashlib.md5((topic or "").encode("utf-8")).hexdigest()
    c1, c2 = _PALETTES[int(digest, 16) % len(_PALETTES)]
    column = Image.new("RGB", (1, max(2, height)))
    px = column.load()
    for y in range(column.height):
        t = y / (column.height - 1)
        px[0, y] = tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))
    return column.resize((width, height))


def _cover_crop(img: Image.Image, width: int, height: int) -> Image.Image:
    """等比缩放到铺满画幅后居中裁切"""
    sw, sh = img.size
    scale = max(width / sw, height / sh)
    nw, nh = max(width, round(sw * scale)), max(height, round(sh * scale))
    img = img.resize((nw, nh), Image.Resampling.LANCZOS)
    left, top = (nw - width) // 2, (nh - height) // 2
    return img.crop((left, top, left + width, top + height))


def _scrim(width: int, height: int, top_alpha: int, bottom_alpha: int) -> Image.Image:
    """垂直渐变的半透明黑色遮罩（上浅下深），提升文字可读性"""
    mask = Image.linear_gradient("L").resize((width, height))
    alpha = mask.point(lambda v: top_alpha + (bottom_alpha - top_alpha) * v // 255)
    black = Image.new("L", (width, height), 0)
    return Image.merge("RGBA", (black, black, black.copy(), alpha))


def _background(segments: list[dict], width: int, height: int, topic: str,
                base_image: str = "") -> Image.Image:
    """封面底图：用户上传的底图（base_image，web /api/upload 传入）优先——
    等比铺满画幅居中裁切后全幅使用（后续仍叠半透明遮罩保证文字可读）；
    底图缺失/读取失败时打印告警并回退默认背景：渐变打底，有配图时把第一张图
    半透明混进渐变（55% 渐变 / 45% 图）"""
    if base_image:
        if os.path.isfile(base_image):
            try:
                with Image.open(base_image) as img:
                    return _cover_crop(img.convert("RGB"), width, height)
            except Exception as e:
                print(f"[Cover] 上传底图读取失败，回退默认背景: {e}")
        else:
            print(f"[Cover] 上传底图不存在，回退默认背景: {base_image}")
    base = _gradient_base(width, height, topic)
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        path = seg.get("image_path")
        if not path or not os.path.exists(path):
            continue
        try:
            with Image.open(path) as img:
                photo = _cover_crop(img.convert("RGB"), width, height)
        except Exception as e:
            print(f"[Cover] 配图读取失败，跳过混合: {e}")
            continue
        return Image.blend(base, photo, 0.45)
    return base


def generate_cover(
    segments: list[dict],
    output_path: str,
    topic: str = "",
    total_duration: float = 0.0,
    width: int = 0,
    height: int = 0,
    project_dir: str = "",
    base_image: str = "",
) -> str:
    """为已合成的视频生成封面 cover.jpg，返回封面路径

    segments 传当前项目的片段列表（用第一张有效配图做底图混合）；
    不传宽高时跟随 config.video_size()，竖版/横版自适应。
    base_image 为用户上传的封面底图（web /api/upload 传入，config 优先已上传
    素材）：存在时全幅铺满作底，缺失/损坏自动回退渐变+配图混合的默认背景。
    """
    if width <= 0 or height <= 0:
        width, height = config.video_size()
    if project_dir and not os.path.isabs(output_path):
        output_path = os.path.join(project_dir, output_path)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    vertical = height > width
    ref = min(width, height)

    title = resolve_title(topic)
    subtitle = f"{datetime.date.today().isoformat()} · 全片 {format_duration(total_duration)}"

    margin = int(width * (0.09 if vertical else 0.06))
    title_size = int(ref * (0.11 if vertical else 0.10))
    sub_size = max(int(ref * 0.040), 20)

    img = _background(segments, width, height, topic, base_image=base_image)
    img = Image.alpha_composite(
        img.convert("RGBA"),
        _scrim(width, height, top_alpha=60, bottom_alpha=185),
    ).convert("RGB")
    draw = ImageDraw.Draw(img)

    # 大号标题：超出 3 行就逐级缩小字号重排（下限 ref 的 5%）
    while True:
        font = _get_system_font(title_size)
        lines = _wrap_text(title, font, width - margin * 2, draw)
        if len(lines) <= 3 or title_size <= int(ref * 0.05):
            break
        title_size = int(title_size * 0.85)

    stroke = max(2, title_size // 28)
    line_gap = int(title_size * 0.32)
    y = int(height * 0.30)
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font, stroke_width=stroke)
        x = (width - (bbox[2] - bbox[0])) // 2 - bbox[0]
        draw.text((x, y), line, font=font, fill=(255, 255, 255),
                  stroke_width=stroke, stroke_fill=(0, 0, 0))
        y += title_size + line_gap

    # 底部副标题：日期 + 时长
    sub_font = _get_system_font(sub_size)
    sb = draw.textbbox((0, 0), subtitle, font=sub_font)
    sx = (width - (sb[2] - sb[0])) // 2 - sb[0]
    sy = height - int(ref * 0.075) - (sb[3] - sb[1])
    draw.text((sx, sy), subtitle, font=sub_font, fill=(232, 232, 232),
              stroke_width=1, stroke_fill=(0, 0, 0))

    img.save(output_path, quality=92)
    print(f"[Cover] 封面已生成: {output_path}")
    return output_path
