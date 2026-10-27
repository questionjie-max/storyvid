"""文稿直出（main.py --script-file / run_workflow_from_script）：离线测试

核心断言：传入文稿后整条管线（分段→TTS→配图→字幕→合成→封面）
一次 llm.chat 都不发生——搜索/审核/LLM 写稿/LLM 配图关键词全部跳过。
"""
import os
import sys

import pytest

import config
import llm
import main
import story


SCRIPT_TEXT = (
    "这是用户自己写的故事稿第一段，字数足够长，不会被分段逻辑过滤掉。\n\n"
    "这是第二段，同样足够长，用来验证文稿直出的完整管线。"
)


@pytest.fixture
def script_file(tmp_path):
    path = tmp_path / "my_script.txt"
    path.write_text(SCRIPT_TEXT, encoding="utf-8")
    return str(path)


@pytest.fixture
def offline_pipeline(tmp_path, monkeypatch):
    """管线各步 mock 成纯本地操作；llm.chat 换成哨兵并记录调用"""
    llm_calls = []

    def fake_chat(*a, **k):
        llm_calls.append(a[0] if a else "")
        return "llm.chat 不应在文稿直出模式被调用"

    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setattr(config, "PROJECT_DIR", None)
    monkeypatch.setattr(config, "TEMP_DIR", None)

    def fake_speech(segments, provider="", voice="", project_dir=""):
        for s in segments:
            s["audio_path"] = ""
            s["duration"] = 2.0
        return segments

    def fake_images(segments, image_source="", aspect_ratio="", project_dir=""):
        for s in segments:
            s["image_path"] = None
        return segments

    def fake_video(segments, output_path, width=0, height=0, project_dir="", topic=""):
        with open(output_path, "wb") as f:
            f.write(b"fake mp4 content" * 100)
        return output_path

    monkeypatch.setattr(main, "generate_all_speech", fake_speech)
    monkeypatch.setattr(main, "fetch_all_images", fake_images)
    monkeypatch.setattr(main, "create_video", fake_video)
    monkeypatch.setattr(main, "generate_cover", lambda *a, **k: "")
    return llm_calls


class TestRunWorkflowFromScript:
    def test_full_pipeline_without_llm(self, script_file, offline_pipeline):
        final_path = main.run_workflow_from_script(script_file, topic="自定义主题")

        assert final_path.endswith(".mp4")
        assert os.path.exists(final_path)
        # 核心验收：传入文稿后 llm.chat 一次都不被调用
        assert offline_pipeline == []

        project_dir = os.path.dirname(final_path)
        with open(os.path.join(project_dir, "story.txt"), encoding="utf-8") as f:
            assert f.read() == SCRIPT_TEXT  # 保存的是用户原稿
        assert os.path.exists(os.path.join(project_dir, "subtitles.srt"))

    def test_topic_derived_from_first_line(self, script_file, offline_pipeline, tmp_path):
        main.run_workflow_from_script(script_file)
        # 未传主题：项目目录名用文稿首行前 20 字（带日期前缀）
        expected = story.derive_topic_from_script(SCRIPT_TEXT)
        dirs = [d.name for d in (tmp_path / "output").iterdir() if d.is_dir()]
        assert len(dirs) == 1
        # setup_project 会把非字母数字字符换成 _，中文保留
        sanitized = "".join(c if c.isalnum() or c in "_- " else "_" for c in expected)
        assert sanitized in dirs[0]

    def test_output_name_suffix_enforced(self, script_file, offline_pipeline):
        final_path = main.run_workflow_from_script(
            script_file, topic="后缀主题", output_name="成品")
        assert os.path.basename(final_path) == "成品.mp4"


class TestScriptFileValidation:
    def test_missing_file_returns_empty(self, offline_pipeline, tmp_path):
        assert main.run_workflow_from_script(str(tmp_path / "不存在.txt")) == ""

    def test_empty_file_returns_empty(self, offline_pipeline, tmp_path):
        path = tmp_path / "empty.txt"
        path.write_text("   \n  ", encoding="utf-8")
        assert main.run_workflow_from_script(str(path)) == ""


