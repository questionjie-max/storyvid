"""bgm：BGM_VOLUME/BGM_TEMP 配置解析、imageio_ffmpeg 离线合成柔和垫乐、混音端到端。

全部离线运行：垫乐由 moviepy 环境自带的 imageio_ffmpeg 合成（anoisesrc/sine 分层 +
低通滤波），端到端走真实 create_video 出小 MP4，不依赖任何 API Key。
成片音轨一律用 ffmpeg CLI 提取成 wav 后断言（地面真值，绕开 moviepy 音频读取器）。
"""
import importlib
import os
import subprocess
import wave

import numpy as np
import pytest
from PIL import Image
from moviepy import VideoFileClip

import config
import video


@pytest.fixture
def proj_dir(tmp_path, monkeypatch):
    """把项目目录指到临时目录，避免污染真实 output/"""
    monkeypatch.setattr(config, "PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr(config, "TEMP_DIR", str(tmp_path / "_temp"))
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path))
    return tmp_path


def _make_image(path, color):
    Image.new("RGB", (480, 270), color).save(path, quality=90)
    return str(path)


def _make_wav(path, duration, freq=440.0, amp=0.5, fps=8000):
    """生成正弦波 WAV（离线配音替身，不依赖任何 TTS API）"""
    n = int(duration * fps)
    t = np.arange(n) / fps
    samples = (amp * np.sin(2 * np.pi * freq * t) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(fps)
        f.writeframes(samples.tobytes())
    return str(path)


def _read_wav(path):
    """读 wav 为 (采样率, float32 单声道数组)"""
    with wave.open(str(path), "rb") as f:
        rate, ch, n = f.getframerate(), f.getnchannels(), f.getnframes()
        raw = f.readframes(n)
    arr = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32767.0
    if ch > 1:
        arr = arr.reshape(-1, ch).mean(axis=1)
    return rate, arr


def _extract_audio(video_path, out_wav):
    """用 ffmpeg CLI 把成片音轨提为 44.1kHz 单声道 wav（地面真值，不依赖 moviepy 读取器）"""
    subprocess.run(
        [video._ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
         "-i", str(video_path), "-map", "0:a", "-ar", "44100", "-ac", "1",
         "-c:a", "pcm_s16le", str(out_wav)],
        check=True, capture_output=True, timeout=120,
    )
    return str(out_wav)


def _band_power(arr, rate, lo, hi):
    """频带 [lo, hi] Hz 内的能量占比之和（FFT 功率谱），用于分频带断言"""
    sp = np.abs(np.fft.rfft(arr)) ** 2
    freqs = np.fft.rfftfreq(len(arr), 1 / rate)
    return float(sp[(freqs >= lo) & (freqs <= hi)].sum())


def _audio_stats(wav_path):
    rate, arr = _read_wav(wav_path)
    return {
        "rate": rate,
        "duration": len(arr) / rate,
        "peak": float(np.abs(arr).max()),
        "rms": float(np.sqrt((arr ** 2).mean())),
        "arr": arr,
    }


class TestBgmConfig:
    """BGM_VOLUME / BGM_TEMP 环境变量解析与钳制"""

    def test_env_parsing_and_fallback(self, monkeypatch):
        """BGM_VOLUME 浮点解析（非法值回退默认 0.2）；BGM_TEMP 大小写归一并回退 warm"""
        before_keys = set(os.environ)
        snapshot = {k: getattr(config, k) for k in ("BGM_VOLUME", "BGM_TEMP")}
        try:
            monkeypatch.setenv("BGM_VOLUME", "0.25")
            monkeypatch.setenv("BGM_TEMP", "COOL")
            importlib.reload(config)
            assert config.BGM_VOLUME == pytest.approx(0.25)
            assert config.BGM_TEMP == "cool"  # 大写归一化

            monkeypatch.setenv("BGM_VOLUME", "abc")
            monkeypatch.setenv("BGM_TEMP", "jazz")
            importlib.reload(config)
            assert config.BGM_VOLUME == pytest.approx(0.2)  # 非法值回退默认
            assert config.BGM_TEMP == "warm"  # 未知风格回退 warm

            monkeypatch.setenv("BGM_VOLUME", "")
            importlib.reload(config)
            assert config.BGM_VOLUME == pytest.approx(0.2)  # 空串视为未配置
        finally:
            for k in set(os.environ) - before_keys:
                os.environ.pop(k, None)
            for k, v in snapshot.items():
                setattr(config, k, v)

    def test_default_volume_is_low_bed(self, monkeypatch):
        """BGM_VOLUME 未配置时默认 0.2（质量循环 R4：成片默认带低电平垫乐，
        配音始终在前）；设 0 可关闭"""
        monkeypatch.delenv("BGM_VOLUME", raising=False)
        importlib.reload(config)
        try:
            assert config.BGM_VOLUME == pytest.approx(0.2)
        finally:
            importlib.reload(config)

    def test_bgm_volume_clamped(self, monkeypatch):
        """video._bgm_volume 钳制：<=0 关闭，>1 压回 1（防止垫乐盖过配音）"""
        monkeypatch.setattr(config, "BGM_VOLUME", -0.5)
        assert video._bgm_volume() == 0.0
        monkeypatch.setattr(config, "BGM_VOLUME", 0.25)
        assert video._bgm_volume() == pytest.approx(0.25)
        monkeypatch.setattr(config, "BGM_VOLUME", 5.0)
        assert video._bgm_volume() == pytest.approx(1.0)

    def test_bgm_style_lookup_and_fallback(self):
        """三档风格参数各不相同；未知风格回退 warm（与 config 解析兜底一致）"""
        warm = video._bgm_style("warm")
        cool = video._bgm_style("cool")
        deep = video._bgm_style("deep")
        assert len({tuple(warm["chord"]), tuple(cool["chord"]), tuple(deep["chord"])}) == 3
        assert video._bgm_style("no-such-style") == warm
        assert video._bgm_style("") == video._bgm_style()  # 缺省读 config.BGM_TEMP

    def test_bgm_dir_env_parsing(self, monkeypatch):
        """BGM_DIR 原样读取并去除首尾空白；未配置为空串（曲库关闭）"""
        before_keys = set(os.environ)
        snapshot = getattr(config, "BGM_DIR")
        try:
            monkeypatch.setenv("BGM_DIR", "  ./my_music  ")
            importlib.reload(config)
            assert config.BGM_DIR == "./my_music"

            monkeypatch.delenv("BGM_DIR", raising=False)
            importlib.reload(config)
            assert config.BGM_DIR == ""
        finally:
            for k in set(os.environ) - before_keys:
                os.environ.pop(k, None)
            setattr(config, "BGM_DIR", snapshot)  # 与 test_env_parsing_and_fallback 同法还原

    def test_validate_config_warns_missing_bgm_dir(self, tmp_path, monkeypatch):
        """BGM_DIR 指向不存在的目录时出警告（不致命，将回退合成垫乐）；未配置无警告"""
        monkeypatch.setattr(config, "BGM_DIR", str(tmp_path / "nope"))
        _, problems, warnings = config.validate_config(require_llm=False)
        assert any("BGM_DIR" in w for w in warnings)
        assert not any("BGM_DIR" in p for p in problems), "曲库目录缺失只是警告不是致命问题"

        monkeypatch.setattr(config, "BGM_DIR", "")
        _, _, warnings = config.validate_config(require_llm=False)
        assert not any("BGM_DIR" in w for w in warnings)


class TestGenerateBgm:
    """generate_bgm：imageio_ffmpeg 离线合成柔和垫乐（anoisesrc/sine 分层 + 低通）"""

    def test_generates_wav_with_requested_duration(self, tmp_path):
        out = tmp_path / "pad.wav"
        result = video.generate_bgm(str(out), 2.0)
        assert result == str(out)
        assert out.exists()
        assert out.stat().st_size > 10_000
        with wave.open(str(out), "rb") as f:
            assert f.getframerate() == video._BGM_SAMPLE_RATE
            assert f.getnchannels() == 2
            assert f.getnframes() / f.getframerate() == pytest.approx(2.0, abs=0.2)

    def test_pad_is_soft_low_frequency_and_no_clipping(self, tmp_path):
        """「柔和/不刺耳」的客观代理：无削波、电平低、>2kHz 高频能量占比极小"""
        out = tmp_path / "pad.wav"
        video.generate_bgm(str(out), 3.0)
        rate, arr = _read_wav(out)
        peak = float(np.abs(arr).max())
        rms = float(np.sqrt((arr ** 2).mean()))
        assert peak < 0.95, f"垫乐不应削波，实际峰值 {peak:.3f}"
        assert 0.005 < rms < 0.2, f"垫乐应有内容且电平低，实际 rms {rms:.4f}"

        sp = np.abs(np.fft.rfft(arr)) ** 2
        freqs = np.fft.rfftfreq(len(arr), 1 / rate)
        hi_ratio = float(sp[freqs > 2000].sum() / sp.sum())
        assert hi_ratio < 0.01, f"垫乐高频（>2kHz）能量占比应 <1%（避免刺耳），实际 {hi_ratio:.5f}"

    def test_styles_differ(self, tmp_path):
        """三档 BGM_TEMP 风格产出可闻不同的垫乐（warm/cool/deep 全部生成并两两对比）"""
        pads = {}
        for style in ("warm", "cool", "deep"):
            p = tmp_path / f"{style}.wav"
            video.generate_bgm(str(p), 2.0, style=style)
            pads[style] = _read_wav(p)[1]
        for a, b in (("warm", "cool"), ("warm", "deep"), ("cool", "deep")):
            x, y = pads[a], pads[b]
            n = min(len(x), len(y))
            assert float(np.abs(x[:n] - y[:n]).max()) > 0.01, \
                f"{a} 与 {b} 垫乐样本应显著不同"

    def test_unknown_style_falls_back_to_warm(self, tmp_path):
        """generate_bgm(style=未知值) 不抛异常，产出与 warm 一致"""
        p1 = tmp_path / "jazz.wav"
        p2 = tmp_path / "warm.wav"
        video.generate_bgm(str(p1), 1.5, style="jazz")
        video.generate_bgm(str(p2), 1.5, style="warm")
        _, a = _read_wav(p1)
        _, b = _read_wav(p2)
        assert np.allclose(a, b)


class TestMixBgm:
    """_mix_bgm：音量开关、失败降级（不中断合成）"""

    def test_volume_zero_is_noop(self, tmp_path, monkeypatch):
        """BGM_VOLUME=0（默认）时直接返回原片，不合成垫乐"""
        monkeypatch.setattr(config, "BGM_VOLUME", 0.0)

        def _boom(*args, **kwargs):
            raise AssertionError("BGM 关闭时不应调用 generate_bgm")

        monkeypatch.setattr(video, "generate_bgm", _boom)
        dummy = object()
        final, bgm = video._mix_bgm(dummy, str(tmp_path))
        assert final is dummy
        assert bgm is None

    def test_ffmpeg_failure_degrades_gracefully(self, tmp_path, monkeypatch, capsys):
        """ffmpeg 不可用时打印告警并返回原片（降级为无垫乐继续出片）"""
        monkeypatch.setattr(config, "BGM_VOLUME", 0.25)
        monkeypatch.setattr(video, "_ffmpeg_exe", lambda: "/nonexistent/ffmpeg")

        class _FakeVideo:
            duration = 2.0

        fake = _FakeVideo()
        final, bgm = video._mix_bgm(fake, str(tmp_path))
        assert final is fake
        assert bgm is None
        assert "跳过 BGM" in capsys.readouterr().out


class TestBgmEndToEnd:
    """端到端：create_video 真实合成，垫乐混入、铺满全片、配音不被盖过（全部离线）"""

    def _env(self, monkeypatch, volume):
        monkeypatch.setattr(config, "VIDEO_FPS", 12)
        monkeypatch.setattr(config, "INTRO_OUTRO", False)
        monkeypatch.setattr(config, "BGM_VOLUME", volume)

    def _segments(self, tmp_path, audio_path=""):
        img = _make_image(tmp_path / "bgm.jpg", (90, 30, 120))
        return [{"text": "一句字幕。", "duration": 2.0, "image_path": img,
                 "audio_path": audio_path}]

    def test_e2e_bgm_mixed_with_voice_not_covered(self, proj_dir, tmp_path, monkeypatch):
        """开 BGM 后：垫乐确实混入（低频带能量出现），且远低于配音（配音不被盖过）"""
        self._env(monkeypatch, 0.25)
        wav = _make_wav(tmp_path / "voice.wav", 2.0)
        out_on = tmp_path / "on.mp4"
        video.create_video(self._segments(tmp_path, wav), str(out_on), width=480, height=270)
        out_off = tmp_path / "off.mp4"
        self._env(monkeypatch, 0.0)  # 同素材关 BGM 对照
        video.create_video(self._segments(tmp_path, wav), str(out_off), width=480, height=270)

        on = _audio_stats(_extract_audio(out_on, tmp_path / "on.wav"))
        off = _audio_stats(_extract_audio(out_off, tmp_path / "off.wav"))

        assert on["peak"] > off["peak"] + 0.01, \
            f"开 BGM 后峰值应上升（垫乐混入），on {on['peak']:.4f} vs off {off['peak']:.4f}"

        pad_band_on = _band_power(on["arr"], on["rate"], 90, 180)   # 垫乐根音 110Hz / 五度 164.8Hz
        pad_band_off = _band_power(off["arr"], off["rate"], 90, 180)
        assert pad_band_on > 20 * max(pad_band_off, 1e-12), \
            "关 BGM 的对照里不应有垫乐低频成分，开后应显著出现"

        voice_band = _band_power(on["arr"], on["rate"], 380, 500)   # 配音 440Hz
        assert pad_band_on < 0.5 * voice_band, \
            f"垫乐频带能量应远低于配音频带（配音不被盖过），实际垫乐/配音 = {pad_band_on / voice_band:.4f}"

    def test_e2e_bgm_fills_silent_video(self, proj_dir, tmp_path, monkeypatch):
        """无配音段落 + BGM：成片出现铺满全片时长的音轨且非静音"""
        self._env(monkeypatch, 0.25)
        out = tmp_path / "silent_bgm.mp4"
        result = video.create_video(self._segments(tmp_path), str(out), width=480, height=270)
        assert result == str(out)
        assert out.exists()
        stats = _audio_stats(_extract_audio(out, tmp_path / "s.wav"))
        assert stats["duration"] == pytest.approx(2.0, abs=0.35)
        assert stats["rms"] > 0.003, f"垫乐应铺满并可闻，实际 rms {stats['rms']:.4f}"

    def test_e2e_bgm_spans_intro_outro_cards(self, proj_dir, tmp_path, monkeypatch):
        """BGM 铺满含片头片尾卡的整条时间轴：片头卡区间也有垫乐"""
        self._env(monkeypatch, 0.25)
        monkeypatch.setattr(config, "INTRO_OUTRO", True)
        out = tmp_path / "cards_bgm.mp4"
        video.create_video(self._segments(tmp_path), str(out), width=480, height=270, topic="垫乐")
        with VideoFileClip(str(out)) as v:
            assert v.duration == pytest.approx(2.0 + video.cards_duration(), abs=0.5)
        stats = _audio_stats(_extract_audio(out, tmp_path / "c.wav"))
        assert stats["duration"] == pytest.approx(2.0 + video.cards_duration(), abs=0.5)
        # 片头卡区间（前 2.5s）应有垫乐能量（正片 2.5s 后才开始，之前只有垫乐）
        rate, arr = _read_wav(tmp_path / "c.wav")
        head_rms = float(np.sqrt((arr[: int(2.0 * rate)] ** 2).mean()))
        assert head_rms > 0.003, f"片头卡区间应有垫乐，实际 rms {head_rms:.4f}"

    def test_e2e_bgm_off_by_default_keeps_rendering(self, proj_dir, tmp_path, monkeypatch):
        """默认 BGM_VOLUME=0：合成照常成功且无垫乐低频成分（旧行为回归保护）"""
        self._env(monkeypatch, 0.0)
        assert config.BGM_VOLUME == 0.0
        wav = _make_wav(tmp_path / "voice.wav", 2.0)
        out = tmp_path / "default.mp4"
        result = video.create_video(self._segments(tmp_path, wav), str(out), width=480, height=270)
        assert result == str(out)
        assert out.exists()
        stats = _audio_stats(_extract_audio(out, tmp_path / "d.wav"))
        assert stats["duration"] == pytest.approx(2.0, abs=0.35)
        assert _band_power(stats["arr"], stats["rate"], 90, 180) < 0.5 * \
            _band_power(stats["arr"], stats["rate"], 380, 500), \
            "默认关闭时成片不应出现垫乐低频成分"


class TestUserBgm:
    """用户上传 BGM（web /api/upload）优先于合成垫乐：_mix_bgm(user_bgm=...) 路径"""

    def test_user_bgm_preferred_over_generated(self, tmp_path, monkeypatch, capsys):
        """传入 user_bgm 时不得调用 generate_bgm，直接用用户文件作音源"""
        monkeypatch.setattr(config, "BGM_VOLUME", 0.2)

        def _boom(*args, **kwargs):
            raise AssertionError("有用户 BGM 时不应再调用 generate_bgm 合成垫乐")

        monkeypatch.setattr(video, "generate_bgm", _boom)
        wav = _make_wav(tmp_path / "user.wav", 2.0)

        class _FakeVideo:
            duration = 2.0
            audio = None

            def with_audio(self, audio_clip):
                self.audio = audio_clip
                return self

        final, bgm = video._mix_bgm(_FakeVideo(), str(tmp_path), user_bgm=wav)
        assert bgm is not None, "用户 BGM 应被加载为音源 clip"
        assert bgm.duration == pytest.approx(2.0, abs=0.2)
        assert "用户上传的背景音乐" in capsys.readouterr().out
        bgm.close()

    def test_user_bgm_mixes_even_when_global_off(self, tmp_path, monkeypatch):
        """BGM_VOLUME=0（全局默认关）时上传 BGM 仍按 _USER_BGM_DEFAULT_VOLUME 混入"""
        monkeypatch.setattr(config, "BGM_VOLUME", 0.0)
        assert video._bgm_volume() == 0.0
        wav = _make_wav(tmp_path / "user.wav", 2.0)

        class _FakeVideo:
            duration = 2.0
            audio = None

            def with_audio(self, audio_clip):
                self.audio = audio_clip
                return self

        final, bgm = video._mix_bgm(_FakeVideo(), str(tmp_path), user_bgm=wav)
        assert bgm is not None, "上传即代表想用：全局 BGM 关闭时用户 BGM 也应混入"
        bgm.close()

    def test_user_bgm_missing_file_falls_back_to_synth(self, tmp_path, monkeypatch, capsys):
        """user_bgm 指向不存在的文件时回退合成垫乐路径（BGM_VOLUME>0 才合成）"""
        monkeypatch.setattr(config, "BGM_VOLUME", 0.25)
        monkeypatch.setattr(video, "_ffmpeg_exe", lambda: "/nonexistent/ffmpeg")

        class _FakeVideo:
            duration = 2.0
            audio = None

            def with_audio(self, audio_clip):
                self.audio = audio_clip
                return self

        final, bgm = video._mix_bgm(_FakeVideo(), str(tmp_path),
                                    user_bgm=str(tmp_path / "nope.wav"))
        assert final is not None and bgm is None, "合成失败应降级为无垫乐"
        assert "跳过 BGM" in capsys.readouterr().out

    def test_user_bgm_longer_than_video_is_trimmed(self, tmp_path, monkeypatch):
        """用户 BGM 比成片长时截到成片时长（subclipped 语义，与垫乐路径一致）"""
        monkeypatch.setattr(config, "BGM_VOLUME", 0.2)
        wav = _make_wav(tmp_path / "long.wav", 6.0)

        class _FakeVideo:
            duration = 2.0
            audio = None

            def with_audio(self, audio_clip):
                self.audio = audio_clip
                return self

        final, bgm = video._mix_bgm(_FakeVideo(), str(tmp_path), user_bgm=wav)
        assert bgm is not None
        assert bgm.duration == pytest.approx(2.0, abs=0.2)
        bgm.close()


# ---------- 用户自备曲库 BGM_DIR（任务 D7）：扫描 / 随机选曲当日不重复 / 混音 ----------


@pytest.fixture(autouse=True)
def _no_bgm_dir(monkeypatch):
    """默认清空 BGM_DIR：本机 .env 若配置了真实曲库也不得影响任何用例。

    曲库相关用例通过各自夹具再显式设置 config.BGM_DIR 覆盖这里。
    """
    monkeypatch.setattr(config, "BGM_DIR", "")


def _ffmpeg_tone(path, freq, duration=3.0):
    """用 ffmpeg 合成一首测试曲（扩展名决定格式：.mp3 走 libmp3lame，其余 pcm wav）。

    两首测试曲均为纯正弦音（频率可区分），供选择逻辑与混音断言使用。
    """
    path = str(path)
    ext = os.path.splitext(path)[1].lower()
    cmd = [video._ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
           "-f", "lavfi",
           "-i", f"sine=frequency={freq}:duration={duration}:sample_rate=44100"]
    if ext == ".mp3":
        cmd += ["-c:a", "libmp3lame", "-b:a", "128k"]
    else:
        cmd += ["-c:a", "pcm_s16le"]
    cmd += [path]
    subprocess.run(cmd, check=True, capture_output=True, timeout=60)
    return path


def _library(tmp_path, monkeypatch, tracks):
    """建曲库目录（tracks = [(相对路径, 频率)]，可含子目录）并设 config.BGM_DIR。

    音频扩展名走 ffmpeg 真实合成；其余扩展名只写占位文本（非音频候选）。
    """
    lib = tmp_path / "music"
    for rel, freq in tracks:
        dst = lib / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if rel.lower().endswith((".mp3", ".wav")):
            _ffmpeg_tone(dst, freq)
        else:
            dst.write_text("占位", encoding="utf-8")
    monkeypatch.setattr(config, "BGM_DIR", str(lib))
    return lib


def _read_memory(proj_dir):
    import json
    with open(os.path.join(str(proj_dir), video._BGM_USED_NAME), "r", encoding="utf-8") as f:
        return json.load(f)


class TestBgmLibrary:
    """bgm_library_tracks / pick_bgm_from_library：曲库扫描与当日不重复选择逻辑"""

    def test_scan_finds_mp3_and_wav_recursively(self, tmp_path, monkeypatch):
        """递归扫描子目录，.mp3 与 .wav 都收、大小写不敏感、结果排序稳定"""
        lib = _library(tmp_path, monkeypatch, [
            ("track_a.mp3", 300.0), ("album/track_b.wav", 620.0),
            ("notes.txt", 0.0),  # 非音频文件混入
        ])
        (lib / "UPPER.MP3").write_bytes(b"junk")  # 大写扩展名的坏文件也算 mp3 候选
        tracks = video.bgm_library_tracks()
        names = [os.path.relpath(t, str(lib)) for t in tracks]
        assert "track_a.mp3" in names and os.path.join("album", "track_b.wav") in names
        assert "UPPER.MP3" in names and "notes.txt" not in names
        assert tracks == sorted(tracks), "扫描结果应排序稳定（记忆比对依赖）"

    def test_scan_unset_or_invalid_dir_returns_empty(self, tmp_path, monkeypatch):
        """BGM_DIR 未配置 / 不存在 / 指向文件 / 空目录 → 空列表（回退合成垫乐）"""
        monkeypatch.setattr(config, "BGM_DIR", "")
        assert video.bgm_library_tracks() == []
        monkeypatch.setattr(config, "BGM_DIR", str(tmp_path / "nope"))
        assert video.bgm_library_tracks() == []
        plain = tmp_path / "afile.txt"
        plain.write_text("x")
        monkeypatch.setattr(config, "BGM_DIR", str(plain))
        assert video.bgm_library_tracks() == []
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setattr(config, "BGM_DIR", str(empty))
        assert video.bgm_library_tracks() == []

    def test_pick_from_library_and_memory_recorded(self, tmp_path, monkeypatch):
        """随机选曲出自曲库，记忆文件记录当日选择与已用历史"""
        lib = _library(tmp_path, monkeypatch, [("a.mp3", 300.0), ("b.wav", 620.0)])
        tracks = video.bgm_library_tracks()
        pick = video.pick_bgm_from_library(str(tmp_path))
        assert pick in tracks
        mem = _read_memory(tmp_path)
        assert mem["date"] == video._bgm_today()
        assert mem["picked"] == pick
        assert mem["used"] == [pick]
        assert (tmp_path / video._BGM_USED_NAME).exists()

    def test_same_day_reuses_picked_track(self, tmp_path, monkeypatch):
        """同一项目当天多次取曲返回同一首（多画幅批量/局部重渲配乐一致）"""
        _library(tmp_path, monkeypatch, [("a.mp3", 300.0), ("b.wav", 620.0)])
        p1 = video.pick_bgm_from_library(str(tmp_path))
        p2 = video.pick_bgm_from_library(str(tmp_path))
        p3 = video.pick_bgm_from_library(str(tmp_path))
        assert p1 == p2 == p3
        assert _read_memory(tmp_path)["used"] == [p1], "当天复用不得追加历史"

    def test_new_day_repick_avoids_history(self, tmp_path, monkeypatch):
        """跨天重选：避开历史已用；记忆文件日期随之更新"""
        _library(tmp_path, monkeypatch, [("a.mp3", 300.0), ("b.wav", 620.0)])
        p1 = video.pick_bgm_from_library(str(tmp_path))
        monkeypatch.setattr(video, "_bgm_today", lambda: "2000-01-02")
        p2 = video.pick_bgm_from_library(str(tmp_path))
        assert p2 != p1, "两首曲库跨天重选应换曲（避开已用）"
        mem = _read_memory(tmp_path)
        assert mem["date"] == "2000-01-02" and mem["picked"] == p2
        assert sorted(mem["used"]) == sorted([p1, p2])

    def test_exhausted_library_resets_round(self, tmp_path, monkeypatch):
        """历史把整库用尽后清空历史开始新一轮（永远有曲可选）"""
        _library(tmp_path, monkeypatch, [("only.wav", 440.0)])
        p1 = video.pick_bgm_from_library(str(tmp_path))
        monkeypatch.setattr(video, "_bgm_today", lambda: "2000-01-02")
        p2 = video.pick_bgm_from_library(str(tmp_path))
        assert p2 == p1, "单曲库跨天重置后唯一曲目仍可选出"
        mem = _read_memory(tmp_path)
        assert mem["used"] == [p2], "整库用尽应清空历史（而非无限累积）"

    def test_corrupt_memory_file_tolerated(self, tmp_path, monkeypatch):
        """记忆文件损坏（坏 JSON）视为空记忆，选曲照常不阻塞"""
        _library(tmp_path, monkeypatch, [("a.mp3", 300.0), ("b.wav", 620.0)])
        (tmp_path / video._BGM_USED_NAME).write_text("{not json", encoding="utf-8")
        pick = video.pick_bgm_from_library(str(tmp_path))
        assert pick in video.bgm_library_tracks()

    def test_picked_file_deleted_repicks(self, tmp_path, monkeypatch):
        """当日已选曲目被从曲库删除后，当天内重选剩余曲目（不返回失效路径）"""
        _library(tmp_path, monkeypatch, [("a.mp3", 300.0), ("b.wav", 620.0)])
        p1 = video.pick_bgm_from_library(str(tmp_path))
        os.remove(p1)
        p2 = video.pick_bgm_from_library(str(tmp_path))
        assert p2 != p1 and os.path.isfile(p2)


@pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnraisableExceptionWarning"
)  # 坏曲库文件让 moviepy 只建出半个 reader，其 __del__ 的 GC 噪声与本测试无关
class TestBgmLibraryMixing:
    """_mix_bgm 曲库接线：曲库优先于合成垫乐、回退链、淡入淡出共存（含端到端）"""

    class _FakeVideo:
        duration = 2.0
        audio = None

        def with_audio(self, audio_clip):
            self.audio = audio_clip
            return self

    def test_library_preferred_over_synth_pad(self, tmp_path, monkeypatch, capsys):
        """有可用曲库时不得调用 generate_bgm，音源为曲库选出的曲目"""
        monkeypatch.setattr(config, "BGM_VOLUME", 0.25)
        lib = _library(tmp_path, monkeypatch, [("a.mp3", 300.0), ("b.wav", 620.0)])

        def _boom(*args, **kwargs):
            raise AssertionError("有可用曲库时不应再调用 generate_bgm 合成垫乐")

        monkeypatch.setattr(video, "generate_bgm", _boom)
        final, bgm = video._mix_bgm(self._FakeVideo(), str(tmp_path))
        assert bgm is not None, "曲库可用时应加载曲目为音源 clip"
        out = capsys.readouterr().out
        picked = os.path.basename(_read_memory(tmp_path)["picked"])
        assert f"BGM_DIR 曲库 {picked}" in out and "跳过 BGM" not in out
        bgm.close()

    def test_library_mixes_even_when_volume_zero(self, tmp_path, monkeypatch, capsys):
        """BGM_VOLUME=0（默认关）+ 配置曲库：按 _USER_BGM_DEFAULT_VOLUME 自动开启"""
        monkeypatch.setattr(config, "BGM_VOLUME", 0.0)
        _library(tmp_path, monkeypatch, [("a.mp3", 300.0)])
        final, bgm = video._mix_bgm(self._FakeVideo(), str(tmp_path))
        assert bgm is not None, "配置曲库即代表想用：全局关时也应混入"
        assert f"音量 {video._USER_BGM_DEFAULT_VOLUME}" in capsys.readouterr().out
        bgm.close()

    def test_empty_library_falls_back_to_synth_pad(self, tmp_path, monkeypatch, capsys):
        """BGM_DIR 目录存在但无音频 → 走合成垫乐路径（此处以替身验证接线）"""
        monkeypatch.setattr(config, "BGM_VOLUME", 0.25)
        empty = tmp_path / "empty_lib"
        empty.mkdir()
        monkeypatch.setattr(config, "BGM_DIR", str(empty))

        def _fake_gen(out_path, duration, style=""):
            return _make_wav(out_path, 2.0, freq=200.0)

        monkeypatch.setattr(video, "generate_bgm", _fake_gen)
        final, bgm = video._mix_bgm(self._FakeVideo(), str(tmp_path))
        assert bgm is not None, "空曲库应回退合成垫乐"
        assert "风格=" in capsys.readouterr().out
        bgm.close()

    def test_corrupt_library_file_falls_back_to_synth_pad(self, tmp_path, monkeypatch, capsys):
        """曲库里的坏 mp3 解码失败 → 打印回退告警并改用合成垫乐，不放弃 BGM"""
        monkeypatch.setattr(config, "BGM_VOLUME", 0.25)
        lib = tmp_path / "lib"
        lib.mkdir()
        (lib / "broken.mp3").write_text("这不是音频文件", encoding="utf-8")
        monkeypatch.setattr(config, "BGM_DIR", str(lib))

        def _fake_gen(out_path, duration, style=""):
            return _make_wav(out_path, 2.0, freq=200.0)

        monkeypatch.setattr(video, "generate_bgm", _fake_gen)
        final, bgm = video._mix_bgm(self._FakeVideo(), str(tmp_path))
        out = capsys.readouterr().out
        assert bgm is not None, "坏文件应回退合成垫乐而非跳过整个 BGM"
        assert "回退合成垫乐" in out
        bgm.close()

    def test_user_upload_beats_library(self, tmp_path, monkeypatch, capsys):
        """web 上传 BGM 仍是最高优先：曲库选曲完全不被触发"""
        monkeypatch.setattr(config, "BGM_VOLUME", 0.2)
        _library(tmp_path, monkeypatch, [("a.mp3", 300.0)])

        def _boom(*args, **kwargs):
            raise AssertionError("有用户上传 BGM 时不应触发曲库选曲")

        monkeypatch.setattr(video, "pick_bgm_from_library", _boom)
        wav = _make_wav(tmp_path / "user.wav", 2.0)
        final, bgm = video._mix_bgm(self._FakeVideo(), str(tmp_path), user_bgm=wav)
        assert bgm is not None
        assert "用户上传" in capsys.readouterr().out
        bgm.close()

    def test_e2e_library_track_mixed_with_fade(self, proj_dir, tmp_path, monkeypatch):
        """端到端：曲库 mp3 真实混入成片（620Hz 频带出现）且首尾淡入淡出生效"""
        monkeypatch.setattr(config, "VIDEO_FPS", 12)
        monkeypatch.setattr(config, "INTRO_OUTRO", False)
        monkeypatch.setattr(config, "BGM_VOLUME", 0.3)
        _library(tmp_path, monkeypatch, [("e2e_track.mp3", 620.0)])
        img = _make_image(tmp_path / "bgm_lib.jpg", (30, 120, 90))
        segments = [{"text": "一句字幕。", "duration": 2.0, "image_path": img,
                     "audio_path": ""}]
        out = tmp_path / "lib_e2e.mp4"
        assert video.create_video(segments, str(out), width=480, height=270) == str(out)

        stats = _audio_stats(_extract_audio(out, tmp_path / "lib.wav"))
        assert stats["duration"] == pytest.approx(2.0, abs=0.35)
        # 合成垫乐（warm/cool/deep 任一风格）基音都不含 620Hz，该频带能量只能来自曲库曲目
        lib_band = _band_power(stats["arr"], stats["rate"], 560, 700)
        assert lib_band > 0.1 * _band_power(stats["arr"], stats["rate"], 0, 22050), \
            f"成片应包含曲库曲目 620Hz 主音，实际占比 {lib_band / max(_band_power(stats['arr'], stats['rate'], 0, 22050), 1e-12):.4f}"
        # 淡入淡出与曲库共存：片头 0.1s（淡入爬坡）远低于中段稳态电平
        rate, arr = stats["rate"], stats["arr"]
        head = float(np.sqrt((arr[: int(0.1 * rate)] ** 2).mean()))
        mid = float(np.sqrt((arr[int(0.8 * rate): int(1.2 * rate)] ** 2).mean()))
        assert head < 0.5 * mid, f"曲库曲目应有淡入：片头 rms {head:.4f} vs 中段 {mid:.4f}"

    def test_e2e_short_library_track_loops_to_fill(self, proj_dir, tmp_path, monkeypatch):
        """端到端：曲库曲目比成片短时 AudioLoop 循环铺满全片（尾部仍有曲目能量）"""
        monkeypatch.setattr(config, "VIDEO_FPS", 12)
        monkeypatch.setattr(config, "INTRO_OUTRO", False)
        monkeypatch.setattr(config, "BGM_VOLUME", 0.3)
        lib = tmp_path / "short_lib"
        lib.mkdir()
        _ffmpeg_tone(lib / "short.mp3", 620.0, duration=1.2)
        monkeypatch.setattr(config, "BGM_DIR", str(lib))
        img = _make_image(tmp_path / "bgm_short.jpg", (120, 30, 90))
        segments = [{"text": "一句字幕。", "duration": 2.0, "image_path": img,
                     "audio_path": ""}]
        out = tmp_path / "lib_loop.mp4"
        video.create_video(segments, str(out), width=480, height=270)

        rate, arr = _read_wav(_extract_audio(out, tmp_path / "loop.wav"))
        assert len(arr) / rate == pytest.approx(2.0, abs=0.35)
        tail = _band_power(arr[int(1.3 * rate):], rate, 560, 700)
        assert tail > 0, f"1.2s 短曲应循环铺满 2s 成片（尾部仍有 620Hz 能量），实际 {tail:.2f}"
