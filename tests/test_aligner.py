"""aligner.py：引擎无关词级时间戳兜底——env 开关、引擎检测、mock 引擎接入与回退、缓存

本机不安装 whisper 系引擎（也不允许自动安装）：所有引擎行为都用注入的
mock 模块 / mock 时间戳结构验证，真实模型验证单独标注待办（TestRealEngineTodo）。
"""
import json
import os
import subprocess
import sys
import types

import pytest

import aligner
import config
import tts


# ---------- mock 引擎的「真实返回结构」（按官方文档结构构造） ----------


def _make_fake_whisper_timestamped(calls: dict, words=None):
    """whisper-timestamped 的 mock 模块：transcribe 返回交叉注意力词级结构。

    真实结构（linto-ai/whisper-timestamped）：
    result = {"language": str,
              "segments": [{"start", "end", "text", "words": [
                  {"text": str, "start": float|None, "end": float|None,
                   "confidence": float|None}, ...]}],
              "text": str}
    低置信/标点词的 start/end 可能为 None（真实行为，归一层必须能丢弃）。
    """
    if words is None:
        words = [
            {"text": "你好", "start": 0.02, "end": 0.48, "confidence": 0.971},
            {"text": "世界", "start": 0.50, "end": 1.05, "confidence": 0.952},
            {"text": "，", "start": None, "end": None, "confidence": None},
        ]

    def transcribe(model, audio, language=None, **kwargs):
        calls.setdefault("wts", []).append(
            {"audio": audio, "language": language, "model": model})
        return {
            "language": language or "zh",
            "segments": [{
                "start": 0.0, "end": 1.1, "text": "你好世界",
                "words": words,
            }],
            "text": "你好世界",
        }

    return types.SimpleNamespace(load_model=lambda size: {"fake": "wts-model", "size": size},
                                 transcribe=transcribe)


class _FakeFasterWord:
    """faster-whisper Word 对象：属性 .word / .start / .end"""

    def __init__(self, word, start, end):
        self.word, self.start, self.end = word, start, end


class _FakeFasterSegment:
    def __init__(self, words):
        self.words = words


class _FakeFasterInfo:
    language = "zh"
    language_probability = 0.98


def _make_fake_faster_whisper(calls: dict, words=None):
    """faster-whisper 的 mock 模块：WhisperModel.transcribe 返回 (生成器, info)。

    真实结构（SYSTRAN/faster-whisper）：segments 是生成器（不遍历不执行），
    每个 segment.words 是 Word(word, start, end, probability) 对象列表。
    """
    if words is None:
        words = [
            _FakeFasterWord("你好", 0.03, 0.50),
            _FakeFasterWord("世界", 0.52, 1.08),
        ]

    class _FakeWhisperModel:
        def __init__(self, model_size, device=None, compute_type=None):
            calls.setdefault("fw_init", []).append(
                {"model_size": model_size, "device": device, "compute_type": compute_type})

        def transcribe(self, audio, language=None, word_timestamps=False, **kwargs):
            calls.setdefault("fw", []).append(
                {"audio": audio, "language": language,
                 "word_timestamps": word_timestamps})
            return iter([_FakeFasterSegment(words)]), _FakeFasterInfo()

    return types.ModuleType("faster_whisper"), _FakeWhisperModel


@pytest.fixture(autouse=True)
def _isolated_aligner_state(monkeypatch):
    """隔离模块级状态：指引只打一次的标志、模型缓存、相关环境变量"""
    monkeypatch.setattr(aligner, "_HINT_SHOWN", False)
    aligner._MODEL_CACHE.clear()
    for key in ("ALIGNER", "ALIGNER_MODEL", "ALIGNER_LANGUAGE"):
        monkeypatch.delenv(key, raising=False)


def _spec_for(module_name):
    """构造一个「已安装」语义的假 spec（不触发真实 import）"""
    return types.SimpleNamespace(name=module_name)


