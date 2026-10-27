"""segment_state.py：段级状态落盘 segments.json 的读写往返（任务 D6）。

Web 层的编辑→局部重跑集成测试见 tests/test_web.py 的 TestSegmentEditAPI；
CLI 管线落盘的回归测试见 tests/test_resume.py 的 TestMainSegmentsJson。
"""
import json
import os

import pytest

import segment_state


def test_save_and_load_roundtrip(tmp_path):
    """保存后载入应还原各段文本/时长/关键词/词级时间戳，index 顺序一致"""
    segments = [
        {"text": "第一段内容，足够长。", "duration": 3.2, "image_keyword": "城市",
         "audio_path": str(tmp_path / "segment_000.mp3"),
         "image_path": str(tmp_path / "image_000.jpg"),
         "words": [{"word": "第", "start": 0.0, "end": 0.1}],
         "words_source": "aligner", "audio_reused": True},
        {"text": "第二段内容，同样足够长。", "duration": 2.5, "image_keyword": "海边",
         "audio_path": "", "image_path": None, "audio_error": "TTS 挂了"},
    ]
    path = segment_state.save_segments_state(segments, str(tmp_path))

    assert path == str(tmp_path / "segments.json")
    assert os.path.isfile(path)

    loaded = segment_state.load_segments_state(str(tmp_path))
    assert loaded is not None and len(loaded) == 2

    first = loaded[0]
    assert first["index"] == 0
    assert first["text"] == "第一段内容，足够长。"
    assert first["duration"] == pytest.approx(3.2)
    assert first["image_keyword"] == "城市"
    assert first["audio_path"] == str(tmp_path / "segment_000.mp3")
    assert first["image_path"] == str(tmp_path / "image_000.jpg")
    assert first["words"] == [{"word": "第", "start": 0.0, "end": 0.1}]
    assert first["words_source"] == "aligner"
    # 瞬时标记（audio_reused 等复用状态）不落盘
    assert "audio_reused" not in first

    second = loaded[1]
    assert second["text"] == "第二段内容，同样足够长。"
    assert second["audio_path"] == ""
    assert second["image_path"] == ""
    assert second["audio_error"] == "TTS 挂了"


def test_json_on_disk_stores_relative_paths(tmp_path):
    """磁盘上的路径字段是相对项目目录的（项目目录整体挪动后仍可用）"""
    segments = [{"text": "s", "duration": 1.0,
                 "audio_path": str(tmp_path / "segment_000.mp3"),
                 "image_path": str(tmp_path / "image_000.jpg")}]
    segment_state.save_segments_state(segments, str(tmp_path))

    with open(tmp_path / "segments.json", "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["version"] == 1
    assert data["segments"][0]["audio_path"] == "segment_000.mp3"
    assert data["segments"][0]["image_path"] == "image_000.jpg"

    # 项目目录改名后载入：相对路径按新目录还原
    moved = tmp_path.parent / (tmp_path.name + "_moved")
    moved.mkdir()
    (moved / "segments.json").write_text(
        (tmp_path / "segments.json").read_text(encoding="utf-8"), encoding="utf-8")
    loaded = segment_state.load_segments_state(str(moved))
    assert loaded[0]["audio_path"] == str(moved / "segment_000.mp3")


def test_out_of_project_path_kept_absolute(tmp_path):
    """项目目录之外的路径原样保留（relpath 会得到 .. 开头）"""
    outside = tmp_path.parent / "elsewhere.jpg"
    segments = [{"text": "s", "duration": 1.0, "image_path": str(outside)}]
    segment_state.save_segments_state(segments, str(tmp_path))

    loaded = segment_state.load_segments_state(str(tmp_path))
    assert loaded[0]["image_path"] == str(outside)


def test_save_atomic_no_tmp_leftover(tmp_path):
    """原子写：成功后不留 .tmp 半成品"""
    segment_state.save_segments_state([{"text": "s", "duration": 1.0}], str(tmp_path))
    assert list(tmp_path.glob("*.tmp")) == []


def test_load_missing_or_broken_returns_none(tmp_path, monkeypatch, capsys):
    """文件缺失 / JSON 损坏 / 结构不符 → None（调用方回退内存清单），不抛异常"""
    assert segment_state.load_segments_state(str(tmp_path)) is None
    assert segment_state.load_segments_state("") is None

    (tmp_path / "segments.json").write_text("{不是 JSON", encoding="utf-8")
    assert segment_state.load_segments_state(str(tmp_path)) is None

    (tmp_path / "segments.json").write_text('{"segments": "不是列表"}', encoding="utf-8")
    assert segment_state.load_segments_state(str(tmp_path)) is None


def test_save_failure_swallowed(tmp_path, monkeypatch):
    """写盘异常不外泄（落盘失败只损失断点信息，不拖垮出片流程）"""
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(json, "dump", boom)
    assert segment_state.save_segments_state([{"text": "s"}], str(tmp_path)) == ""
