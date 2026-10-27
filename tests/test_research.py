"""research.py：搜索容错 + 审核解析"""
import pytest

import research


class FakeDDGS:
    def __init__(self, behavior):
        self.behavior = behavior

    def text(self, query, **kwargs):
        if self.behavior == "raise":
            raise RuntimeError("网络不可达")
        return [{"title": "标题", "body": "摘要内容", "href": "https://example.com"}]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestSearchTopic:
    def test_returns_results(self, monkeypatch):
        monkeypatch.setattr(research, "DDGS", lambda: FakeDDGS("ok"))
        results = research.search_topic("主题")
        assert len(results) == 1
        assert results[0]["title"] == "标题"

    def test_network_failure_returns_empty(self, monkeypatch):
        monkeypatch.setattr(research, "DDGS", lambda: FakeDDGS("raise"))
        assert research.search_topic("主题") == []


class TestAnalyzeContent:
    def test_no_results_defaults_safe(self):
        result = research.analyze_content("主题", [])
        assert result["is_safe"] is True

    def test_parses_llm_json(self, monkeypatch):
        monkeypatch.setattr(
            research.llm, "chat",
            lambda *a, **k: '```json\n{"is_hot": true, "is_safe": false, "risk_level": "high", "reason": "敏感", "suggestions": "换个主题"}\n```',
        )
        fake_results = [{"title": "t", "body": "b"}]
        result = research.analyze_content("主题", fake_results)
        assert result["is_safe"] is False

    def test_llm_failure_defaults_safe(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("LLM 不可用")
        monkeypatch.setattr(research.llm, "chat", boom)
        fake_results = [{"title": "t", "body": "b"}]
        result = research.analyze_content("主题", fake_results)
        assert result["is_safe"] is True