def _install_engine(monkeypatch, calls: dict, engine="faster-whisper", words=None):
    """让引擎检测通过并注入 mock 引擎模块，返回注入的模块（便于断言）"""
    real_module = {"faster-whisper": "faster_whisper",
                   "whisper-timestamped": "whisper_timestamped"}[engine]

    def fake_find_spec(name):
        return _spec_for(name) if name == real_module else None

    monkeypatch.setattr(aligner, "_find_spec", fake_find_spec)
    if engine == "faster-whisper":
        module, model_cls = _make_fake_faster_whisper(calls, words)
        module.WhisperModel = model_cls
        monkeypatch.setitem(sys.modules, "faster_whisper", module)
    else:
        module = _make_fake_whisper_timestamped(calls, words)
        monkeypatch.setitem(sys.modules, "whisper_timestamped", module)
    return module


class TestAlignerSwitch:
    """ALIGNER env：auto / off / 强制引擎 / 非法值兜底"""

    def test_default_is_auto(self):
        assert aligner._aligner_mode() == "auto"
        assert aligner.enabled() is True

    def test_off_values_disable(self, monkeypatch):
        for raw in ("off", "OFF", "0", "false", "no"):
            monkeypatch.setenv("ALIGNER", raw)
            assert aligner._aligner_mode() == "off", raw
            assert aligner.enabled() is False, raw

    def test_engine_names_accepted(self, monkeypatch):
        monkeypatch.setenv("ALIGNER", "whisper-timestamped")
        assert aligner._aligner_mode() == "whisper-timestamped"
        monkeypatch.setenv("ALIGNER", "faster_whisper")  # 下划线别名
        assert aligner._aligner_mode() == "faster-whisper"

    def test_invalid_value_warns_and_falls_back_to_auto(self, monkeypatch, capsys):
        monkeypatch.setenv("ALIGNER", "whisperx")
        assert aligner._aligner_mode() == "auto"
        assert aligner.enabled() is True
        out = capsys.readouterr().out
        assert "无法识别" in out and "whisperx" in out

    def test_off_skips_everything_without_touching_segments(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ALIGNER", "off")
        seg = {"text": "关闭开关的段。", "audio_path": str(tmp_path / "a.mp3")}

        def boom(*a, **k):  # 引擎任何环节都不允许被触达
            raise AssertionError("ALIGNER=off 不应触发引擎检测/加载")

        monkeypatch.setattr(aligner, "resolve_engine", boom)
        result = aligner.fill_missing_words([seg], project_dir=str(tmp_path))
        assert "words" not in result[0]


class TestEngineDetection:
    """引擎检测：find_spec 只查安装不 import；未装时优雅跳过并打印安装指引"""

    def test_no_engine_installed_returns_empty(self, monkeypatch):
        monkeypatch.setattr(aligner, "_find_spec", lambda name: None)
        assert aligner.available_engines() == []

    def test_priority_faster_whisper_first(self, monkeypatch):
        """两个引擎都装了时 auto 选 faster-whisper（较轻、无 torch，见模块 docstring）"""
        monkeypatch.setattr(aligner, "_find_spec", lambda name: _spec_for(name))
        assert aligner.available_engines() == ["faster-whisper", "whisper-timestamped"]
        assert aligner.resolve_engine() == "faster-whisper"

    def test_only_timestamped_installed(self, monkeypatch):
        monkeypatch.setattr(
            aligner, "_find_spec",
            lambda name: _spec_for(name) if name == "whisper_timestamped" else None)
        assert aligner.resolve_engine() == "whisper-timestamped"

    def test_find_spec_error_treated_as_missing(self, monkeypatch):
        """真实 _find_spec 内部把 find_spec 的各类异常都消化为「未安装」"""
        def raising_find_spec(name):
            raise ModuleNotFoundError(f"no module named {name!r}")

        monkeypatch.setattr(aligner.importlib.util, "find_spec", raising_find_spec)
        assert aligner._find_spec("whatever") is None
        assert aligner.available_engines() == []
        assert aligner.resolve_engine() is None

    def test_missing_engine_prints_install_hint_once(self, monkeypatch, capsys):
        monkeypatch.setattr(aligner, "_find_spec", lambda name: None)
        assert aligner.resolve_engine() is None
        assert aligner.resolve_engine() is None  # 第二次不再刷屏
        out = capsys.readouterr().out
        assert out.count("pip install faster-whisper") == 1
        assert "绝不自动安装" in out
        assert "ALIGNER=off" in out  # 告知如何关闭提示

    def test_forced_engine_missing_is_graceful(self, monkeypatch, capsys):
        """强制指定未安装的引擎：与 auto 一样优雅跳过，不报错不安装"""
        monkeypatch.setenv("ALIGNER", "faster-whisper")
        monkeypatch.setattr(aligner, "_find_spec", lambda name: None)
        assert aligner.resolve_engine() is None
        assert "绝不自动安装" in capsys.readouterr().out


class TestNormalizeWords:
    """两种引擎词结构 → 通道契约 {"word","start","end"} 的归一"""

    def test_whisper_timestamped_dict_words(self):
        raw = [{"text": "你好", "start": 0.02, "end": 0.48, "confidence": 0.97},
               {"text": "世界", "start": 0.50, "end": 1.05, "confidence": 0.95}]
        words = aligner.normalize_words(raw)
        assert words == [{"word": "你好", "start": 0.02, "end": 0.48},
                         {"word": "世界", "start": 0.50, "end": 1.05}]

    def test_faster_whisper_object_words(self):
        raw = [_FakeFasterWord("你", 0.1, 0.3), _FakeFasterWord("好", 0.3, 0.5)]
        assert aligner.normalize_words(raw) == [
            {"word": "你", "start": 0.1, "end": 0.3},
            {"word": "好", "start": 0.3, "end": 0.5}]

    def test_none_or_invalid_times_dropped(self):
        """whisper-timestamped 低置信词 start/end=None、非数值、负起点都要丢弃"""
        raw = [
            {"text": "标点", "start": None, "end": None},
            {"text": "坏词", "start": "abc", "end": 1.0},
            {"text": "负起点", "start": -0.5, "end": 0.1},
            {"text": "好词", "start": 0.2, "end": 0.9},
        ]
        assert aligner.normalize_words(raw) == [
            {"word": "好词", "start": 0.2, "end": 0.9}]

    def test_end_earlier_than_start_clamped_and_sorted(self):
        raw = [{"word": "b", "start": 1.0, "end": 0.2},   # end<start 钳到 start
               {"word": "a", "start": 0.3, "end": 0.8}]   # 乱序 → 按 start 排序
        words = aligner.normalize_words(raw)
        assert words == [{"word": "a", "start": 0.3, "end": 0.8},
                         {"word": "b", "start": 1.0, "end": 1.0}]

    def test_empty_and_none_inputs(self):
        assert aligner.normalize_words([]) == []
        assert aligner.normalize_words(None) == []


class TestEngineAdaptersWithMockedModules:
    """用注入 sys.modules 的 mock 引擎模块走真实适配/模型缓存代码路径"""

    def test_whisper_timestamped_adapter(self, monkeypatch, tmp_path):
        calls: dict = {}
        _install_engine(monkeypatch, calls, engine="whisper-timestamped")
        audio = tmp_path / "seg.mp3"
        audio.write_bytes(b"fake-audio")

        words = aligner.align_segment(str(audio), text="你好世界",
                                      engine="whisper-timestamped")

        assert words == [{"word": "你好", "start": 0.02, "end": 0.48},
                         {"word": "世界", "start": 0.50, "end": 1.05}]  # None 时间词已丢弃
        (call,) = calls["wts"]
        assert call["audio"] == str(audio)
        assert call["language"] == "zh"  # ALIGNER_LANGUAGE 默认 zh
        assert call["model"] == {"fake": "wts-model", "size": "small"}  # 默认尺寸

    def test_faster_whisper_adapter(self, monkeypatch, tmp_path):
        calls: dict = {}
        _install_engine(monkeypatch, calls, engine="faster-whisper")
        audio = tmp_path / "seg.mp3"
        audio.write_bytes(b"fake-audio")

        words = aligner.align_segment(str(audio), engine="faster-whisper")

        assert words == [{"word": "你好", "start": 0.03, "end": 0.50},
                         {"word": "世界", "start": 0.52, "end": 1.08}]
        (init,) = calls["fw_init"]
        assert init == {"model_size": "small", "device": "cpu", "compute_type": "int8"}
        (call,) = calls["fw"]
        assert call["audio"] == str(audio)
        assert call["word_timestamps"] is True  # 词级时间戳必须显式开启

    def test_language_auto_env_resets_to_engine_detect(self, monkeypatch, tmp_path):
        """ALIGNER_LANGUAGE=auto → 传 None/空给引擎自动检测"""
        calls: dict = {}
        _install_engine(monkeypatch, calls, engine="faster-whisper")
        monkeypatch.setenv("ALIGNER_LANGUAGE", "auto")
        audio = tmp_path / "seg.mp3"
        audio.write_bytes(b"fake-audio")
        aligner.align_segment(str(audio), engine="faster-whisper")
        assert calls["fw"][0]["language"] in (None, "")

    def test_model_cached_across_calls(self, monkeypatch, tmp_path):
        """同 (引擎, 尺寸) 的模型进程内只加载一次（web 常驻进程不逐段重载）"""
        calls: dict = {}
        _install_engine(monkeypatch, calls, engine="faster-whisper")
        audio = tmp_path / "seg.mp3"
        audio.write_bytes(b"fake-audio")
        aligner.align_segment(str(audio), engine="faster-whisper")
        aligner.align_segment(str(audio), engine="faster-whisper")
        assert len(calls["fw_init"]) == 1   # 模型只构造一次
        assert len(calls["fw"]) == 2        # 两次都真的转写了


class TestAlignSegmentGraceful:
    """对齐永不抛异常：坏输入/引擎崩溃一律返回 []"""

    def test_missing_or_empty_audio_short_circuits(self, monkeypatch, tmp_path):
        called = []

        def fake_transcriber(*a, **k):
            called.append(a)
            return [{"word": "x", "start": 0.0, "end": 0.1}]

        monkeypatch.setitem(aligner._TRANSCRIBERS, "faster-whisper", fake_transcriber)
        monkeypatch.setattr(aligner, "_find_spec", lambda n: _spec_for(n))
        empty = tmp_path / "empty.mp3"
        empty.write_bytes(b"")
        assert aligner.align_segment("") == []
        assert aligner.align_segment(str(tmp_path / "nope.mp3")) == []
        assert aligner.align_segment(str(empty)) == []
        assert called == []  # 文件不可用时不触达引擎

    def test_engine_crash_returns_empty_with_warning(self, monkeypatch, tmp_path, capsys):
        def crashing_transcriber(*a, **k):
            raise RuntimeError("模型下载失败 / CUDA out of memory")

        monkeypatch.setitem(aligner._TRANSCRIBERS, "faster-whisper", crashing_transcriber)
        monkeypatch.setattr(aligner, "_find_spec", lambda n: _spec_for(n))
        audio = tmp_path / "seg.mp3"
        audio.write_bytes(b"fake-audio")
        assert aligner.align_segment(str(audio), engine="faster-whisper") == []
        assert "对齐失败" in capsys.readouterr().out

    def test_unknown_engine_without_adapter_warns(self, monkeypatch, tmp_path, capsys):
        audio = tmp_path / "seg.mp3"
        audio.write_bytes(b"fake-audio")
        assert aligner.align_segment(str(audio), engine="not-an-engine") == []
        assert "没有可用适配器" in capsys.readouterr().out


class TestFillMissingWordsChannel:
    """通道接入：只补缺不覆盖、无配音跳过、words.json 缓存与失效"""

    def _setup(self, monkeypatch, calls: dict):
        _install_engine(monkeypatch, calls, engine="faster-whisper")

    def test_fills_only_missing_words(self, monkeypatch, tmp_path):
        """D2 通道已有 words 的段不覆盖；无配音/文件缺失段跳过"""
        calls: dict = {}

        def counting_transcriber(audio, language, model_size):
            calls.setdefault("t", []).append(audio)
            return [{"word": "词", "start": 0.0, "end": 0.4}]

        monkeypatch.setitem(aligner._TRANSCRIBERS, "faster-whisper", counting_transcriber)
        self._setup(monkeypatch, calls)
        real_audio = tmp_path / "segment_000.mp3"
        real_audio.write_bytes(b"fake")
        missing_audio = tmp_path / "segment_002.mp3"  # 声明了路径但文件不存在
        tts_words = [{"word": "TTS", "start": 0.0, "end": 1.0}]
        segments = [
            {"text": "需要兜底对齐的一段。", "audio_path": str(real_audio)},
            {"text": "TTS 已带时间戳的段。", "audio_path": str(real_audio),
             "words": tts_words, "words_source": "tts"},
            {"text": "TTS 失败无配音段。", "audio_path": ""},          # 生成失败段
            {"text": "文件缺失段。", "audio_path": str(missing_audio)},
        ]
        result = aligner.fill_missing_words(segments, project_dir=str(tmp_path))

        assert calls["t"] == [str(real_audio)]  # 引擎只被无时间戳且有声频的段触达一次
        assert result[0]["words"] == [{"word": "词", "start": 0.0, "end": 0.4}]
        assert result[0]["words_source"] == "aligner"
        # D2 通道的词级时间戳原样保留（只补缺，绝不覆盖）
        assert result[1]["words"] is tts_words
        assert result[1]["words_source"] == "tts"
        assert "words" not in result[2]
        assert "words" not in result[3]

    def test_cache_written_and_reused(self, monkeypatch, tmp_path):
        """对齐结果落盘 segment_XXX.words.json，重跑直接读缓存不再推理"""
        calls: dict = {}

        def counting_transcriber(audio, language, model_size):
            calls.setdefault("t", []).append(audio)
            return [{"word": "缓存词", "start": 0.1, "end": 0.9}]

        monkeypatch.setitem(aligner._TRANSCRIBERS, "faster-whisper", counting_transcriber)
        self._setup(monkeypatch, calls)
        audio = tmp_path / "segment_000.mp3"
        audio.write_bytes(b"fake")
        seg = {"text": "同一份文案。", "audio_path": str(audio)}

        first = aligner.fill_missing_words([seg], project_dir=str(tmp_path))
        cache_path = tmp_path / "segment_000.words.json"
        assert cache_path.is_file()
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        assert payload["text"] == "同一份文案。"
        assert payload["engine"] == "faster-whisper"
        assert payload["model"] == "small"
        assert payload["words"] == [{"word": "缓存词", "start": 0.1, "end": 0.9}]

        def must_not_run(audio, language, model_size):
            raise AssertionError("缓存命中时不应再次推理")

        monkeypatch.setitem(aligner._TRANSCRIBERS, "faster-whisper", must_not_run)
        second = aligner.fill_missing_words(
            [{"text": "同一份文案。", "audio_path": str(audio)}],
            project_dir=str(tmp_path))
        assert second[0]["words"] == first[0]["words"]
        assert second[0]["words_source"] == "aligner"

    def test_cache_invalidated_by_text_change(self, monkeypatch, tmp_path):
        """文案改了（A12 复用语义下音频/缓存仍是旧文案的）：缓存作废重新对齐"""
        calls: dict = {}
        counter = {"n": 0}

        def counting_transcriber(audio, language, model_size):
            counter["n"] += 1
            return [{"word": f"第{counter['n']}次", "start": 0.0, "end": 0.5}]

        monkeypatch.setitem(aligner._TRANSCRIBERS, "faster-whisper", counting_transcriber)
        self._setup(monkeypatch, calls)
        audio = tmp_path / "segment_000.mp3"
        audio.write_bytes(b"fake")

        aligner.fill_missing_words([{"text": "旧文案。", "audio_path": str(audio)}],
                                   project_dir=str(tmp_path))
        result = aligner.fill_missing_words([{"text": "新文案。", "audio_path": str(audio)}],
                                            project_dir=str(tmp_path))
        assert counter["n"] == 2  # 文案变化触发重新对齐
        assert result[0]["words"] == [{"word": "第2次", "start": 0.0, "end": 0.5}]

    def test_cache_invalidated_by_model_change(self, monkeypatch, tmp_path):
        """换了模型尺寸：缓存作废（保证词级时间戳质量与配置一致）。
        注意传全新的段 dict：fill 只补缺，已被填过 words 的段按设计跳过。"""
        calls: dict = {}
        counter = {"n": 0}

        def counting_transcriber(audio, language, model_size):
            counter["n"] += 1
            return [{"word": "w", "start": 0.0, "end": 0.5}]

        monkeypatch.setitem(aligner._TRANSCRIBERS, "faster-whisper", counting_transcriber)
        self._setup(monkeypatch, calls)
        audio = tmp_path / "segment_000.mp3"
        audio.write_bytes(b"fake")
        aligner.fill_missing_words([{"text": "文案。", "audio_path": str(audio)}],
                                   project_dir=str(tmp_path))
        monkeypatch.setenv("ALIGNER_MODEL", "medium")
        result = aligner.fill_missing_words(
            [{"text": "文案。", "audio_path": str(audio)}],  # 新一轮管线的全新段
            project_dir=str(tmp_path))
        assert counter["n"] == 2
        assert result[0]["words"] == [{"word": "w", "start": 0.0, "end": 0.5}]

    def test_empty_alignment_result_not_cached(self, monkeypatch, tmp_path):
        """引擎返回空（静音/识别不出）：不写缓存、不设 words，下轮重试"""
        calls: dict = {}
        monkeypatch.setitem(aligner._TRANSCRIBERS, "faster-whisper",
                            lambda *a, **k: [])
        self._setup(monkeypatch, calls)
        audio = tmp_path / "segment_000.mp3"
        audio.write_bytes(b"fake")
        seg = {"text": "静音段。", "audio_path": str(audio)}
        result = aligner.fill_missing_words([seg], project_dir=str(tmp_path))
        assert "words" not in result[0]
        assert not (tmp_path / "segment_000.words.json").exists()


class TestTtsChannelIntegration:
    """generate_all_speech 接线：TTS 通道走完必调词级兜底（CLI/Web 共用）"""

    def test_generate_all_speech_invokes_fill_missing_words(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(config, "TEMP_DIR", str(tmp_path))
        monkeypatch.setattr(tts, "get_audio_duration", lambda p: 3.0)

        def fake_generate_speech(text, output_path, provider="", voice=""):
            with open(output_path, "wb"):
                pass

        monkeypatch.setattr(tts, "generate_speech", fake_generate_speech)
        monkeypatch.setattr(tts, "normalize_loudness", lambda p: p)

        recorded = {}

        def fake_fill(segments, project_dir=""):
            recorded["segments"] = segments
            recorded["project_dir"] = project_dir
            return segments

        monkeypatch.setattr(aligner, "fill_missing_words", fake_fill)
        segments = [{"text": "接入验证段。", "duration": 0, "image_keyword": ""}]
        result = tts.generate_all_speech(segments)

        assert recorded["segments"] is result          # 同一批段对象传入兜底
        assert recorded["project_dir"] == str(tmp_path)
        assert result[0]["audio_path"]  # TTS 主流程照常完成


class TestRealEngineTodo:
    """【待办】真实模型验证（D3 报告 §遗留#1）

    本机未安装 whisper 系引擎、模型权重需联网下载：以下用例在装好引擎的
    机器上才会真正执行，用于验证真实模型链路（load_model→transcribe→归一）。
    """

    def _make_sine_mp3(self, path, seconds=2):
        try:
            import imageio_ffmpeg
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            pytest.skip("imageio-ffmpeg 不可用")
        subprocess.run(
            [ffmpeg, "-y", "-hide_banner", "-f", "lavfi",
             "-i", f"sine=frequency=440:duration={seconds}",
             "-ar", "24000", "-ac", "1", "-q:a", "4", str(path)],
            capture_output=True, check=True,
        )
        return str(path)

    def test_real_whisper_timestamped(self, tmp_path):
        pytest.importorskip("whisper_timestamped",
                            reason="whisper-timestamped 未安装（真实模型验证待办）")
        mp3 = self._make_sine_mp3(tmp_path / "seg.mp3")
        words = aligner.align_segment(mp3, text="正弦波不是语音",
                                      engine="whisper-timestamped")
        assert isinstance(words, list)  # 真实链路不崩、结构合法（正弦音可识别出 0 词）
        for w in words:
            assert {"word", "start", "end"} <= set(w)
            assert 0 <= w["start"] <= w["end"]

    def test_real_faster_whisper(self, tmp_path):
        pytest.importorskip("faster_whisper",
                            reason="faster-whisper 未安装（真实模型验证待办）")
        mp3 = self._make_sine_mp3(tmp_path / "seg.mp3")
        words = aligner.align_segment(mp3, text="正弦波不是语音",
                                      engine="faster-whisper")
        assert isinstance(words, list)
        for w in words:
            assert {"word", "start", "end"} <= set(w)
            assert 0 <= w["start"] <= w["end"]
