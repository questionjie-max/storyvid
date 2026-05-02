"""字幕生成"""
import os


def generate_srt(segments: list[dict], output_path: str) -> str:
    """生成 SRT 字幕文件"""
    srt_lines = []
    current_time = 0.0

    for i, seg in enumerate(segments):
        start = current_time
        duration = seg.get("duration", 5.0)
        end = start + duration

        start_str = _format_time(start)
        end_str = _format_time(end)

        srt_lines.append(str(i + 1))
        srt_lines.append(f"{start_str} --> {end_str}")
        srt_lines.append(seg["text"])
        srt_lines.append("")

        current_time = end

    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(srt_lines))

    return output_path


def _format_time(seconds: float) -> str:
    """将秒数格式化为 SRT 时间格式 HH:MM:SS,mmm"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds - int(seconds)) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"
