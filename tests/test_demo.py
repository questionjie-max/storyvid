"""演示模式（--demo / DEMO=1）：零 Key 全离线出片。

覆盖三层：
- demo.py 单元：示例文稿可分段、静音配音（固定 3 秒 + 断点续跑）、
  PIL 渐变大字占位图（渐变 + 大字 + 配色轮换 + 断点续跑）；
- main.py 接线：--demo 参数 / DEMO=1 环境变量同效、跳过 API Key 校验、
  可与 --script-file / --batch-aspect 组合；
- 端到端：命令行 --demo 从 argv 到 mp4 文件落地，全程把正式管线的所有
  API 出口换成哨兵（一碰即失败），证明整条链路零网络、零 Key。
"""
import glob
import importlib
import os
import sys

import numpy as np
import pytest
from moviepy import VideoFileClip
from PIL import Image

import config
import demo
import images
import llm
import main
import tts
from story import split_story_into_segments

USER_SCRIPT = (
    "这是用户自己写的演示文稿第一段，长度足够分段。\n\n"
    "这是第二段，同样足够长，验证演示模式也能读用户文稿。"
)


def _boom(name):
    def _raise(*args, **kwargs):
        raise AssertionError(f"演示模式不应调用外部 API/正式管线出口: {name}")
    return _raise


@pytest.fixture
def offline_seams(monkeypatch):
    """把正式管线的全部 API 出口换成哨兵：demo 路径一旦触达即失败。

    - llm.chat：搜索审核/写稿/配图关键词的总出口；
    - main.search_topic / analyze_content / generate_story_with_research：
      演示模式应整体跳过搜索/审核/写稿阶段；
    - tts.generate_speech：TTS API 出口（演示用 ffmpeg 离线静音替代）；
    - images.fetch_image_for_segment / fetch_fallback_image：
      配图 API 出口（演示用 PIL 占位图替代）。
    """
    monkeypatch.setattr(llm, "chat", _boom("llm.chat"))
    monkeypatch.setattr(main, "search_topic", _boom("search_topic"))
    monkeypatch.setattr(main, "analyze_content", _boom("analyze_content"))
    monkeypatch.setattr(main, "generate_story_with_research", _boom("generate_story_with_research"))
    monkeypatch.setattr(tts, "generate_speech", _boom("tts.generate_speech"))
    monkeypatch.setattr(images, "fetch_image_for_segment", _boom("fetch_image_for_segment"))
    monkeypatch.setattr(images, "fetch_fallback_image", _boom("fetch_fallback_image"))


@pytest.fixture
def demo_env(tmp_path, monkeypatch):
    """演示端到端环境：输出进临时目录、小分辨率低帧率加速真实合成、
    每段静音缩到 1 秒、所有 Key 置空（演示模式必须零 Key 可跑）。"""
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(config, "PROJECT_DIR", None)
    monkeypatch.setattr(config, "TEMP_DIR", None)
    monkeypatch.setattr(config, "VIDEO_FPS", 12)
    # main() 会直接改写 config.DEMO / config.PREVIEW，先登记原值供 monkeypatch 还原
    monkeypatch.setattr(config, "DEMO", False)
    monkeypatch.setattr(config, "PREVIEW", False)
    monkeypatch.delenv("DEMO", raising=False)
    monkeypatch.delenv("PREVIEW", raising=False)
    # 小尺寸合成（16:9→480×270 / 9:16→270×480），缩短端到端用时
    monkeypatch.setattr(config, "video_size",
                        lambda aspect_ratio="", preview=None:
                        (270, 480) if aspect_ratio == "9:16" else (480, 270))
    monkeypatch.setattr(demo, "DEMO_SEGMENT_DURATION", 1.0)
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    monkeypatch.setattr(config, "STEPFUN_API_KEY", "")
    monkeypatch.setattr(config, "MINIMAX_API_KEY", "")
    # 演示端到端钉回硬切语义（默认值已是 0.6，交叉淡化由 test_video.TestCrossfade 覆盖）
    monkeypatch.setattr(config, "CROSSFADE_SECONDS", 0.0)


