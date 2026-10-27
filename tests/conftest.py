"""pytest 共享夹具（任务 D2 新增）。

离线围栏：D2 的 TTS 时间戳通道（tts.fetch_segment_timestamps）默认随
config.TTS_TIMESTAMPS 开启，而 config 会加载 .env——本机若配置了真实
StepFun / MiniMax / 火山 Key，任何没有显式 mock 通道的用例都可能发起
真实网络请求。围栏把 generate_all_speech 的时间戳出口短路成
「拿不到时间戳」（返回 None，与线上优雅回退路径完全一致），保证既有
用例（test_tts / test_aligner / test_demo 等）在带 Key 的机器上照常离线运行。

需要验证通道真实实现的用例（tests/test_tts_timestamps.py）不经过
generate_all_speech 的这处出口，或自行装回真实现（模块导入期捕获原函数）。
"""
import pytest

import tts

_REAL_FETCH_SEGMENT_TIMESTAMPS = tts.fetch_segment_timestamps


@pytest.fixture(autouse=True)
def _offline_tts_timestamp_channel(monkeypatch):
    monkeypatch.setattr(tts, "fetch_segment_timestamps", lambda *a, **k: None)
