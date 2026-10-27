"""断点续跑：images.py 的预置文件复用与重试计数、main.py 的复用提示输出。

TTS 侧（tts.py）的同类用例见 tests/test_tts.py 的
TestGenerateAllSpeechResume / TestGenerateAllSpeechRetry。
"""
import os
import types
from pathlib import Path

import pytest
from PIL import Image

import config
import images
import llm
import main


SCRIPT_TEXT = (
    "这是用户自己写的故事稿第一段，字数足够长，不会被分段逻辑过滤掉。\n\n"
    "这是第二段，同样足够长，用来验证文稿直出的完整管线。"
)


def _make_jpg(path):
    """生成真实可解码的 jpg（复用判据含完整解码校验，坏字节不再算「已完成」）"""
    Image.new("RGB", (64, 64), (80, 80, 80)).save(path, quality=90)
    return str(path)


def _stub_time(monkeypatch, recorder):
    """把 images.time 换成记录型替身：验证退避参数时不真实等待"""
    monkeypatch.setattr(images, "time", types.SimpleNamespace(sleep=recorder))


class TestFetchAllImagesResume:
    """断点续跑：已存在且非空的 image_XXX.jpg 直接复用，不再调配图来源"""

    def test_existing_image_file_is_reused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(config, "TEMP_DIR", str(tmp_path))

        pre = tmp_path / "image_000.jpg"
        _make_jpg(pre)

        def must_not_fetch(*a, **k):
            raise AssertionError("已存在的配图不应再触发任何来源获取")

        sleeps = []
        _stub_time(monkeypatch, sleeps.append)  # 第 2 段获取失败会走重试，不能真等 2s+8s
        monkeypatch.setattr(images, "fetch_image_for_segment", must_not_fetch)

        segments = [
            {"text": "第一段", "duration": 5.0, "image_keyword": "城市"},
            {"text": "第二段", "duration": 5.0, "image_keyword": "海边"},
        ]
        result = images.fetch_all_images(segments, project_dir=str(tmp_path))

        assert result[0]["image_path"] == str(pre)
        assert result[0].get("image_reused") is True
        assert pre.stat().st_size > 0  # 未被覆盖
        # 缺失段不受影响：获取失败（异常计入重试后仍失败）→ None，不打复用标记
        assert result[1].get("image_reused") is None

    def test_existing_image_does_not_consume_retries(self, tmp_path, monkeypatch):
        """复用段不应产生任何重试等待（与失败段的退避区分开）"""
        monkeypatch.setattr(config, "PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(config, "TEMP_DIR", str(tmp_path))
        _make_jpg(tmp_path / "image_000.jpg")

        def must_not_fetch(*a, **k):
            raise AssertionError("不应调用")

        sleeps = []
        _stub_time(monkeypatch, sleeps.append)
        monkeypatch.setattr(images, "fetch_image_for_segment", must_not_fetch)

        images.fetch_all_images(
            [{"text": "s", "duration": 5.0, "image_keyword": "k"}],
            project_dir=str(tmp_path),
        )
        assert sleeps == []  # 复用路径零等待

    def test_empty_image_file_is_regenerated(self, tmp_path, monkeypatch):
        """0 字节的 image_XXX.jpg 视为中断残留，必须重新获取"""
        monkeypatch.setattr(config, "PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(config, "TEMP_DIR", str(tmp_path))

        empty = tmp_path / "image_000.jpg"
        empty.write_bytes(b"")

        def fake_fetch(keyword, index, image_source="", aspect_ratio="", project_dir=""):
            path = os.path.join(str(tmp_path), f"image_{index:03d}.jpg")
            with open(path, "wb") as f:
                f.write(b"fresh jpg")
            return path

        monkeypatch.setattr(images, "fetch_image_for_segment", fake_fetch)

        result = images.fetch_all_images(
            [{"text": "s", "duration": 5.0, "image_keyword": "k"}],
            project_dir=str(tmp_path),
        )

        assert result[0]["image_path"] == str(empty)
        assert empty.read_bytes() == b"fresh jpg"  # 空文件被重新获取覆盖
        assert result[0].get("image_reused") is None


class TestFetchAllImagesRetry:
    """单段配图失败自动重试 2 次（退避 2s/8s），仍失败才标记跳过（黑底帧）"""

    def test_retry_then_success(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(config, "TEMP_DIR", str(tmp_path))
        sleeps = []
        _stub_time(monkeypatch, sleeps.append)

        calls = {"n": 0}

        def flaky_fetch(keyword, index, image_source="", aspect_ratio="", project_dir=""):
            calls["n"] += 1
            if calls["n"] < 3:
                return None  # 前两次所有来源都拿不到图
            path = os.path.join(str(tmp_path), f"image_{index:03d}.jpg")
            with open(path, "wb") as f:
                f.write(b"late jpg")
            return path

        monkeypatch.setattr(images, "fetch_image_for_segment", flaky_fetch)

        result = images.fetch_all_images(
            [{"text": "s", "duration": 5.0, "image_keyword": "k"}],
            project_dir=str(tmp_path),
        )

        assert calls["n"] == 3  # 首次尝试 + 重试 2 次
        assert sleeps == [2.0, 8.0]  # 退避 2s / 8s
        assert result[0]["image_path"] == os.path.join(str(tmp_path), "image_000.jpg")
        assert not result[0].get("image_reused")

    def test_retry_exhausted_marks_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(config, "TEMP_DIR", str(tmp_path))
        sleeps = []
        _stub_time(monkeypatch, sleeps.append)

        calls = {"n": 0}

        def always_none(keyword, index, image_source="", aspect_ratio="", project_dir=""):
            calls["n"] += 1
            return None

        monkeypatch.setattr(images, "fetch_image_for_segment", always_none)

        result = images.fetch_all_images(
            [{"text": "s", "duration": 5.0, "image_keyword": "k"}],
            project_dir=str(tmp_path),
        )

        assert calls["n"] == 3  # 恰好重试 2 次（共 3 次尝试）后放弃
        assert sleeps == [2.0, 8.0]
        assert result[0]["image_path"] is None  # 标记跳过，合成时用黑底帧
        assert result[0].get("image_reused") is None


class _ResumePipeline:
    """main.py 管线 mock：TTS/配图/合成/封面全换本地假件，llm.chat 哨兵"""

    def __init__(self, reuse_speech: bool = False, reuse_images: bool = False):
        self.segment_count = 0
        self.reuse_speech = reuse_speech
        self.reuse_images = reuse_images

    def install(self, tmp_path, monkeypatch):
        monkeypatch.setattr(llm, "chat", lambda *a, **k: "llm.chat 不应被调用")
        monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path / "output"))
        monkeypatch.setattr(config, "PROJECT_DIR", None)
        monkeypatch.setattr(config, "TEMP_DIR", None)

        def fake_speech(segments, provider="", voice="", project_dir=""):
            self.segment_count = len(segments)
            for i, s in enumerate(segments):
                s["audio_path"] = ""
                s["duration"] = 2.0
                if self.reuse_speech and i == 0:
                    s["audio_reused"] = True
            return segments

        def fake_images(segments, image_source="", aspect_ratio="", project_dir=""):
            for s in segments:
                s["image_path"] = None
                if self.reuse_images:
                    s["image_reused"] = True
            return segments

        def fake_video(segments, output_path, width=0, height=0, project_dir="", topic=""):
            with open(output_path, "wb") as f:
                f.write(b"fake mp4 content" * 100)
            return output_path

        monkeypatch.setattr(main, "generate_all_speech", fake_speech)
        monkeypatch.setattr(main, "fetch_all_images", fake_images)
        monkeypatch.setattr(main, "create_video", fake_video)
        monkeypatch.setattr(main, "generate_cover", lambda *a, **k: "")


@pytest.fixture
def script_file(tmp_path):
    path = tmp_path / "my_script.txt"
    path.write_text(SCRIPT_TEXT, encoding="utf-8")
    return str(path)


class TestMainReuseMessage:
    """main.py 输出「检测到 N 个已完成段，已复用」提示"""

    def test_prints_reused_segments_message(self, tmp_path, monkeypatch, capsys, script_file):
        pipeline = _ResumePipeline(reuse_speech=True, reuse_images=True)
        pipeline.install(tmp_path, monkeypatch)

        final_path = main.run_workflow_from_script(script_file, topic="续跑主题")
        assert final_path.endswith(".mp4")

        out = capsys.readouterr().out
        assert pipeline.segment_count >= 1  # 前置：管线确实分出了段
        # fake_speech 只给第 1 段打 audio_reused → main 应输出「检测到 1 个已完成段，已复用」
        assert "检测到 1 个已完成段，已复用" in out
        # 配图 fake 全部打 image_reused → 同步输出配图复用提示
        assert f"检测到 {pipeline.segment_count} 张已完成配图，已复用" in out

    def test_no_message_when_nothing_reused(self, tmp_path, monkeypatch, capsys, script_file):
        pipeline = _ResumePipeline(reuse_speech=False, reuse_images=False)
        pipeline.install(tmp_path, monkeypatch)

        main.run_workflow_from_script(script_file, topic="全新主题")

        out = capsys.readouterr().out
        assert "已复用" not in out


class TestMainSegmentsJson:
    """段级状态落盘（D6）：main.py 管线跑完在项目目录写 segments.json，
    记录各段文本/时长/配音/配图路径，供 Web /api/segment/edit 局部重跑使用"""

    def test_pipeline_writes_segments_json(self, tmp_path, monkeypatch, script_file):
        pipeline = _ResumePipeline(reuse_speech=True, reuse_images=True)
        pipeline.install(tmp_path, monkeypatch)

        final_path = main.run_workflow_from_script(script_file, topic="落盘主题")
        assert final_path.endswith(".mp4")

        project_dir = Path(final_path).parent
        assert (project_dir / "segments.json").is_file()

        import segment_state
        segments = segment_state.load_segments_state(str(project_dir))
        assert segments is not None
        assert len(segments) == pipeline.segment_count
        assert [s["index"] for s in segments] == list(range(pipeline.segment_count))
        assert segments[0]["text"].startswith("这是用户自己写的故事稿第一段")
        assert segments[0]["duration"] == pytest.approx(2.0)
        # 路径落盘为相对项目目录的形式（audio_path 为空串 = TTS 替身未产音频）
        assert segments[0]["audio_path"] == ""
        assert segments[0]["image_path"] == ""