class TestDemoConfig:
    """DEMO 环境变量解析与内置文稿"""

    def test_demo_env_parsing(self, monkeypatch):
        """DEMO=1/true 开、0/off 关（reload config 验证，与 PREVIEW 同一套解析）"""
        before_keys = set(os.environ)
        snapshot = config.DEMO
        try:
            monkeypatch.setenv("DEMO", "1")
            importlib.reload(config)
            assert config.DEMO is True

            monkeypatch.setenv("DEMO", "0")
            importlib.reload(config)
            assert config.DEMO is False

            monkeypatch.setenv("DEMO", "off")
            importlib.reload(config)
            assert config.DEMO is False
        finally:
            for k in set(os.environ) - before_keys:
                os.environ.pop(k, None)
            config.DEMO = snapshot

    def test_demo_story_splits_into_multiple_segments(self):
        """内置示例文稿按空行分段：段数足够成片、每段都过长分段过滤线"""
        segs = split_story_into_segments(demo.DEMO_STORY)
        assert len(segs) >= 4, f"示例文稿应分出多段，实际 {len(segs)}"
        assert all(len(s["text"]) >= 10 for s in segs), "每段都应超过分段过滤线（10 字）"
        assert all(s["image_keyword"] == "" for s in segs)


class TestDemoKeywords:
    """fill_demo_keywords：段首大字、空白剔除、两级兜底"""

    def test_uses_whitespace_stripped_segment_head(self):
        segs = [{"text": "这是第一段的文案，足够长。"},
                {"text": "第二段\n换行  也算空白。"}]
        out = demo.fill_demo_keywords(segs, "某主题")
        assert out[0]["image_keyword"] == "这是第一段的文案，足够长。"[:12]
        assert out[1]["image_keyword"] == "第二段换行也算空白。"[:12]  # \n 与空格被剔除

    def test_fallback_topic_then_example_label(self):
        assert demo.fill_demo_keywords([{"text": ""}], "主题甲")[0]["image_keyword"] == "主题甲"
        assert demo.fill_demo_keywords([{"text": ""}], "")[0]["image_keyword"] == "示例画面1"


class TestDemoSpeech:
    """演示配音：ffmpeg 离线静音 mp3，每段固定 3 秒，断点续跑语义一致"""

    def test_generate_silence_fixed_duration(self, tmp_path):
        out = tmp_path / "silence.mp3"
        demo.generate_silence(str(out), 3.0)
        assert out.exists() and out.stat().st_size > 0
        assert tts.get_audio_duration(str(out)) == pytest.approx(3.0, abs=0.3)

    def test_fill_demo_speech_sets_fixed_duration_and_paths(self, tmp_path):
        segs = [{"text": "第一段。"}, {"text": "第二段。"}]
        out = demo.fill_demo_speech(segs, project_dir=str(tmp_path))
        assert demo.DEMO_SEGMENT_DURATION == 3.0  # 默认每段 3 秒（任务要求）
        for i, s in enumerate(out):
            assert s["audio_path"] == str(tmp_path / f"segment_{i:03d}.mp3")
            assert os.path.isfile(s["audio_path"])
            assert s["duration"] == 3.0
        # 实测音频时长 ≈ 3 秒（mp3 编码器填充允许零点几秒误差）
        assert tts.get_audio_duration(out[0]["audio_path"]) == pytest.approx(3.0, abs=0.3)

    def test_fill_demo_speech_reuses_existing(self, tmp_path):
        """断点续跑：已存在且非空的 segment_XXX.mp3 直接复用，不重写"""
        pre = tmp_path / "segment_000.mp3"
        pre.write_bytes(b"pre-existing-bytes")
        out = demo.fill_demo_speech([{"text": "第一段。"}], project_dir=str(tmp_path))
        assert pre.read_bytes() == b"pre-existing-bytes"
        assert out[0]["audio_path"] == str(pre)

    def test_fill_demo_speech_never_calls_tts(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tts, "generate_speech", _boom("tts.generate_speech"))
        out = demo.fill_demo_speech([{"text": "第一段。"}], project_dir=str(tmp_path))
        assert os.path.isfile(out[0]["audio_path"])


