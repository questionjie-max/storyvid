"""llm.py：宽松 JSON 解析、懒加载客户端、chat 返回值防护"""
import pytest

import config
import llm


class TestParseJsonLoose:
    def test_plain_json(self):
        assert llm.parse_json_loose('{"a": 1}') == {"a": 1}

    def test_fenced_json(self):
        content = '```json\n{"passed": true}\n```'
        assert llm.parse_json_loose(content) == {"passed": True}

    def test_generic_fence(self):
        content = '```\n{"x": "y"}\n```'
        assert llm.parse_json_loose(content) == {"x": "y"}

    def test_prose_around_json(self):
        content = '好的，以下是结果：\n{"a": [1, 2]}\n希望有帮助'
        assert llm.parse_json_loose(content) == {"a": [1, 2]}

    def test_nested_braces(self):
        content = '说明 {"a": {"b": 2}} 结尾'
        assert llm.parse_json_loose(content) == {"a": {"b": 2}}

    def test_no_json_raises(self):
        with pytest.raises(Exception):
            llm.parse_json_loose("完全不是 JSON")


class TestGetClient:
    def setup_method(self):
        llm._client = None

    def teardown_method(self):
        llm._client = None

    def test_missing_key_raises_with_guidance(self, monkeypatch):
        monkeypatch.setattr(config, "LLM_API_KEY", "")
        with pytest.raises(RuntimeError, match="LLM_API_KEY"):
            llm.get_client()

    def test_client_created_and_cached(self, monkeypatch):
        monkeypatch.setattr(config, "LLM_API_KEY", "test-key")
        monkeypatch.setattr(config, "LLM_BASE_URL", "https://example.com/v1")
        client = llm.get_client()
        assert client is not None
        # 第二次调用应返回缓存的同一个实例
        assert llm.get_client() is client


class TestChat:
    def test_content_none_returns_empty_string(self, monkeypatch):
        """部分兼容服务内容过滤时会返回 content=None，不应直接 AttributeError"""

        class _FakeCompletions:
            def create(self, **kwargs):
                msg = type("M", (), {"content": None})()
                choice = type("C", (), {"message": msg})()
                return type("R", (), {"choices": [choice]})()

        class _FakeClient:
            def __init__(self):
                self.chat = type("Chat", (), {"completions": _FakeCompletions()})()

        monkeypatch.setattr(llm, "get_client", lambda: _FakeClient())
        assert llm.chat("你好") == ""

    def test_chat_passes_model_override(self, monkeypatch):
        captured = {}

        class _FakeCompletions:
            def create(self, **kwargs):
                captured.update(kwargs)
                msg = type("M", (), {"content": " 回复 "})()
                choice = type("C", (), {"message": msg})()
                return type("R", (), {"choices": [choice]})()

        class _FakeClient:
            def __init__(self):
                self.chat = type("Chat", (), {"completions": _FakeCompletions()})()

        monkeypatch.setattr(llm, "get_client", lambda: _FakeClient())
        result = llm.chat("你好", system="sys", model="my-model")
        assert result == "回复"  # strip 过
        assert captured["model"] == "my-model"
        assert captured["messages"][0] == {"role": "system", "content": "sys"}
