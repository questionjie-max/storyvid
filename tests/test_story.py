"""story.py：文稿切分 + LLM 调用容错"""
import pytest

import story


class TestSplitStoryIntoSegments:
    def test_split_by_paragraph_marker(self):
        text = "[段落]第一段内容足够长了啊。[段落]第二段内容也要足够长才行。"
        segs = story.split_story_into_segments(text)
        assert len(segs) == 2
        assert segs[0]["text"].startswith("第一段")
        assert segs[1]["text"].startswith("第二段")
        assert all(s["image_keyword"] == "" for s in segs)

    def test_split_by_blank_line(self):
        text = "这是第一段，字数已经超过十个字了。\n\n这是第二段，字数同样超过十个字。"
        segs = story.split_story_into_segments(text)
        assert len(segs) == 2

    def test_fallback_sentence_split_for_single_blob(self):
        text = "这是一个没有分段的完整长文本。" * 20  # 无 [段落]、无空行
        segs = story.split_story_into_segments(text)
        assert len(segs) > 1
        assert all(len(s["text"]) >= 10 for s in segs)

    def test_short_fragments_dropped(self):
        text = "短。\n\n这个段落是足够长的，超过十个字符的限制。"
        segs = story.split_story_into_segments(text)
        assert len(segs) == 1
        assert "足够长" in segs[0]["text"]


class TestGenerateImageKeywords:
    def test_keywords_mapped_in_order(self, monkeypatch):
        def fake_chat(user, system="", temperature=0.7, max_tokens=2000, model=None):
            return "ancient palace\nmysterious night\ncity skyline"

        monkeypatch.setattr(story.llm, "chat", fake_chat)
        segs = [{"text": "段落" + str(i) * 10, "image_keyword": ""} for i in range(3)]
        result = story.generate_image_keywords(segs, "测试主题")
        assert [s["image_keyword"] for s in result] == [
            "ancient palace", "mysterious night", "city skyline",
        ]

    def test_keyword_shortage_falls_back_to_topic(self, monkeypatch):
        monkeypatch.setattr(story.llm, "chat", lambda *a, **k: "only one keyword")
        segs = [{"text": "段落" + str(i) * 10, "image_keyword": ""} for i in range(2)]
        result = story.generate_image_keywords(segs, "测试主题")
        assert result[0]["image_keyword"] == "only one keyword"
        assert result[1]["image_keyword"] == "测试主题"


class TestSelfReview:
    def test_parse_plain_json(self, monkeypatch):
        monkeypatch.setattr(
            story.llm, "chat",
            lambda *a, **k: '{"passed": true, "issues": [], "severity": "low", "improved_version": ""}',
        )
        assert story.self_review_story("文案内容", "主题")["passed"] is True

    def test_parse_fenced_json(self, monkeypatch):
        monkeypatch.setattr(
            story.llm, "chat",
            lambda *a, **k: '```json\n{"passed": false, "issues": ["数据存疑"], "severity": "high", "improved_version": "改成..."}\n```',
        )
        review = story.self_review_story("文案内容", "主题")
        assert review["passed"] is False
        assert "数据存疑" in review["issues"]

    def test_parse_failure_defaults_to_pass(self, monkeypatch):
        monkeypatch.setattr(story.llm, "chat", lambda *a, **k: "完全不是JSON的回复")
        assert story.self_review_story("文案内容", "主题")["passed"] is True


class TestRetry:
    def test_retry_uses_improved_version(self, monkeypatch):
        calls = []

        def fake_generate(topic, extra=""):
            calls.append(extra)
            return "故事稿"

        reviews = iter([
            {"passed": False, "issues": ["x"], "improved_version": "改进意见"},
            {"passed": True, "issues": [], "improved_version": ""},
        ])
        monkeypatch.setattr(story, "generate_story", fake_generate)
        monkeypatch.setattr(story, "self_review_story", lambda *a: next(reviews))

        result = story.generate_story_with_retry("主题", max_retries=2)
        assert result == "故事稿"
        assert len(calls) == 2
        assert "改进意见" in calls[1]

    def test_returns_last_version_after_exhausting_retries(self, monkeypatch):
        monkeypatch.setattr(story, "generate_story", lambda t, e="": "最后一版")
        monkeypatch.setattr(
            story, "self_review_story",
            lambda *a: {"passed": False, "issues": ["x"], "improved_version": ""},
        )
        assert story.generate_story_with_retry("主题", max_retries=1) == "最后一版"