class TestDemoImages:
    """演示配图：PIL 渐变 + 大字占位图，配色轮换，断点续跑语义一致"""

    @staticmethod
    def _small_size(monkeypatch, size=(480, 270)):
        monkeypatch.setattr(config, "video_size", lambda aspect_ratio="", preview=None: size)

    def test_placeholder_is_gradient_with_big_text(self, tmp_path, monkeypatch):
        self._small_size(monkeypatch)
        out = demo.fetch_demo_images(
            [{"text": "渐变占位图测试段落。", "image_keyword": "渐变占位图测试段落"}],
            project_dir=str(tmp_path))
        path = out[0]["image_path"]
        assert path == str(tmp_path / "image_000.jpg")
        img = Image.open(path).convert("RGB")
        assert img.size == (480, 270)
        top, bottom = img.getpixel((240, 2)), img.getpixel((240, 267))
        assert sum(bottom) > sum(top), "占位图应为上深下浅的渐变底"
        arr = np.asarray(img, dtype=int)
        white = int((np.abs(arr - 255).sum(axis=2) < 90).sum())
        assert white > 500, f"画面中央应有大字（白色像素 {white} 应显著）"

    def test_palettes_rotate_by_index(self, tmp_path, monkeypatch):
        """同文案不同段：配色逐段轮换，图片内容不相同"""
        self._small_size(monkeypatch)
        segs = [{"image_keyword": "同词"}, {"image_keyword": "同词"}]
        out = demo.fetch_demo_images(segs, project_dir=str(tmp_path))
        with open(out[0]["image_path"], "rb") as f:
            b0 = f.read()
        with open(out[1]["image_path"], "rb") as f:
            b1 = f.read()
        assert b0 != b1

    def test_portrait_aspect_uses_portrait_size(self, tmp_path, monkeypatch):
        """aspect_ratio 传入后尺寸跟随 config.video_size（批量画幅首画幅原生尺寸）"""
        self._small_size(monkeypatch, size=(270, 480))
        out = demo.fetch_demo_images([{"image_keyword": "竖屏"}],
                                     aspect_ratio="9:16", project_dir=str(tmp_path))
        assert Image.open(out[0]["image_path"]).size == (270, 480)

    def test_resume_reuses_existing_placeholder(self, tmp_path, monkeypatch):
        """断点续跑：已存在且可完整解码的 image_XXX.jpg 直接复用，不重新生成
        （B6 起复用判据含解码校验：损坏的非空坏图会重新生成，见
        overnight_reports/B6_regress.md——fixture 必须放真实 jpg）"""
        self._small_size(monkeypatch)
        pre = tmp_path / "image_000.jpg"
        Image.new("RGB", (64, 64), (9, 9, 9)).save(pre, quality=90)
        before = pre.read_bytes()
        monkeypatch.setattr(demo, "_build_card_frame", _boom("_build_card_frame"))
        out = demo.fetch_demo_images([{"image_keyword": "k"}], project_dir=str(tmp_path))
        assert pre.read_bytes() == before
        assert out[0]["image_path"] == str(pre)

    def test_corrupt_placeholder_is_regenerated(self, tmp_path, monkeypatch):
        """损坏的非空坏图不复用：删除后重新生成（占位图离线零成本，
        与 images.fetch_all_images 的坏图处理同一判据）"""
        self._small_size(monkeypatch)
        pre = tmp_path / "image_000.jpg"
        pre.write_bytes(b"pre-existing-bytes")  # 非空但无法解码
        out = demo.fetch_demo_images([{"image_keyword": "k"}], project_dir=str(tmp_path))
        assert pre.read_bytes() != b"pre-existing-bytes"  # 被重新生成覆盖
        assert images._is_valid_image_file(str(pre))
        assert out[0]["image_path"] == str(pre)

    def test_never_calls_image_apis(self, tmp_path, monkeypatch):
        self._small_size(monkeypatch, size=(320, 180))
        out = demo.fetch_demo_images([{"image_keyword": "离线"}], project_dir=str(tmp_path))
        assert os.path.isfile(out[0]["image_path"])


