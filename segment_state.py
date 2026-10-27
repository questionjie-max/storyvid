"""段级状态落盘 segments.json（任务 D6，R1 §五 P1-3 / §一#8）

把「改任何细节都要整条重跑」改成「分段状态可追溯、可局部重合成」：
项目目录里的 segments.json 记录每段的 文本 / 配音 / 配图 / 时长 状态。
Web /api/segment/edit 据此只重新 TTS 被改的段——其余段的 segment_XXX.mp3
已落盘，由 A12 断点续跑语义直接复用（已花钱的调用不重复），再走 D4 的
reuse_frames 局部重渲把视频重新合成出来。

JSON 与内存任务字段的差异（刻意为之）：
- 磁盘上 audio_path / image_path 存「相对项目目录的路径」：项目目录整体
  挪动/改名后文件对应关系仍然成立；load_segments_state(project_dir) 载入时
  还原为绝对路径（项目目录外落盘的绝对路径原样保留）。
- words（词级时间戳，[{word,start,end}] 列表）原样存 JSON，供改词前的
  段状态完整可追溯；改词后该段旧时间戳作废，由编辑流程清除重取。
- audio_reused / image_reused 等瞬时标记不落盘（只描述「上一次运行是否
  复用」，与段内容无关，载入方不应据此做任何判断）。
"""
import json
import os

SEGMENTS_JSON_NAME = "segments.json"

# 落盘字段白名单：段状态里与本程序局部重合成相关的全部标量/可序列化结构
_FIELDS = ("text", "duration", "audio_path", "image_path", "video_path",
           "image_keyword", "audio_error", "words", "words_source")


def _rel_path(path: str, project_dir: str) -> str:
    """把绝对路径转成相对项目目录的路径；项目目录外的路径原样保留"""
    if not path:
        return ""
    try:
        rel = os.path.relpath(path, project_dir)
    except ValueError:  # Windows 跨盘符无法 relpath
        return path
    if rel.startswith(".."):
        return path
    return rel


def segments_to_jsonable(segments: list[dict], project_dir: str) -> list[dict]:
    """内存段列表 → 可 JSON 序列化的段状态列表（index 按顺序重排，路径转相对）"""
    out: list[dict] = []
    for i, seg in enumerate(segments):
        item: dict = {"index": i}
        for key in _FIELDS:
            value = seg.get(key)
            if key == "duration":
                try:
                    value = float(value or 0.0)
                except (TypeError, ValueError):
                    value = 0.0
            if key in ("audio_path", "image_path", "video_path"):
                value = _rel_path(str(value or ""), project_dir)
            if key == "words":
                value = list(value) if value else []
            item[key] = value if value is not None else ""
        out.append(item)
    return out


def save_segments_state(segments: list[dict], project_dir: str) -> str:
    """把段级状态原子写入 project_dir/segments.json，返回写入路径。

    - project_dir 为空直接返回 ""（无可落盘处，调用方忽略）
    - 先写 .tmp 再 os.replace 原子替换：中断不会留下半个 JSON
    - 任何写盘异常都不抛出：落盘失败只损失「改词重跑」的断点信息，
      不应拖垮已经完成的出片流程
    """
    if not project_dir:
        return ""
    try:
        os.makedirs(project_dir, exist_ok=True)
        path = os.path.join(project_dir, SEGMENTS_JSON_NAME)
        payload = {"version": 1, "segments": segments_to_jsonable(segments, project_dir)}
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return path
    except OSError as e:
        print(f"[Segments] segments.json 落盘失败（忽略，不影响成片）: {e}")
        return ""


def load_segments_state(project_dir: str) -> "list[dict] | None":
    """从 project_dir/segments.json 载入段级状态；相对路径还原为绝对路径。

    文件缺失 / JSON 损坏 / 结构不符一律返回 None（调用方回退内存任务清单）；
    载入结果保证每个段都有 text/duration/audio_path/image_path 键。
    """
    if not project_dir:
        return None
    path = os.path.join(project_dir, SEGMENTS_JSON_NAME)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        print(f"[Segments] segments.json 读取失败（忽略，回退内存清单）: {e}")
        return None
    raw = data.get("segments") if isinstance(data, dict) else data
    if not isinstance(raw, list) or not raw:
        return None
    segments: list[dict] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        seg: dict = {"index": i}
        for key in _FIELDS:
            value = item.get(key)
            if key in ("audio_path", "image_path", "video_path"):
                value = str(value or "")
                if value and not os.path.isabs(value):
                    value = os.path.join(project_dir, value)
            elif key == "duration":
                try:
                    value = float(value or 0.0)
                except (TypeError, ValueError):
                    value = 0.0
            elif key == "words":
                value = list(value) if isinstance(value, list) else []
            seg[key] = value if value is not None else ""
        segments.append(seg)
    return segments or None
