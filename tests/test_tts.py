"""tts.py：时长探测、提供商分发、单段失败容忍、响度归一、火山引擎 volc 接入"""
import base64
import binascii
import hashlib
import os
import re
import subprocess
import types
import uuid

import pytest

import config
import tts

# conftest 的离线围栏会把 tts.fetch_segment_timestamps 短路成 None（防出网）。
# edge 通道的用例要验证真实接线，这里在「夹具生效前」捕获原函数，用例内装回。
_REAL_FETCH_SEGMENT_TIMESTAMPS = tts.fetch_segment_timestamps


class _FakeJsonResponse:
    """模拟 requests.Response：本套件只用 json() 读响应体"""

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def _load_fresh_config(monkeypatch, env: dict):
    """以独立模块身份重新执行 config.py（屏蔽 .env），env 为要覆盖的环境变量。

    避免 importlib.reload 污染全局 config 模块状态，也让用例不受 .env 内容影响。
    """
    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: None)  # 屏蔽 .env
    for key in ("TARGET_LUFS", "LOUDNESS_NORM"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    path = os.path.join(os.path.dirname(config.__file__), "config.py")
    module = types.ModuleType("config_fresh_for_test")
    module.__file__ = path  # config.py 内部用 __file__ 定位项目目录
    with open(path, encoding="utf-8") as f:
        exec(compile(f.read(), path, "exec"), module.__dict__)
    return module


def _make_sine_mp3(path, freq=440, seconds=3, gain_db=-20):
    """用 ffmpeg 生成指定增益的正弦 mp3；imageio-ffmpeg 不可用时跳过所属测试"""
    import imageio_ffmpeg
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run(
        [ffmpeg, "-y", "-hide_banner",
         "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
         "-af", f"volume={gain_db}dB",
         "-ar", "24000", "-ac", "1", "-q:a", "4", str(path)],
        capture_output=True, check=True,
    )
    return str(path)


def _mean_volume_db(path) -> float:
    """用 ffmpeg volumedetect 测整段平均音量（RMS, dB）"""
    import imageio_ffmpeg
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(path),
         "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    m = re.search(r"mean_volume:\s*(-?[\d.]+)\s*dB", proc.stderr)
    assert m, f"volumedetect 未输出 mean_volume:\n{proc.stderr[-500:]}"
    return float(m.group(1))


class TestAudioDuration:
    def test_real_mp3_duration(self, tmp_path):
        try:
            import imageio_ffmpeg
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            pytest.skip("imageio-ffmpeg 不可用")

        mp3 = tmp_path / "one_second.mp3"
        import subprocess
        subprocess.run(
            [ffmpeg, "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
             "-t", "1", "-q:a", "9", str(mp3)],
            capture_output=True, check=True,
        )
        duration = tts.get_audio_duration(str(mp3))
        assert 0.8 <= duration <= 1.6

    def test_missing_file_falls_back_to_5s(self, tmp_path):
        assert tts.get_audio_duration(str(tmp_path / "nope.mp3")) == 5.0


class TestProviderDispatch:
    def test_minimax_provider_routes_to_minimax(self, monkeypatch, tmp_path):
        calls = {}
        monkeypatch.setattr(tts, "generate_speech_minimax",
                            lambda text, path, voice="": calls.setdefault("mm", (text, path, voice)))
        monkeypatch.setattr(tts, "generate_speech_stepfun",
                            lambda text, path, voice="": (_ for _ in ()).throw(AssertionError("不应调用 stepfun")))
        tts.generate_speech("文本", str(tmp_path / "a.mp3"), provider="minimax", voice="v1")
        assert calls["mm"] == ("文本", str(tmp_path / "a.mp3"), "v1")

    def test_default_provider_from_config(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "TTS_PROVIDER", "minimax")
        called = {}
        monkeypatch.setattr(tts, "generate_speech_minimax",
                            lambda text, path, voice="": called.setdefault("hit", True))
        tts.generate_speech("文本", str(tmp_path / "b.mp3"))
        assert called.get("hit")

    def test_volc_provider_routes_to_volc(self, monkeypatch, tmp_path):
        """provider=volc 应分发到 generate_speech_volc，且不走其它提供商"""
        calls = {}
        monkeypatch.setattr(tts, "generate_speech_volc",
                            lambda text, path, voice="": calls.setdefault("volc", (text, path, voice)))
        monkeypatch.setattr(tts, "generate_speech_stepfun",
                            lambda text, path, voice="": (_ for _ in ()).throw(AssertionError("不应调用 stepfun")))
        monkeypatch.setattr(tts, "generate_speech_minimax",
                            lambda text, path, voice="": (_ for _ in ()).throw(AssertionError("不应调用 minimax")))
        tts.generate_speech("文本", str(tmp_path / "c.mp3"), provider="volc", voice="BV002_streaming")
        assert calls["volc"] == ("文本", str(tmp_path / "c.mp3"), "BV002_streaming")


def _volc_cfg(monkeypatch):
    """固定 volc 相关配置项，屏蔽 .env 内容对用例的影响"""
    monkeypatch.setattr(config, "VOLC_TTS_APPID", "test-appid")
    monkeypatch.setattr(config, "VOLC_TTS_TOKEN", "test-token")
    monkeypatch.setattr(config, "VOLC_TTS_API_URL", "https://openspeech.bytedance.com/api/v1/tts")
    monkeypatch.setattr(config, "VOLC_TTS_CLUSTER", "volcano_tts")
    monkeypatch.setattr(config, "VOLC_TTS_VOICE_TYPE", "BV001_streaming")
    monkeypatch.setattr(config, "VOLC_TTS_ENCODING", "mp3")
    monkeypatch.setattr(config, "VOLC_TTS_SPEED_RATIO", 1.0)
    monkeypatch.setattr(config, "VOLC_TTS_VOLUME_RATIO", 1.0)


class TestVolcTTS:
    """火山引擎（豆包）TTS：payload 组装、鉴权、响应解析（base64 音频）"""

    def test_payload_assembly_and_auth_header(self, monkeypatch, tmp_path):
        _volc_cfg(monkeypatch)
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured.update(url=url, headers=headers, json=json, timeout=timeout)
            audio = base64.b64encode(b"fake-mp3-bytes").decode()
            return _FakeJsonResponse({"reqid": "r1", "code": 3000,
                                      "message": "success", "data": audio})

        monkeypatch.setattr(tts.requests, "post", fake_post)
        out = tmp_path / "seg.mp3"
        tts.generate_speech_volc("你好，世界", str(out), voice="BV002_streaming")

        # 请求目标与鉴权（官方格式：Bearer 与 token 用分号分隔）
        assert captured["url"] == "https://openspeech.bytedance.com/api/v1/tts"
        assert captured["headers"]["Authorization"] == "Bearer; test-token"
        assert captured["headers"]["Content-Type"] == "application/json"
        assert captured["timeout"] == 60

        # payload 四大块：app / user / audio / request
        payload = captured["json"]
        assert payload["app"] == {"appid": "test-appid", "token": "test-token",
                                  "cluster": "volcano_tts"}
        assert payload["user"] == {"uid": "storyvid"}
        assert payload["audio"] == {"voice_type": "BV002_streaming", "encoding": "mp3",
                                    "speed_ratio": 1.0, "volume_ratio": 1.0}
        req = payload["request"]
        assert req["text"] == "你好，世界"
        assert req["text_type"] == "plain"
        assert req["operation"] == "query"  # 非流式一次性合成
        uuid.UUID(req["reqid"])  # reqid 必须是合法唯一标识（官方要求每次合成唯一）

        # 响应解析：base64 音频解码写入文件
        assert out.read_bytes() == b"fake-mp3-bytes"

    def test_voice_falls_back_to_config(self, monkeypatch, tmp_path):
        _volc_cfg(monkeypatch)
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured.update(json=json)
            return _FakeJsonResponse({"code": 3000, "message": "success",
                                      "data": base64.b64encode(b"x").decode()})

        monkeypatch.setattr(tts.requests, "post", fake_post)
        tts.generate_speech_volc("文本", str(tmp_path / "v.mp3"), voice="")
        assert captured["json"]["audio"]["voice_type"] == "BV001_streaming"

    def test_stepfun_voice_name_falls_back_with_warning(self, monkeypatch, tmp_path, capsys):
        """Web 音色下拉的 StepFun 音色名（cixingnansheng 等，不含下划线）不是
        合法 volc voice_type——原样发送 volc 必报「音色无效/未授权」（B 队复核
        确认的缺陷，见 overnight_reports/A5_volc_tts.md「遗留」）。B6 修复：
        告警并回退 VOLC_TTS_VOICE_TYPE，而不是让每次合成都失败。"""
        _volc_cfg(monkeypatch)
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured.update(json=json)
            return _FakeJsonResponse({"code": 3000, "message": "success",
                                      "data": base64.b64encode(b"x").decode()})

        monkeypatch.setattr(tts.requests, "post", fake_post)
        out = tts.generate_speech_volc("文本", str(tmp_path / "v.mp3"),
                                       voice="cixingnansheng")
        assert out == str(tmp_path / "v.mp3")
        assert captured["json"]["audio"]["voice_type"] == "BV001_streaming"
        warn = capsys.readouterr().out
        assert "不是火山引擎 voice_type 格式" in warn
        assert "cixingnansheng" in warn

    def test_volc_style_voice_with_underscore_passes_through(self, monkeypatch, tmp_path, capsys):
        """含下划线的音色视为 volc voice_type 格式（如 BV*_streaming、豆包 bigtts
        命名），原样透传且不告警"""
        _volc_cfg(monkeypatch)
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured.update(json=json)
            return _FakeJsonResponse({"code": 3000, "message": "success",
                                      "data": base64.b64encode(b"x").decode()})

        monkeypatch.setattr(tts.requests, "post", fake_post)
        tts.generate_speech_volc("文本", str(tmp_path / "v.mp3"),
                                 voice="zh_female_cancan_mars_bigtts")
        assert captured["json"]["audio"]["voice_type"] == "zh_female_cancan_mars_bigtts"
        assert "不是火山引擎 voice_type 格式" not in capsys.readouterr().out

    def test_audio_base64_decode_exact_bytes(self, monkeypatch, tmp_path):
        """响应 data 为 base64；解码后字节必须与原文一致（含多字节/二进制头）"""
        _volc_cfg(monkeypatch)
        raw = bytes([0xFF, 0xFB, 0x90, 0x00]) + "音频".encode("utf-8")

        def fake_post(url, headers=None, json=None, timeout=None):
            return _FakeJsonResponse({"code": 3000, "message": "success",
                                      "data": base64.b64encode(raw).decode()})

        monkeypatch.setattr(tts.requests, "post", fake_post)
        out = tmp_path / "seg.mp3"
        tts.generate_speech_volc("文本", str(out))
        assert out.read_bytes() == raw

    def test_missing_appid_or_token_raises_clear_error(self, monkeypatch, tmp_path):
        _volc_cfg(monkeypatch)
        monkeypatch.setattr(config, "VOLC_TTS_APPID", "")
        with pytest.raises(ValueError, match="VOLC_TTS_APPID"):
            tts.generate_speech_volc("文本", str(tmp_path / "a.mp3"))

        monkeypatch.setattr(config, "VOLC_TTS_APPID", "test-appid")
        monkeypatch.setattr(config, "VOLC_TTS_TOKEN", "")
        with pytest.raises(ValueError, match="VOLC_TTS_TOKEN"):
            tts.generate_speech_volc("文本", str(tmp_path / "b.mp3"))

    def test_api_error_code_raises_runtime_error(self, monkeypatch, tmp_path):
        """code != 3000（如 3001 参数错误）应抛出带 code 与 message 的 RuntimeError"""
        _volc_cfg(monkeypatch)
        monkeypatch.setattr(tts.requests, "post",
                            lambda *a, **k: _FakeJsonResponse(
                                {"code": 3001, "message": "invalid parameter"}))
        with pytest.raises(RuntimeError, match=r"3001.*invalid parameter"):
            tts.generate_speech_volc("文本", str(tmp_path / "a.mp3"))

    def test_success_without_audio_data_raises(self, monkeypatch, tmp_path):
        _volc_cfg(monkeypatch)
        monkeypatch.setattr(tts.requests, "post",
                            lambda *a, **k: _FakeJsonResponse(
                                {"code": 3000, "message": "success", "data": ""}))
        with pytest.raises(RuntimeError, match="data"):
            tts.generate_speech_volc("文本", str(tmp_path / "a.mp3"))

    def test_invalid_base64_raises(self, monkeypatch, tmp_path):
        """data 不是合法 base64 时报解码失败，而不是写坏文件"""
        _volc_cfg(monkeypatch)
        monkeypatch.setattr(tts.requests, "post",
                            lambda *a, **k: _FakeJsonResponse(
                                {"code": 3000, "message": "success", "data": "!!!not-base64!!!"}))
        out = tmp_path / "a.mp3"
        with pytest.raises(RuntimeError, match="base64"):
            tts.generate_speech_volc("文本", str(out))
        assert not out.exists()  # 不应留下半截文件

    def test_long_text_truncated_within_byte_limit(self, monkeypatch, tmp_path):
        """官方限制单次约 1024 字节（UTF-8）：超长文本截断并留省略号"""
        _volc_cfg(monkeypatch)
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured.update(json=json)
            return _FakeJsonResponse({"code": 3000, "message": "success",
                                      "data": base64.b64encode(b"x").decode()})

        monkeypatch.setattr(tts.requests, "post", fake_post)
        long_text = "长" * 400  # 400 汉字 = 1200 字节，超限
        tts.generate_speech_volc(long_text, str(tmp_path / "a.mp3"))
        sent = captured["json"]["request"]["text"]
        assert len(sent.encode("utf-8")) <= 1000
        assert sent.endswith("...")
        assert sent[:-3] != long_text  # 确实发生了截断


class TestMinimaxHTTP:
    """MiniMax HTTP 直连：payload 组装与响应解析（data.audio 为 hex 编码音频）"""

    def _minimax_cfg(self, monkeypatch):
        monkeypatch.setattr(config, "MINIMAX_API_KEY", "mm-key")
        monkeypatch.setattr(config, "MINIMAX_BASE_URL", "https://api.minimaxi.com")
        monkeypatch.setattr(config, "MINIMAX_TTS_MODEL", "speech-01-hd")
        monkeypatch.setattr(config, "MINIMAX_TTS_VOICE_ID", "male-qn-jingying")
        monkeypatch.setattr(config, "MINIMAX_TTS_SPEED", 1.15)

    def test_payload_and_hex_audio_decode(self, monkeypatch, tmp_path):
        self._minimax_cfg(monkeypatch)
        captured = {}
        raw = bytes([0xFF, 0xFB, 0x90, 0x00]) + b"minimax-fake-mp3"

        def fake_post(url, headers=None, json=None, timeout=None):
            captured.update(url=url, headers=headers, json=json, timeout=timeout)
            return _FakeJsonResponse({
                "base_resp": {"status_code": 0, "status_msg": "success"},
                "data": {"audio": binascii.hexlify(raw).decode()},
            })

        monkeypatch.setattr(tts.requests, "post", fake_post)
        out = tmp_path / "seg.mp3"
        tts.generate_speech_minimax("你好世界", str(out), voice="female-shaonv")

        assert captured["url"] == "https://api.minimaxi.com/v1/t2a_v2"
        assert captured["headers"]["Authorization"] == "Bearer mm-key"
        payload = captured["json"]
        assert payload["model"] == "speech-01-hd"
        assert payload["text"] == "你好世界"
        assert payload["stream"] is False
        assert payload["voice_setting"]["voice_id"] == "female-shaonv"
        assert payload["audio_setting"]["format"] == "mp3"

        # hex 音频解码：文件内容与原始字节一致
        assert out.read_bytes() == raw

    def test_api_error_raises(self, monkeypatch, tmp_path):
        self._minimax_cfg(monkeypatch)
        monkeypatch.setattr(tts.requests, "post",
                            lambda *a, **k: _FakeJsonResponse(
                                {"base_resp": {"status_code": 1004, "status_msg": "auth failed"}}))
        with pytest.raises(RuntimeError, match="auth failed"):
            tts.generate_speech_minimax("文本", str(tmp_path / "a.mp3"))


class TestGenerateAllSpeech:
    def test_single_failure_does_not_break_others(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(config, "TEMP_DIR", str(tmp_path))
        # 坏段失败会触发自动重试（退避 2s/8s），换成不真实等待的替身时钟
        monkeypatch.setattr(tts, "time", _FakeTime())

        def fake_generate_speech(text, output_path, provider="", voice=""):
            if "坏段" in text:
                raise RuntimeError("TTS 服务超时")

        monkeypatch.setattr(tts, "generate_speech", fake_generate_speech)

        segments = [
            {"text": "第一段正常文本内容足够长。", "duration": 0, "image_keyword": ""},
            {"text": "第二段是坏段会失败。", "duration": 0, "image_keyword": ""},
            {"text": "第三段也正常没问题啊。", "duration": 0, "image_keyword": ""},
        ]
        result = tts.generate_all_speech(segments)
        assert len(result) == 3
        failed = [s for s in result if s.get("audio_error")]
        ok = [s for s in result if not s.get("audio_error")]
        assert len(failed) == 1
        assert "坏段" in failed[0]["text"]
        assert failed[0]["duration"] == 5.0
        assert failed[0]["audio_path"] == ""
        assert all(s["audio_path"] for s in ok)


class TestLoudnessNorm:
    def test_norm_narrows_rms_gap(self, tmp_path):
        """两段不同音量的 sine mp3 归一后 RMS 差距明显缩小（核心验收）"""
        try:
            _make_sine_mp3(tmp_path / "loud.mp3", gain_db=-6)
            _make_sine_mp3(tmp_path / "quiet.mp3", gain_db=-30)
        except Exception:
            pytest.skip("imageio-ffmpeg 不可用")

        gap_before = abs(_mean_volume_db(tmp_path / "loud.mp3") - _mean_volume_db(tmp_path / "quiet.mp3"))
        assert gap_before > 15, f"测试前置条件不成立：两段差距仅 {gap_before:.1f} dB"

        tts.normalize_loudness(str(tmp_path / "loud.mp3"))
        tts.normalize_loudness(str(tmp_path / "quiet.mp3"))

        loud_after = _mean_volume_db(tmp_path / "loud.mp3")
        quiet_after = _mean_volume_db(tmp_path / "quiet.mp3")
        gap_after = abs(loud_after - quiet_after)
        assert gap_after < 3.0, f"归一后差距仍有 {gap_after:.1f} dB（loud={loud_after:.1f}, quiet={quiet_after:.1f}）"
        # 都应被拉到目标响度附近（TARGET_LUFS=-16；sine 的 LUFS≈RMS，允许少量误差）
        assert -20 < loud_after < -12
        assert -20 < quiet_after < -12
        # 不留临时文件
        assert not list(tmp_path.glob("*.loudnorm.mp3"))

    def test_norm_failure_keeps_original(self, tmp_path, monkeypatch):
        """归一失败（ffmpeg 不可用）时保留原文件、不抛异常、不留临时文件"""
        original = tmp_path / "seg.mp3"
        _make_sine_mp3(original, gain_db=-12)
        before = original.read_bytes()

        monkeypatch.setattr(tts, "_get_ffmpeg", lambda: "/nonexistent/ffmpeg")
        result = tts.normalize_loudness(str(original))

        assert result == str(original)
        assert original.read_bytes() == before  # 内容未变
        assert not list(tmp_path.glob("*.loudnorm.mp3"))  # 无残留临时文件

    def test_norm_disabled_by_config(self, tmp_path, monkeypatch):
        """LOUDNESS_NORM 关闭时不调用 ffmpeg，文件原样返回"""
        mp3 = tmp_path / "seg.mp3"
        _make_sine_mp3(mp3, gain_db=-12)
        before = mp3.read_bytes()

        monkeypatch.setattr(config, "LOUDNESS_NORM", False)
        monkeypatch.setattr(tts, "_get_ffmpeg",
                            lambda: (_ for _ in ()).throw(AssertionError("关闭后不应调用 ffmpeg")))
        assert tts.normalize_loudness(str(mp3)) == str(mp3)
        assert mp3.read_bytes() == before

    def test_norm_skips_missing_file(self, tmp_path):
        assert tts.normalize_loudness(str(tmp_path / "nope.mp3")) == str(tmp_path / "nope.mp3")

    def test_target_lufs_config_env(self, monkeypatch):
        """TARGET_LUFS 默认 -16、LOUDNESS_NORM 默认开，均可被环境变量覆盖"""
        cfg = _load_fresh_config(monkeypatch, {})
        assert cfg.TARGET_LUFS == -16.0
        assert cfg.LOUDNESS_NORM is True

        cfg = _load_fresh_config(monkeypatch, {"TARGET_LUFS": "-23", "LOUDNESS_NORM": "0"})
        assert cfg.TARGET_LUFS == -23.0
        assert cfg.LOUDNESS_NORM is False

    def test_target_lufs_config_invalid_value(self, monkeypatch):
        """TARGET_LUFS 非法值回退默认 -16，不让 import 崩溃"""
        cfg = _load_fresh_config(monkeypatch, {"TARGET_LUFS": "not-a-number"})
        assert cfg.TARGET_LUFS == -16.0

    def test_extract_loudnorm_stats(self):
        stderr = (
            '[Parsed_loudnorm_0 @ 0x...] \n{\n\t"input_i" : "-41.71",\n'
            '\t"input_tp" : "-37.41",\n\t"input_lra" : "0.00",\n'
            '\t"input_thresh" : "-51.71",\n\t"output_i" : "-16.01",\n'
            '\t"target_offset" : "0.01"\n}\n'
        )
        stats = tts._extract_loudnorm_stats(stderr)
        assert stats is not None
        assert stats["input_i"] == "-41.71"
        assert stats["target_offset"] == "0.01"

    def test_extract_loudnorm_stats_silence_returns_none(self):
        assert tts._extract_loudnorm_stats('{"input_i" : "-inf", "input_tp" : "-inf", '
                                           '"input_lra" : "0.00", "input_thresh" : "-inf", '
                                           '"target_offset" : "nan"}') is None
        assert tts._extract_loudnorm_stats("no json here") is None


class TestGenerateAllSpeechLoudnessHook:
    def test_generate_all_speech_normalizes_each_segment(self, tmp_path, monkeypatch):
        """generate_all_speech 应对每个成功段调用响度归一"""
        monkeypatch.setattr(config, "PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(config, "TEMP_DIR", str(tmp_path))
        # 坏段失败会触发自动重试（退避 2s/8s），换成不真实等待的替身时钟
        monkeypatch.setattr(tts, "time", _FakeTime())

        def fake_generate_speech(text, output_path, provider="", voice=""):
            if "坏段" in text:
                raise RuntimeError("TTS 服务超时")
            # 不写真实文件：本用例只验证钩子接线，避免无效音频触发 moviepy 噪音

        monkeypatch.setattr(tts, "generate_speech", fake_generate_speech)
        calls = []
        monkeypatch.setattr(tts, "normalize_loudness", lambda p: calls.append(p) or p)

        segments = [
            {"text": "第一段正常文本内容足够长。", "duration": 0, "image_keyword": ""},
            {"text": "第二段是坏段会失败。", "duration": 0, "image_keyword": ""},
            {"text": "第三段也正常没问题啊。", "duration": 0, "image_keyword": ""},
        ]
        result = tts.generate_all_speech(segments)
        expected = sorted(os.path.join(str(tmp_path), f"segment_{i:03d}.mp3") for i in (0, 2))
        # 并行完成顺序不定，按排序比较；失败段（segment_001）不应出现在 calls 中
        assert sorted(calls) == expected
        failed = [s for s in result if s.get("audio_error")]
        assert len(failed) == 1  # 失败段不应触发归一


class _FakeTime:
    """替身 time 模块：记录 sleep 调用参数，不真实等待（验证退避 2s/8s）"""

    def __init__(self):
        self.sleeps: list[float] = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)


class TestGenerateAllSpeechResume:
    """断点续跑：已存在且非空、文本快照一致的 segment_XXX.mp3 直接复用，
    不再花 API 调用；同日重跑文稿变了（快照不符）必须重新合成"""

    @pytest.fixture(autouse=True)
    def _no_audio_probe(self, monkeypatch):
        """替身时长探测：假音频字节喂给 moviepy 会产生无法回收进程的告警，
        单元层直接固定返回值；真实文件的时长读取另有 test_existing_real_mp3_reused"""
        monkeypatch.setattr(tts, "get_audio_duration", lambda p: 3.0)

    def test_existing_segment_file_is_reused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(config, "TEMP_DIR", str(tmp_path))

        seg0_text = "第一段已有成品音频。"
        pre = tmp_path / "segment_000.mp3"
        pre.write_bytes(b"pre-existing-audio-bytes")
        # 文本快照边车：与本次段落文本一致才可复用（断点续跑判据，见 _audio_matches_text）
        pre.with_suffix(".mp3.sha1").write_text(
            hashlib.sha1(seg0_text.encode("utf-8")).hexdigest(), encoding="utf-8")

        gen_calls = []
        norm_calls = []

        def fake_generate_speech(text, output_path, provider="", voice=""):
            gen_calls.append(output_path)
            with open(output_path, "wb"):
                pass  # 写空文件模拟生成产物

        monkeypatch.setattr(tts, "generate_speech", fake_generate_speech)
        monkeypatch.setattr(tts, "normalize_loudness",
                            lambda p: norm_calls.append(p) or p)

        segments = [
            {"text": "第一段已有成品音频。", "duration": 0, "image_keyword": ""},
            {"text": "第二段需要现场生成。", "duration": 0, "image_keyword": ""},
        ]
        result = tts.generate_all_speech(segments)

        # 复用段：内容未被覆盖、不再触发生成与响度归一、打上复用标记
        assert pre.read_bytes() == b"pre-existing-audio-bytes"
        assert str(pre) not in gen_calls
        assert str(pre) not in norm_calls
        assert result[0]["audio_reused"] is True
        assert result[0]["audio_path"] == str(pre)
        assert result[0]["duration"] == 3.0  # 复用段仍回填时长（供字幕/合成用）
        assert not result[0]["audio_error"]
        # 缺失段：照常生成，不打复用标记
        assert result[1].get("audio_reused") is None
        assert gen_calls == [os.path.join(str(tmp_path), "segment_001.mp3")]

    def test_empty_segment_file_is_regenerated(self, tmp_path, monkeypatch):
        """0 字节文件视为上次中断的残留，必须重新生成而不是复用"""
        monkeypatch.setattr(config, "PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(config, "TEMP_DIR", str(tmp_path))

        empty = tmp_path / "segment_000.mp3"
        empty.write_bytes(b"")

        gen_calls = []

        def fake_generate_speech(text, output_path, provider="", voice=""):
            gen_calls.append(output_path)
            with open(output_path, "wb") as f:
                f.write(b"fresh audio")

        monkeypatch.setattr(tts, "generate_speech", fake_generate_speech)
        monkeypatch.setattr(tts, "normalize_loudness", lambda p: p)

        segments = [{"text": "上次中断留下的空文件段。", "duration": 0, "image_keyword": ""}]
        result = tts.generate_all_speech(segments)

        assert gen_calls == [str(empty)]  # 仍然调用了生成
        assert empty.read_bytes() == b"fresh audio"  # 且覆盖了空文件
        assert result[0].get("audio_reused") is None


class TestGenerateAllSpeechResumeRealAudio:
    """真实 mp3 预置时的复用：读出真实时长、不生成、不归一（无替身探测）"""

    def test_existing_real_mp3_reused_with_real_duration(self, tmp_path, monkeypatch):
        try:
            pre = tmp_path / "segment_000.mp3"
            _make_sine_mp3(pre, seconds=2)
        except Exception:
            pytest.skip("imageio-ffmpeg 不可用")
        monkeypatch.setattr(config, "PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(config, "TEMP_DIR", str(tmp_path))
        # 文本快照边车：与本次段落文本一致才可复用（见 _audio_matches_text）
        pre.with_suffix(".mp3.sha1").write_text(
            hashlib.sha1("真实音频复用段。".encode("utf-8")).hexdigest(), encoding="utf-8")
        monkeypatch.setattr(tts, "generate_speech",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("已有成品音频不应再生成")))
        monkeypatch.setattr(tts, "normalize_loudness",
                            lambda p: (_ for _ in ()).throw(
                                AssertionError("复用段不应再做响度归一")))

        result = tts.generate_all_speech(
            [{"text": "真实音频复用段。", "duration": 0, "image_keyword": ""}])

        assert result[0]["audio_reused"] is True
        assert not result[0]["audio_error"]
        assert 1.5 <= result[0]["duration"] <= 3.0  # 真实时长来自 mutagen/ffmpeg 探测

    def test_stale_text_audio_is_regenerated(self, tmp_path, monkeypatch):
        """同日重跑文稿变了(快照哈希不符)：旧音频不得复用,重新合成并写新快照"""
        monkeypatch.setattr(config, "PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(config, "TEMP_DIR", str(tmp_path))

        stale = tmp_path / "segment_000.mp3"
        stale.write_bytes(b"yesterdays-audio")
        stale.with_suffix(".mp3.sha1").write_text(
            hashlib.sha1("上一次运行的旧文稿。".encode("utf-8")).hexdigest(), encoding="utf-8")

        gen_calls = []

        def fake_generate_speech(text, output_path, provider="", voice=""):
            gen_calls.append((text, output_path))
            with open(output_path, "wb") as f:
                f.write(b"fresh audio for new text")

        monkeypatch.setattr(tts, "generate_speech", fake_generate_speech)
        monkeypatch.setattr(tts, "normalize_loudness", lambda p: p)

        segments = [{"text": "本次运行的新文稿。", "duration": 0, "image_keyword": ""}]
        result = tts.generate_all_speech(segments)

        assert gen_calls and gen_calls[0][0] == "本次运行的新文稿。"
        assert result[0].get("audio_reused") is None  # 不是复用,是重新生成
        # 新音频带上了本次文本的快照,下次同文本续跑才能复用
        assert stale.with_suffix(".mp3.sha1").read_text(encoding="utf-8") == \
            hashlib.sha1("本次运行的新文稿。".encode("utf-8")).hexdigest()

    def test_legacy_audio_without_snapshot_regenerated(self, tmp_path, monkeypatch):
        """本修复之前生成的音频没有快照:保守起见不复用,重新合成一次"""
        monkeypatch.setattr(config, "PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(config, "TEMP_DIR", str(tmp_path))

        legacy = tmp_path / "segment_000.mp3"
        legacy.write_bytes(b"legacy-audio-no-snapshot")

        gen_calls = []

        def fake_generate_speech(text, output_path, provider="", voice=""):
            gen_calls.append(output_path)
            with open(output_path, "wb") as f:
                f.write(b"fresh")

        monkeypatch.setattr(tts, "generate_speech", fake_generate_speech)
        monkeypatch.setattr(tts, "normalize_loudness", lambda p: p)

        result = tts.generate_all_speech(
            [{"text": "任意文本。", "duration": 0, "image_keyword": ""}])
        assert gen_calls == [str(legacy)]
        assert result[0].get("audio_reused") is None


class TestGenerateAllSpeechRetry:
    """单段失败自动重试 2 次（退避 2s/8s），仍失败才标记跳过"""

    @pytest.fixture(autouse=True)
    def _no_audio_probe(self, monkeypatch):
        """替身时长探测：避免假音频字节触发 moviepy 读文件（理由同 Resume 类）"""
        monkeypatch.setattr(tts, "get_audio_duration", lambda p: 3.0)

    def test_retry_then_success(self, tmp_path, monkeypatch):
        """前 2 次失败、第 3 次成功：段正常产出，退避顺序为 2s、8s"""
        monkeypatch.setattr(config, "PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(config, "TEMP_DIR", str(tmp_path))
        fake_time = _FakeTime()
        monkeypatch.setattr(tts, "time", fake_time)

        attempts = {"count": 0}

        def flaky_generate_speech(text, output_path, provider="", voice=""):
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise RuntimeError("TTS 暂时超时")
            with open(output_path, "wb"):
                pass

        monkeypatch.setattr(tts, "generate_speech", flaky_generate_speech)
        monkeypatch.setattr(tts, "normalize_loudness", lambda p: p)

        segments = [{"text": "前两次失败第三次成功的一段。", "duration": 0, "image_keyword": ""}]
        result = tts.generate_all_speech(segments)

        assert attempts["count"] == 3  # 首次尝试 + 重试 2 次
        assert fake_time.sleeps == [2.0, 8.0]  # 退避 2s / 8s
        assert not result[0]["audio_error"]
        assert result[0]["audio_path"] == os.path.join(str(tmp_path), "segment_000.mp3")

    def test_retry_exhausted_marks_skipped(self, tmp_path, monkeypatch):
        """重试 2 次仍失败：该段标记跳过（无音频/5s 兜底），好段不受影响"""
        monkeypatch.setattr(config, "PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(config, "TEMP_DIR", str(tmp_path))
        fake_time = _FakeTime()
        monkeypatch.setattr(tts, "time", fake_time)

        calls = {"bad": 0, "good": 0}

        def fake_generate_speech(text, output_path, provider="", voice=""):
            if "坏段" in text:
                calls["bad"] += 1
                raise RuntimeError("TTS 服务不可用")
            calls["good"] += 1
            with open(output_path, "wb"):
                pass

        monkeypatch.setattr(tts, "generate_speech", fake_generate_speech)
        monkeypatch.setattr(tts, "normalize_loudness", lambda p: p)

        segments = [
            {"text": "好段正常生成。", "duration": 0, "image_keyword": ""},
            {"text": "坏段一直失败。", "duration": 0, "image_keyword": ""},
        ]
        result = tts.generate_all_speech(segments)

        assert calls["bad"] == 3  # 恰好重试 2 次（共 3 次尝试）后放弃
        assert fake_time.sleeps == [2.0, 8.0]
        bad = next(s for s in result if "坏段" in s["text"])
        assert bad["audio_error"]  # 标记跳过
        assert bad["audio_path"] == ""
        assert bad["duration"] == 5.0
        assert bad.get("audio_reused") is None
        good = next(s for s in result if "坏段" not in s["text"])
        assert good["audio_path"]  # 好段不受坏段影响
        assert not good["audio_error"]


# ---------------------------------------------------------------------
# Edge TTS provider（免费无 Key，音频流自带词级时间戳）
# ---------------------------------------------------------------------

class FakeEdgeCommunicate:
    """edge_tts.Communicate 的离线替身：产出一段音频 + 两条词边界"""

    calls: list = []

    def __init__(self, text, voice, rate=None, boundary=None):
        FakeEdgeCommunicate.calls.append(
            {"text": text, "voice": voice, "rate": rate, "boundary": boundary}
        )

    async def stream(self):
        yield {"type": "audio", "data": b"RIFFfake-audio"}
        yield {"type": "WordBoundary", "offset": 10_000_000, "duration": 3_000_000, "text": "你好"}
        yield {"type": "WordBoundary", "offset": 13_000_000, "duration": 4_000_000, "text": "世界"}


class FakeEdgeNoAudio(FakeEdgeCommunicate):
    async def stream(self):
        yield {"type": "WordBoundary", "offset": 0, "duration": 1_000_000, "text": "空"}


class TestEdgeProvider:
    def test_dispatch_routes_to_edge(self, monkeypatch, tmp_path):
        calls = {}

        def fake_edge(text, path, voice=""):
            calls["hit"] = (text, path, voice)
            return path

        def fail_stepfun(*a, **k):
            raise AssertionError("不应调用 stepfun")

        monkeypatch.setattr(tts, "generate_speech_edge", fake_edge)
        monkeypatch.setattr(tts, "generate_speech_stepfun", fail_stepfun)
        tts.generate_speech("文本", str(tmp_path / "e.mp3"), provider="edge", voice="v1")
        assert calls["hit"] == ("文本", str(tmp_path / "e.mp3"), "v1")

    def test_edge_collects_words_and_audio(self, monkeypatch, tmp_path):
        import edge_tts
        monkeypatch.setattr(edge_tts, "Communicate", FakeEdgeCommunicate)
        monkeypatch.setattr(config, "EDGE_TTS_VOICE", "zh-CN-TestVoice")
        monkeypatch.setattr(config, "TTS_SPEED", 1.15)

        out = tmp_path / "seg_000.mp3"
        result = tts.generate_speech_edge("你好世界", str(out))

        assert result == str(out)
        assert out.read_bytes() == b"RIFFfake-audio"
        assert FakeEdgeCommunicate.calls[-1] == {
            "text": "你好世界", "voice": "zh-CN-TestVoice", "rate": "+15%",
            "boundary": "WordBoundary",
        }
        words = tts._pop_edge_words(str(out))
        assert words[0]["word"] == "你好"
        assert words[0]["start"] == pytest.approx(1.0)
        assert words[0]["end"] == pytest.approx(1.3)
        assert words[1]["word"] == "世界"
        assert words[1]["start"] == pytest.approx(1.3)
        assert words[1]["end"] == pytest.approx(1.7)
        assert tts._pop_edge_words(str(out)) is None  # 取走即清除

    def test_edge_empty_audio_raises(self, monkeypatch, tmp_path):
        import edge_tts
        monkeypatch.setattr(edge_tts, "Communicate", FakeEdgeNoAudio)
        with pytest.raises(RuntimeError, match="未返回音频"):
            tts.generate_speech_edge("空", str(tmp_path / "empty.mp3"))

    def test_fetch_timestamps_edge_pops_stash(self, monkeypatch, tmp_path):
        import edge_tts
        # 装回被 conftest 围栏短路掉的真实通道入口
        monkeypatch.setattr(tts, "fetch_segment_timestamps", _REAL_FETCH_SEGMENT_TIMESTAMPS)
        monkeypatch.setattr(edge_tts, "Communicate", FakeEdgeCommunicate)
        out = tmp_path / "seg_001.mp3"
        tts.generate_speech_edge("你好世界", str(out))

        words = tts.fetch_segment_timestamps("你好世界", provider="edge", audio_path=str(out))
        assert words and words[0]["word"] == "你好"
        assert tts.fetch_segment_timestamps("你好世界", provider="edge", audio_path=str(out)) is None

    def test_fetch_timestamps_edge_without_path_returns_none(self):
        assert _REAL_FETCH_SEGMENT_TIMESTAMPS("x", provider="edge", audio_path="") is None

    def test_validate_config_edge_needs_no_key(self, monkeypatch):
        monkeypatch.setattr(config, "LLM_API_KEY", "k")
        monkeypatch.setattr(config, "TTS_PROVIDER", "edge")
        monkeypatch.setattr(config, "STEPFUN_API_KEY", "")
        monkeypatch.setattr(config, "MINIMAX_API_KEY", "")
        monkeypatch.setattr(config, "VOLC_TTS_APPID", "")
        monkeypatch.setattr(config, "VOLC_TTS_TOKEN", "")
        monkeypatch.setattr(config, "IMAGE_SOURCE", "local")
        monkeypatch.setattr(config, "LOCAL_IMAGE_DIR", "/tmp")
        ok, problems, _ = config.validate_config()
        assert ok
        assert problems == []

    def test_generate_all_speech_edge_attaches_words(self, monkeypatch, tmp_path):
        """集成：edge 提供商下 generate_all_speech 应把词级时间片写进 seg["words"]"""
        # 装回真实时间戳通道（覆盖 conftest 的离线围栏）
        monkeypatch.setattr(tts, "fetch_segment_timestamps", _REAL_FETCH_SEGMENT_TIMESTAMPS)
        monkeypatch.setattr(config, "PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(config, "TEMP_DIR", str(tmp_path))

        def fake_edge(text, path, provider="", voice=""):
            with open(path, "wb") as f:
                f.write(b"RIFFfake")
            tts._EDGE_WORDS[path] = [{"word": "词", "start": 0.0, "end": 1.0}]

        monkeypatch.setattr(tts, "generate_speech", fake_edge)
        monkeypatch.setattr(tts, "normalize_loudness", lambda p: p)
        monkeypatch.setattr(tts, "get_audio_duration", lambda p: 1.0)
        monkeypatch.setattr(tts.aligner, "fill_missing_words", lambda segs, project_dir="": segs)

        segments = [{"text": "一段文本。", "duration": 0, "image_keyword": ""}]
        result = tts.generate_all_speech(segments, provider="edge")
        assert result[0]["words"] == [{"word": "词", "start": 0.0, "end": 1.0}]
        assert result[0]["words_source"] == "tts"
