"""视频合成"""
import os
from PIL import Image, ImageDraw, ImageFont
from moviepy.editor import ImageClip, AudioFileClip, concatenate_videoclips
from config import VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS, TEMP_DIR, get_project_dir


def _get_system_font(size: int):
    """找系统中文字体"""
    candidates = [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
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


def _draw_subtitle_on_image(image_path: str, text: str, output_path: str) -> str:
    """用 Pillow 在图片上绘制字幕"""
    img = Image.open(image_path).convert("RGB")
    img = img.resize((VIDEO_WIDTH, VIDEO_HEIGHT), Image.LANCZOS)
    draw = ImageDraw.Draw(img)

    # 竖屏时字体稍小，边距调整
    font_size = 38 if VIDEO_HEIGHT > VIDEO_WIDTH else 42
    font = _get_system_font(font_size)

    # 换行
    margin = 60 if VIDEO_HEIGHT > VIDEO_WIDTH else 100
    max_text_width = VIDEO_WIDTH - margin * 2
    lines = _wrap_text(text, font, max_text_width, draw)

    # 计算文字区域高度
    line_height = font_size + 12
    text_block_height = len(lines) * line_height + 40

    # 底部半透明背景条（竖屏更靠下）
    bottom_margin = 80 if VIDEO_HEIGHT > VIDEO_WIDTH else 40
    bar_y = VIDEO_HEIGHT - text_block_height - bottom_margin
    overlay = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    overlay_draw.rectangle(
        [(0, bar_y), (VIDEO_WIDTH, VIDEO_HEIGHT)],
        fill=(0, 0, 0, 160)
    )
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(img)

    # 绘制文字（带描边）
    y = bar_y + 20
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        text_w = bbox[2] - bbox[0]
        x = (VIDEO_WIDTH - text_w) // 2

        # 黑色描边
        for dx, dy in [(-2, -2), (-2, 0), (-2, 2), (0, -2), (0, 2), (2, -2), (2, 0), (2, 2)]:
            draw.text((x + dx, y + dy), line, font=font, fill=(0, 0, 0))
        # 白色文字
        draw.text((x, y), line, font=font, fill=(255, 255, 255))
        y += line_height

    img.save(output_path, quality=95)
    return output_path


def create_video(segments: list[dict], output_path: str) -> str:
    """将图片、音频、字幕合成为视频，所有中间文件直接放在项目目录"""
    proj_dir = get_project_dir()
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)

    clips = []

    for i, seg in enumerate(segments):
        duration = seg.get("duration", 5.0)
        img_path = seg.get("image_path")
        audio_path = seg.get("audio_path")
        text = seg["text"]

        # 准备底图
        if not img_path or not os.path.exists(img_path):
            black = Image.new("RGB", (VIDEO_WIDTH, VIDEO_HEIGHT), "black")
            img_path = os.path.join(proj_dir, f"_black_{i:03d}.jpg")
            black.save(img_path)

        # 在图上绘制字幕
        subtitled_path = os.path.join(proj_dir, f"frame_{i:03d}.jpg")
        try:
            _draw_subtitle_on_image(img_path, text, subtitled_path)
            frame_path = subtitled_path
        except Exception as e:
            print(f"[Video] 字幕绘制失败，使用原图: {e}")
            frame_path = img_path

        # 创建视频片段
        img_clip = ImageClip(frame_path).set_duration(duration)

        if audio_path and os.path.exists(audio_path):
            audio = AudioFileClip(audio_path)
            img_clip = img_clip.set_audio(audio)

        clips.append(img_clip)

    if not clips:
        raise ValueError("没有可合成的片段")

    # 拼接所有片段
    final_video = concatenate_videoclips(clips, method="compose")
    final_video = final_video.set_fps(VIDEO_FPS)

    # 写入文件（临时音频也放项目目录）
    final_video.write_videofile(
        output_path,
        fps=VIDEO_FPS,
        codec="libx264",
        audio_codec="aac",
        temp_audiofile=os.path.join(proj_dir, "_temp_audio.m4a"),
        remove_temp=True,
    )

    # 清理
    for clip in clips:
        clip.close()
    final_video.close()

    return output_path