class TestDemoWorkflowEndToEnd:
    """端到端：命令行 --demo 从 argv 到 mp4 文件落地（全程零网络零 Key）"""

    def test_cli_demo_produces_mp4(self, demo_env, offline_seams, tmp_path,
                                   monkeypatch, capsys):
        """核心验收：python main.py --demo --output demo.mp4 → 项目目录落地成片"""
        monkeypatch.setattr(sys, "argv", ["main.py", "--demo", "--output", "demo.mp4"])
        main.main()
        out = capsys.readouterr().out

        assert "演示模式" in out
        assert "跳过 API Key 校验" in out  # 全空 Key 下未触发 validate_config 退出

        proj = next(d for d in tmp_path.iterdir() if d.is_dir())
        video_path = proj / "demo.mp4"
        assert video_path.exists() and video_path.stat().st_size > 10_000

        # 成片：6 段 × 1 秒（测试缩短）+ 片头卡 1.8s + 片尾卡 1.5s，横屏小尺寸
        with VideoFileClip(str(video_path)) as v:
            assert v.size == [480, 270]
            assert v.duration == pytest.approx(6 * 1.0 + 3.3, abs=0.5)
            assert v.audio is not None, "静音段是真实音频文件，成片应带音轨"

        # 项目产物齐全：文稿=内置示例、字幕、封面、每段一个静音 mp3 + 一张占位图
        assert (proj / "story.txt").read_text(encoding="utf-8") == demo.DEMO_STORY
        assert (proj / "subtitles.srt").exists()
        assert (proj / "cover.jpg").exists()
        assert len(glob.glob(str(proj / "segment_*.mp3"))) == 6
        assert len(glob.glob(str(proj / "image_*.jpg"))) == 6
        # 占位图按首画幅尺寸生成
        assert Image.open(proj / "image_000.jpg").size == (480, 270)

        # 字幕首条从片头卡之后开始（与正式管线一致）
        first_block = (proj / "subtitles.srt").read_text(encoding="utf-8").strip().split("\n\n")[0]
        assert first_block.splitlines()[1].startswith("00:00:01,800 -->")

    def test_demo_env_var_equivalent_to_flag(self, demo_env, offline_seams, tmp_path,
                                             monkeypatch, capsys):
        """DEMO=1 环境变量与 --demo 同效：不带 flag 也能跑通演示模式"""
        monkeypatch.setattr(config, "DEMO", True)  # env DEMO=1 在 import config 时解析为该值
        monkeypatch.setattr(sys, "argv", ["main.py", "--output", "env.mp4"])
        main.main()
        assert "演示模式" in capsys.readouterr().out
        proj = next(d for d in tmp_path.iterdir() if d.is_dir())
        assert (proj / "env.mp4").exists()

    def test_cli_demo_with_script_file(self, demo_env, offline_seams, tmp_path,
                                       monkeypatch, capsys):
        """--demo 搭配 --script-file：用用户文稿替换内置示例，仍是零 Key 离线管线"""
        script = tmp_path / "user.txt"
        script.write_text(USER_SCRIPT, encoding="utf-8")
        monkeypatch.setattr(sys, "argv",
                            ["main.py", "--demo", "--script-file", str(script),
                             "--output", "mine.mp4"])
        main.main()
        out = capsys.readouterr().out
        assert "文稿直出模式" in out and "演示模式" in out

        proj = next(d for d in tmp_path.iterdir() if d.is_dir())
        assert (proj / "mine.mp4").exists() and (proj / "mine.mp4").stat().st_size > 10_000
        assert (proj / "story.txt").read_text(encoding="utf-8") == USER_SCRIPT
        assert len(glob.glob(str(proj / "segment_*.mp3"))) == 2  # 用户文稿 2 段

    def test_cli_demo_batch_aspect(self, demo_env, offline_seams, tmp_path, monkeypatch):
        """--demo 搭配 --batch-aspect：横竖两版一次出，占位图底图被智能裁切派生"""
        monkeypatch.setattr(sys, "argv",
                            ["main.py", "--demo", "--batch-aspect", "16:9,9:16",
                             "--output", "ba.mp4"])
        main.main()
        proj = next(d for d in tmp_path.iterdir() if d.is_dir())
        assert (proj / "ba_16x9.mp4").exists() and (proj / "ba_9x16.mp4").exists()
        assert (proj / "image_000_9x16.jpg").exists()  # 第二画幅离线裁切派生
        with VideoFileClip(str(proj / "ba_16x9.mp4")) as v:
            assert v.size == [480, 270]
        with VideoFileClip(str(proj / "ba_9x16.mp4")) as v:
            assert v.size == [270, 480]