class TestCliScriptFile:
    def test_main_dispatches_to_script_mode_without_llm_key(
            self, script_file, offline_pipeline, monkeypatch, capsys):
        """CLI 端到端：--script-file 且无 LLM_API_KEY 也应能启动并出片"""
        monkeypatch.setattr(sys, "argv",
                            ["main.py", "--script-file", script_file, "--output", "out.mp4"])
        monkeypatch.setattr(config, "LLM_API_KEY", "")
        monkeypatch.setattr(config, "TTS_PROVIDER", "stepfun")
        monkeypatch.setattr(config, "STEPFUN_API_KEY", "fake-key")
        monkeypatch.setattr(config, "IMAGE_SOURCE", "minimax")
        monkeypatch.setattr(config, "MINIMAX_API_KEY", "")

        main.main()
        assert "视频生成完成" in capsys.readouterr().out
        assert offline_pipeline == []  # 无 LLM key 也全程没碰 llm.chat

    def test_main_requires_topic_or_script(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["main.py"])
        with pytest.raises(SystemExit):
            main.main()  # 既没主题也没文稿：argparse error 退出码非 0


class TestStoryHelpers:
    def test_fill_fallback_keywords_uses_topic(self):
        segs = [{"text": "第一段足够长", "image_keyword": ""},
                {"text": "第二段足够长", "image_keyword": ""}]
        result = story.fill_fallback_keywords(segs, "测试主题")
        assert [s["image_keyword"] for s in result] == ["测试主题", "测试主题"]

    def test_fill_fallback_keywords_keeps_existing(self):
        segs = [{"text": "第一段足够长", "image_keyword": "ancient palace"}]
        assert story.fill_fallback_keywords(segs, "测试主题")[0]["image_keyword"] == "ancient palace"

    def test_derive_topic_first_line(self):
        assert story.derive_topic_from_script("第一行是标题\n\n正文内容") == "第一行是标题"

    def test_derive_topic_strips_markdown_hash(self):
        assert story.derive_topic_from_script("## 三国演义\n正文足够长") == "三国演义"

    def test_derive_topic_truncates_to_20(self):
        assert story.derive_topic_from_script("一" * 50) == "一" * 20

    def test_derive_topic_blank_falls_back(self):
        assert story.derive_topic_from_script("") == "自定义文稿"


class TestValidateConfigScriptMode:
    def _base(self, monkeypatch):
        monkeypatch.setattr(config, "TTS_PROVIDER", "stepfun")
        monkeypatch.setattr(config, "STEPFUN_API_KEY", "stepfun-key")
        monkeypatch.setattr(config, "MINIMAX_API_KEY", "")
        monkeypatch.setattr(config, "IMAGE_SOURCE", "minimax")
        monkeypatch.setattr(config, "UNSPLASH_ACCESS_KEY", "")

    def test_require_llm_false_allows_missing_llm_key(self, monkeypatch):
        """文稿直出模式不用 LLM：缺 LLM_API_KEY 不再致命"""
        self._base(monkeypatch)
        monkeypatch.setattr(config, "LLM_API_KEY", "")
        ok, problems, _ = config.validate_config(require_llm=False)
        assert ok
        assert not any("LLM_API_KEY" in p for p in problems)

    def test_require_llm_false_still_requires_tts_key(self, monkeypatch):
        """LLM 豁免不代表全豁免：所选 TTS 提供商缺 key 仍然致命"""
        self._base(monkeypatch)
        monkeypatch.setattr(config, "LLM_API_KEY", "")
        monkeypatch.setattr(config, "STEPFUN_API_KEY", "")
        ok, problems, _ = config.validate_config(require_llm=False)
        assert not ok
        assert any("STEPFUN_API_KEY" in p for p in problems)

    def test_default_require_llm_true_unchanged(self, monkeypatch):
        """默认行为不变：topic 模式缺 LLM_API_KEY 仍然是致命问题"""
        self._base(monkeypatch)
        monkeypatch.setattr(config, "LLM_API_KEY", "")
        ok, problems, _ = config.validate_config()
        assert not ok
        assert any("LLM_API_KEY" in p for p in problems)
