"""video.py：字体探测、字幕绘制、离线视频合成端到端；--batch-aspect 批量画幅"""
import importlib
import json
import os
import wave

import numpy as np
import pytest
from PIL import Image, ImageFont, ImageDraw
from moviepy import ColorClip, CompositeVideoClip, ImageClip, VideoFileClip

import config
import images
import main
import subtitles
import video


@pytest.fixture
def proj_dir(tmp_path, monkeypatch):
    """把项目目录指到临时目录，避免污染真实 output/"""
    monkeypatch.setattr(config, "PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr(config, "TEMP_DIR", str(tmp_path / "_temp"))
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path))
    # 合成类测试钉回硬切语义（默认值已是 0.6；交叉淡化行为由 TestCrossfade 专项覆盖，
    # 其用例在测试体内自行设置 CROSSFADE_SECONDS，会覆盖此处）
    monkeypatch.setattr(config, "CROSSFADE_SECONDS", 0.0)
    return tmp_path


def _make_image(path, color):
    Image.new("RGB", (640, 360), color).save(path, quality=90)
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


class TestFont:
    def test_finds_real_cjk_font(self):
        font = video._get_system_font(42)
        assert isinstance(font, ImageFont.FreeTypeFont), (
            "应能找到系统 TrueType/OTF 中文字体，而不是退回默认位图字体"
        )

    def test_font_path_env_override(self, monkeypatch, tmp_path):
        font_file = tmp_path / "fake.ttf"
        # 用真实字体文件内容拷贝过来才有效；这里直接用系统探测到的路径验证优先级
        real = video._get_system_font(20)
        real_path = real.path
        monkeypatch.setattr(config, "FONT_PATH", real_path)
        font = video._get_system_font(30)
        assert font.path == real_path


class TestWrapText:
    def test_wraps_long_text(self):
        img = Image.new("RGB", (100, 100))
        draw = ImageDraw.Draw(img)
        font = video._get_system_font(42)
        lines = video._wrap_text("很长很长的一段中文字幕文本" * 5, font, 300, draw)
        assert len(lines) > 1

    def test_short_text_single_line(self):
        img = Image.new("RGB", (100, 100))
        draw = ImageDraw.Draw(img)
        font = video._get_system_font(24)
        lines = video._wrap_text("短句", font, 1000, draw)
        assert lines == ["短句"]


class TestDrawSubtitle:
    def test_draws_and_saves(self, tmp_path):
        src = _make_image(tmp_path / "src.jpg", (30, 30, 30))
        out = tmp_path / "out.jpg"
        video._draw_subtitle_on_image(src, "测试字幕一行", str(out), 640, 360)
        assert out.exists()
        assert out.stat().st_size > 1000


class TestPrepareBaseImage:
    """底图复用重构：同段只 open/resize 一次，块间复制绘制，输出帧与旧逻辑逐字节一致"""

    MULTI_CHUNK_TEXT = "第一句话。第二句话！第三句话？"

    def _segment(self, tmp_path):
        src = _make_image(tmp_path / "seg.jpg", (96, 48, 160))
        return {"text": self.MULTI_CHUNK_TEXT, "duration": 3.0,
                "image_path": src, "audio_path": ""}, src

    def test_prepare_base_image_same_aspect_matches_plain_resize(self, tmp_path):
        """P0-1 后 prepare_base_image 对同画幅输入退化为纯 resize，与旧「open + convert
        + LANCZOS resize」链逐像素一致（cover 裁切只影响非同画幅素材，见 TestImageCoverFit）"""
        src = _make_image(tmp_path / "prep.jpg", (10, 200, 100))
        img = video.prepare_base_image(src, 480, 270)
        ref = Image.open(src).convert("RGB").resize((480, 270), Image.Resampling.LANCZOS)
        assert img.size == (480, 270)
        assert img.mode == "RGB"
        assert np.asarray(img).tobytes() == np.asarray(ref).tobytes()

    def test_render_subtitle_frame_does_not_mutate_base(self, tmp_path):
        """块间复制绘制：画完一块后底图不变，下一块仍从干净底图起步"""
        src = _make_image(tmp_path / "base.jpg", (200, 100, 50))
        base = video.prepare_base_image(src, 480, 270)
        before = np.asarray(base).copy()

        out1 = tmp_path / "f1.jpg"
        video.render_subtitle_frame(base, "第一块字幕", str(out1), 480, 270)
        assert np.asarray(base).tobytes() == before.tobytes(), "底图被绘制污染"

        out2 = tmp_path / "f2.jpg"
        video.render_subtitle_frame(base, "第二块字幕长一些", str(out2), 480, 270)
        assert out1.read_bytes() != out2.read_bytes(), "不同字幕应产出不同帧"

    def test_segment_clip_opens_base_image_once_per_segment(self, tmp_path, monkeypatch):
        """复用测试：多块段落合成只 open 底图 1 次（旧逻辑每块各 open 一次）。

        _image_readable 坏图预检（video.py，A12 遗留修复）每段额外 open 1 次、
        属另一语义，stub 为 True 使本测试只度量「底图准备」的复用。
        """
        monkeypatch.setattr(config, "KEN_BURNS", False)
        monkeypatch.setattr(video, "_image_readable", lambda path: True)
        seg, src = self._segment(tmp_path)
        chunks = subtitles.build_chunk_durations(seg["text"], seg["duration"])
        assert len(chunks) >= 2, "前提：该文案应切成多块，否则本测试失去意义"

        real_open = Image.open
        counter = {"n": 0}

        def counting_open(*args, **kwargs):
            counter["n"] += 1
            return real_open(*args, **kwargs)

        monkeypatch.setattr(Image, "open", counting_open)
        clip = video._build_segment_clip(seg, 0, str(tmp_path), 480, 270)
        total_opens = counter["n"]

        # ImageClip 读帧文件自身也经 PIL（imageio pillow 插件，每帧 1 次 open），测基线扣除
        counter["n"] = 0
        for j in range(len(chunks)):
            ImageClip(str(tmp_path / f"frame_000_{j:02d}.jpg")).close()
        imageclip_opens = counter["n"]

        base_opens = total_opens - imageclip_opens
        assert base_opens == 1, (
            f"整段 {len(chunks)} 块字幕应只 open 底图 1 次："
            f"总 open {total_opens} - ImageClip 读帧 {imageclip_opens} = {base_opens}"
        )
        clip.close()

        # 对照组：旧逻辑（每块重新走 _draw_subtitle_on_image）每块各 open 一次
        counter["n"] = 0
        for chunk_text, _ in chunks:
            video._draw_subtitle_on_image(src, chunk_text, str(tmp_path / "old.jpg"), 480, 270)
        assert counter["n"] == len(chunks), "对照：旧路径每块各 open 一次底图"

    def test_baked_frames_identical_to_old_logic(self, tmp_path, monkeypatch):
        """回归：新路径（同段一次底图 + 副本绘制）每帧与旧逻辑（每块重新 open+resize）逐字节一致

        旧参照 = 保留原语义的 _draw_subtitle_on_image（内部仍是「打开→缩放→绘制→保存」），
        对同一段落的每一块分别渲染；Pillow 的 JPEG 编码对相同像素是字节确定的
        （无时间戳），故可直接比较文件字节。
        """
        monkeypatch.setattr(config, "KEN_BURNS", False)
        seg, src = self._segment(tmp_path)
        chunks = subtitles.build_chunk_durations(seg["text"], seg["duration"])
        assert len(chunks) >= 2

        for style in ("plain", "card"):
            monkeypatch.setattr(config, "SUBTITLE_STYLE", style)
            video._build_segment_clip(seg, 0, str(tmp_path), 480, 270)
            for j, (chunk_text, _dur) in enumerate(chunks):
                new_frame = tmp_path / f"frame_000_{j:02d}.jpg"
                old_frame = tmp_path / f"old_{style}_{j:02d}.jpg"
                video._draw_subtitle_on_image(src, chunk_text, str(old_frame), 480, 270)
                assert new_frame.exists(), f"{style} 第 {j} 帧未产出"
                assert new_frame.read_bytes() == old_frame.read_bytes(), (
                    f"{style} 样式第 {j} 帧与旧逻辑输出不一致"
                )


class TestImageCoverFit:
    """P0-1 图片等比适配（cover）：非同画幅素材不再直接拉伸变形（R1_competitive.md §五 P0-1）。

    判别原理：圆形素材在正确 cover 适配下仍是圆；旧拉伸行为会把 1:1 圆压成
    宽:高 = 目标画幅比的椭圆，|bbox宽-bbox高| 断言必然失败。
    三色带竖图进横屏：正确行为是中心裁切只见中带；旧拉伸会把三条带全压进画面。
    """

    @staticmethod
    def _circle_image(size, radius, color=(255, 255, 255), bg=(0, 0, 0)):
        img = Image.new("RGB", (size, size), bg)
        d = ImageDraw.Draw(img)
        c = size // 2
        d.ellipse([c - radius, c - radius, c + radius, c + radius], fill=color)
        return img

    @staticmethod
    def _band_image():
        """9:16 竖图三横带：上红 / 中绿 / 下蓝"""
        img = Image.new("RGB", (720, 1280), (30, 180, 70))
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, 720, 427], fill=(220, 30, 30))
        d.rectangle([0, 853, 720, 1280], fill=(30, 60, 200))
        return img

    @staticmethod
    def _circle_bbox(arr, thresh=128):
        """帧内白色圆的外接矩形 (宽, 高)"""
        mask = arr.astype(int).sum(axis=2) > thresh * 3
        rows = np.where(mask.any(axis=1))[0]
        cols = np.where(mask.any(axis=0))[0]
        return int(cols[-1] - cols[0] + 1), int(rows[-1] - rows[0] + 1)

    @staticmethod
    def _band_fractions(arr):
        """画面中偏红 / 偏蓝像素占比"""
        a = arr.astype(int)
        r, g, b = a[..., 0], a[..., 1], a[..., 2]
        reddish = float(((r - g > 60) & (r - b > 60)).mean())
        bluish = float(((b - r > 60) & (b - g > 60)).mean())
        return reddish, bluish

    def test_cover_fit_circle_not_distorted(self):
        """1:1 圆形素材进 16:9：中心裁切后仍是圆（旧拉伸会压成 200x113 椭圆）"""
        out = video.fit_image_cover(self._circle_image(480, 100), 480, 270)
        assert out.size == (480, 270)
        w, h = self._circle_bbox(np.asarray(out))
        assert abs(w - h) <= 6, f"圆被拉变形：bbox {w}x{h}"

    def test_cover_fit_upscale_small_source(self):
        """小图（小于目标画幅）等比放大铺满后仍 cover 裁切，不变形不出黑边"""
        out = video.fit_image_cover(self._circle_image(240, 50), 480, 270)
        assert out.size == (480, 270)
        w, h = self._circle_bbox(np.asarray(out))
        assert abs(w - h) <= 6, f"放大路径圆被拉变形：bbox {w}x{h}"

    def test_cover_fit_center_crop_keeps_middle_band(self):
        """9:16 三色带竖图进 16:9：居中裁切只见中带绿；旧拉伸红/蓝各占约 1/3"""
        reddish, bluish = self._band_fractions(np.asarray(video.fit_image_cover(self._band_image(), 480, 270)))
        assert reddish < 0.02, f"画面出现上带红色 {reddish:.1%}：未做中心裁切"
        assert bluish < 0.02, f"画面出现下带蓝色 {bluish:.1%}：未做中心裁切"

    def test_cover_fit_same_aspect_pixel_identical_to_plain_resize(self):
        """同画幅输入（640x360 → 480x270，均 16:9）：cover 与纯 resize 逐像素一致"""
        img = Image.new("RGB", (640, 360), (10, 200, 100))
        out = video.fit_image_cover(img, 480, 270)
        ref = img.resize((480, 270), Image.Resampling.LANCZOS)
        assert np.asarray(out).tobytes() == np.asarray(ref).tobytes()

    def test_cover_fit_extreme_aspect_no_crash(self):
        """极端画幅（1x20000 超窄条，B7 初版会放大出 ≈13.8GB 中间图）不崩、输出正确"""
        out = video.fit_image_cover(Image.new("RGB", (1, 20000), (90, 90, 90)), 480, 270)
        assert out.size == (480, 270)
        out_wide = video.fit_image_cover(Image.new("RGB", (20000, 1), (90, 90, 90)), 480, 270)
        assert out_wide.size == (480, 270)

    def test_cover_fit_no_intermediate_resize(self, monkeypatch):
        """内存防护（B 队检测回归面的根除判据）：任何源画幅下唯一一次 resize 的
        目标恒为输出画幅（先裁后缩）——初版「先缩后裁」对 1x5000 源会请求
        (480, 2400000) ≈3.46GB 中间图，本断言使该回归无法再通过测试。"""
        resize_targets = []
        real_resize = Image.Image.resize

        def spying_resize(self, size, *args, **kwargs):
            resize_targets.append(tuple(size))
            return real_resize(self, size, *args, **kwargs)

        monkeypatch.setattr(Image.Image, "resize", spying_resize)
        for src_size in ((1, 20000), (20000, 1), (1, 5000), (640, 360), (720, 1280)):
            resize_targets.clear()
            out = video.fit_image_cover(Image.new("RGB", src_size, (7, 7, 7)), 480, 270)
            assert out.size == (480, 270)
            assert resize_targets == [(480, 270)], (
                f"源 {src_size} 出现了非输出画幅的中间 resize（内存放大回归）：{resize_targets}"
            )

    def test_prepare_base_image_cover_crops_mismatched_aspect(self, tmp_path):
        """端到端：竖版三带图落盘 → prepare_base_image(480,270) 中心裁切只见中带"""
        src = tmp_path / "portrait.jpg"
        self._band_image().save(src, quality=95)
        out = video.prepare_base_image(str(src), 480, 270)
        assert out.size == (480, 270) and out.mode == "RGB"
        reddish, bluish = self._band_fractions(np.asarray(out))
        assert reddish < 0.02, f"竖图进横屏仍被拉伸（出现红带 {reddish:.1%}）：应中心裁切"
        assert bluish < 0.02, f"竖图进横屏仍被拉伸（出现蓝带 {bluish:.1%}）：应中心裁切"

    def test_ken_burns_background_uses_cover_fit(self, tmp_path):
        """Ken Burns 路径同样走 cover：圆形素材在 zoom / pan 两个动效下都不变形"""
        src = tmp_path / "kb.jpg"
        self._circle_image(480, 100).save(src, quality=95)
        for effect in ("zoom", "pan"):
            bg = video._ken_burns_background(str(src), 480, 270, 2.0, 0.06, effect)
            comp = CompositeVideoClip([bg], size=(480, 270))
            w, h = self._circle_bbox(np.asarray(comp.get_frame(0)))
            comp.close()
            bg.close()
            assert abs(w - h) <= 8, f"{effect} 动效下圆被拉变形：bbox {w}x{h}"


class TestSubtitleStyles:
    """SUBTITLE_STYLE=plain|card 两种字幕样式的渲染：帧文件存在且像素有差异（全部离线）"""

    SRC_COLOR = (200, 200, 200)  # 浅灰底图：黑条/卡片、主题色都与之拉开差距
    SAMPLE_TEXT = "第3天，他说“加油”，坚持了102.5天！"

    def _render(self, tmp_path, style, text=None):
        text = self.SAMPLE_TEXT if text is None else text
        src = _make_image(tmp_path / f"src_{style}.jpg", self.SRC_COLOR)
        out = tmp_path / f"frame_{style}.jpg"
        video._draw_subtitle_on_image(src, text, str(out), 640, 360)
        return out

    @staticmethod
    def _pixels(path):
        return np.asarray(Image.open(path).convert("RGB"), dtype=int)

    def test_default_style_is_plain(self):
        """不配置 SUBTITLE_STYLE 时默认 plain，保持旧行为"""
        assert config.SUBTITLE_STYLE == "plain"

    def test_plain_style_renders_frame(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "SUBTITLE_STYLE", "plain")
        out = self._render(tmp_path, "plain")
        assert out.exists()
        assert out.stat().st_size > 1000

    def test_card_style_renders_frame(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "SUBTITLE_STYLE", "card")
        out = self._render(tmp_path, "card")
        assert out.exists()
        assert out.stat().st_size > 1000

    def test_card_style_pixels_differ_from_plain(self, tmp_path, monkeypatch):
        """同一底图同一文案，card 与 plain 渲染出的帧必须有实质性像素差异"""
        monkeypatch.setattr(config, "SUBTITLE_STYLE", "plain")
        plain = self._render(tmp_path, "plain")
        monkeypatch.setattr(config, "SUBTITLE_STYLE", "card")
        card = self._render(tmp_path, "card")

        a, b = self._pixels(plain), self._pixels(card)
        assert a.shape == b.shape
        changed = int((np.abs(a - b).sum(axis=2) > 15).sum())
        assert changed > 1000, f"两种样式应大面积不同，实际差异像素 {changed}"

    def test_card_style_is_rounded_translucent_card(self, tmp_path, monkeypatch):
        """卡片条：中心比外侧暗（半透明黑卡），圆角顶点保持底图色（未被覆盖）"""
        monkeypatch.setattr(config, "SUBTITLE_STYLE", "card")
        out = self._render(tmp_path, "card", text="一行字")
        img = self._pixels(out)

        cx, cy, cw, _ch = video._card_geometry(1, 640, 360)
        assert cy > 0 and cw < 640, "card 模式卡片应为左右留边的内嵌条"

        inside = img[cy + 30, cx + 10].sum()           # 卡片内左侧（内边距区，避开文字笔画）
        outside = img[cy + 30, 5].sum()                # 同一行的画面左侧（卡片外）
        assert inside < outside - 120, "卡片覆盖区应明显比外侧暗（半透明）"

        corner = img[cy + 2, cx + 2].sum()             # 圆角弧线外的顶点 ≈ 底图
        top_edge = img[cy + 2, cx + cw // 2].sum()     # 顶边直段已被卡片覆盖
        assert corner > 480, f"圆角顶点应接近底图色，实际 {corner}"
        assert top_edge < 380, f"顶边直段应被卡片覆盖变暗，实际 {top_edge}"

    def test_card_style_uses_theme_color(self, tmp_path, monkeypatch):
        """card 模式画面上应大量出现主题色像素（描边 + 高亮）"""
        monkeypatch.setattr(config, "SUBTITLE_STYLE", "card")
        out = self._render(tmp_path, "card")
        img = self._pixels(out)

        theme = np.array(config.subtitle_theme_color())
        close = int((np.abs(img - theme).sum(axis=2) < 90).sum())
        assert close > 50, f"应出现主题色（描边/高亮）像素，实际 {close}"

    def test_plain_style_has_no_theme_color(self, tmp_path, monkeypatch):
        """plain 模式只有黑条白字，不应出现成片主题色（回归保护）"""
        monkeypatch.setattr(config, "SUBTITLE_STYLE", "plain")
        out = self._render(tmp_path, "plain")
        img = self._pixels(out)

        theme = np.array(config.subtitle_theme_color())
        close = int((np.abs(img - theme).sum(axis=2) < 90).sum())
        assert close < 10, f"plain 模式不应出现主题色像素，实际 {close}"

    def test_highlight_runs_numbers_and_quotes(self):
        """高亮切分：数字（含小数）与引号内关键词命中，且拼接后还原原文"""
        text = "第3天，他说“加油”，听了102.5遍，占比30%！"
        runs = video._tokenize_highlight_runs(text)
        highlighted = [t for t, hl in runs if hl]
        assert "3" in highlighted
        assert "“加油”" in highlighted
        assert "102.5" in highlighted
        assert "30%" in highlighted
        assert "".join(t for t, _ in runs) == text

    def test_long_quote_not_highlighted(self, monkeypatch):
        """引号内容超过 SUBTITLE_HIGHLIGHT_QUOTE_MAX 视为整句引用，不高亮"""
        monkeypatch.setattr(config, "SUBTITLE_HIGHLIGHT_QUOTE_MAX", 4)
        runs = video._tokenize_highlight_runs("他说“这是一句很长的话”")
        assert all(not hl for _, hl in runs)

    def test_theme_color_parses_hex_and_fallback(self, monkeypatch):
        monkeypatch.setattr(config, "SUBTITLE_THEME_COLOR", "#FF0000")
        assert config.subtitle_theme_color() == (255, 0, 0)
        monkeypatch.setattr(config, "SUBTITLE_THEME_COLOR", "not-a-color")
        assert config.subtitle_theme_color() == (255, 205, 90)  # 非法值回退默认

    def test_env_int_fallback_on_bad_value(self, monkeypatch):
        """数值样式参数非法时不崩：_env_int 回退默认值"""
        monkeypatch.setenv("SUBTITLE_CARD_OPACITY", "abc")
        assert config._env_int("SUBTITLE_CARD_OPACITY", 170) == 170
        monkeypatch.setenv("SUBTITLE_HIGHLIGHT_QUOTE_MAX", "")
        assert config._env_int("SUBTITLE_HIGHLIGHT_QUOTE_MAX", 24) == 24  # 空串视为未配置
        monkeypatch.setenv("SUBTITLE_CARD_MARGIN", " 80 ")
        assert config._env_int("SUBTITLE_CARD_MARGIN", 60) == 80  # 带空白可正常解析

    def test_bad_numeric_env_does_not_crash_import(self):
        """B队 mustFix 回归：数值参数配错时 import config 不得崩溃（main/web 都 import 它）。
        覆盖两批同类隐患：A10 检出的 7 个样式参数 + B6 首轮修的 SUBTITLE_MAX_CHARS；
        B 队复检又发现 config.py 残留 3 处裸 int()（TTS_SAMPLE_RATE/VIDEO_FPS/WEB_PORT）
        与 5 处裸 float()（TTS_SPEED/TTS_VOLUME/MINIMAX_TTS_SPEED/VOLC_TTS_SPEED_RATIO/
        VOLC_TTS_VOLUME_RATIO），B6 二轮全部补齐守卫后一并纳入本用例。"""
        import subprocess
        import sys

        repo = os.path.dirname(os.path.abspath(config.__file__))
        env = {
            **os.environ,
            "SUBTITLE_MAX_CHARS": "abc",
            "SUBTITLE_CARD_OPACITY": "abc",
            "SUBTITLE_HIGHLIGHT_QUOTE_MAX": "xyz",
            "TTS_SAMPLE_RATE": "abc",
            "VIDEO_FPS": "abc",
            "WEB_PORT": "abc",
            "TTS_SPEED": "abc",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        r = subprocess.run(
            [sys.executable, "-c",
             "import config; print(config.SUBTITLE_MAX_CHARS, "
             "config.SUBTITLE_CARD_OPACITY, config.SUBTITLE_HIGHLIGHT_QUOTE_MAX, "
             "config.TTS_SAMPLE_RATE, config.VIDEO_FPS, config.WEB_PORT, config.TTS_SPEED)"],
            cwd=repo, env=env, capture_output=True, text=True, timeout=60,
        )
        assert r.returncode == 0, f"import config 不应崩溃: {r.stderr}"
        assert "26 170 24 24000 24 5000 1.15" in r.stdout  # 全部非法参数都回退默认值

    def test_card_style_end_to_end(self, proj_dir, tmp_path, monkeypatch):
        """card 样式走完整合成管线（create_video）也能出片"""
        monkeypatch.setattr(config, "VIDEO_FPS", 12)
        monkeypatch.setattr(config, "SUBTITLE_STYLE", "card")
        img = _make_image(tmp_path / "c.jpg", (90, 90, 90))
        segments = [
            {"text": "第7天，他说“坚持”。", "duration": 1.0, "image_path": img, "audio_path": ""},
        ]
        out = tmp_path / "card.mp4"
        result = video.create_video(segments, str(out), width=480, height=270)
        assert result == str(out)
        assert out.exists()
        assert out.stat().st_size > 10_000


class TestKaraokeSubtitle:
    """SUBTITLE_STYLE=karaoke 逐词卡拉OK字幕（D5，R1 §五 P1-2）：
    词级时间戳（seg["words"]，D2 TTS / D3 本地对齐通道）驱动、缺失时块内
    字符比例；Pillow 逐帧渲染当前词主题色高亮，与 plain/card 并存；
    Ken Burns 动效下走差分透明叠加层正常出片。全部离线。"""

    SENTENCE = "你好世界"
    # 词级时间片：两词、每词两字（时间片只给词边界，词内按字符进度插值）
    WORDS = [
        {"word": "你好", "start": 0.0, "end": 0.8},
        {"word": "世界", "start": 0.8, "end": 1.6},
    ]
    SRC_COLOR = (90, 90, 90)
    LONG_TEXT = "今天天气真好我们一起去公园散步看看风景"

    def _karaoke_env(self, monkeypatch):
        monkeypatch.setattr(config, "SUBTITLE_STYLE", "karaoke")

    def _render_active(self, tmp_path, active_word, text=None):
        """渲染一句字幕在 active_word 时刻的卡拉OK帧"""
        text = self.SENTENCE if text is None else text
        src = _make_image(tmp_path / f"karaoke_src_{active_word}.jpg", self.SRC_COLOR)
        base = video.prepare_base_image(src, 480, 270)
        out = tmp_path / f"karaoke_frame_{active_word}.jpg"
        video.render_subtitle_frame(base, text, str(out), 480, 270, active_word=active_word)
        return out

    @staticmethod
    def _pixels(path):
        return np.asarray(Image.open(path).convert("RGB"), dtype=int)

    @staticmethod
    def _changed_pixels(a, b, thresh=15):
        return int((np.abs(a - b).sum(axis=2) > thresh).sum())

    # ---------- 配置解析 ----------

    def test_config_env_parsing_karaoke(self, monkeypatch):
        """SUBTITLE_STYLE=karaoke 合法（大小写归一）；非法值仍回退 plain"""
        before_keys = set(os.environ)
        snapshot = config.SUBTITLE_STYLE
        try:
            monkeypatch.setenv("SUBTITLE_STYLE", "karaoke")
            importlib.reload(config)
            assert config.SUBTITLE_STYLE == "karaoke"

            monkeypatch.setenv("SUBTITLE_STYLE", "KARAOKE")
            importlib.reload(config)
            assert config.SUBTITLE_STYLE == "karaoke"

            monkeypatch.setenv("SUBTITLE_STYLE", "neon")
            importlib.reload(config)
            assert config.SUBTITLE_STYLE == "plain"  # 非法值回退默认，不中断生成
        finally:
            for k in set(os.environ) - before_keys:
                os.environ.pop(k, None)
            config.SUBTITLE_STYLE = snapshot

    # ---------- 词时间表（subtitles.karaoke_*） ----------

    def test_tokens_roundtrip_and_granularity(self):
        """token 拼接严格还原原文；英文/小数整词、中文按字、标点独立"""
        text = "Hello 世界，第102.5天 加油！"
        tokens = subtitles.karaoke_tokens(text)
        assert "".join(tokens) == text, "token 拼接必须还原原文（空白不丢）"
        assert any(t.strip() == "Hello" for t in tokens)   # 拉丁串整词（空白并入邻位 token）
        assert any(t.strip() == "102.5" for t in tokens)   # 小数不拆散
        assert "世" in tokens and "界" in tokens            # 中文按字粒度（KTV 逐字惯例）

    def test_word_timings_project_word_boundaries(self):
        """词级时间片驱动：词边界（0.8s）精确落位，词内按字符插值，铺满块窗口"""
        timings = subtitles.karaoke_chunk_timings([(self.SENTENCE, 1.6)], words=self.WORDS)
        assert len(timings) == 1
        toks = [t for t, _s, _e in timings[0]]
        assert "".join(toks) == self.SENTENCE
        spans = [(s, e) for _t, s, e in timings[0]]
        assert spans[0] == (pytest.approx(0.0), pytest.approx(0.4))   # 你
        assert spans[1] == (pytest.approx(0.4), pytest.approx(0.8))   # 好
        assert spans[2] == (pytest.approx(0.8), pytest.approx(1.2))   # 世（词边界精确）
        assert spans[3] == (pytest.approx(1.2), pytest.approx(1.6))   # 界
        assert spans[0][0] == pytest.approx(0.0)
        assert spans[-1][1] == pytest.approx(1.6)  # 铺满块窗口

    def test_active_index_switches_at_word_boundary(self):
        """时间边界附近切换正确：边界前属前词、边界时刻起属下词（开口即切）"""
        timings = subtitles.karaoke_chunk_timings(
            [(self.SENTENCE, 1.6)], words=self.WORDS)[0]
        assert subtitles.karaoke_active_index(timings, 0.75) == 1   # 好
        assert subtitles.karaoke_active_index(timings, 0.85) == 2   # 世
        assert subtitles.karaoke_active_index(timings, 0.8) == 2    # 0.8s 边界归下词
        assert subtitles.karaoke_active_index(timings, 0.0) == 0
        assert subtitles.karaoke_active_index(timings, 9.9) == 3    # 超尾停在末词

    def test_proportional_fallback_without_words(self):
        """无 words 回退字符比例：块内铺满块窗口，按归一字符长度均分"""
        chunks = [(self.SENTENCE, 1.6), ("再见。", 0.4)]
        timings = subtitles.karaoke_chunk_timings(chunks)  # 不传 words → 字符比例
        spans = [(s, e) for _t, s, e in timings[0]]
        assert len(spans) == 4
        assert spans[0][0] == pytest.approx(0.0)
        assert spans[-1][1] == pytest.approx(1.6)
        assert spans[1] == (pytest.approx(0.4), pytest.approx(0.8))  # 4 字 1.6s → 每字 0.4s

    def test_bad_words_fall_back_proportional(self):
        """坏时间戳（文本对不上/时间超出段时长）与块级同口径回退字符比例"""
        bad = [{"word": "完全不同的一大段文字内容", "start": 0.0, "end": 99.0}]
        chunks = [(self.SENTENCE, 1.6)]
        timings = subtitles.karaoke_chunk_timings(chunks, words=bad)
        assert timings == subtitles.karaoke_timings_proportional(chunks)

    def test_multi_chunk_timings_anchor_to_chunk_window(self):
        """多块：第二块词时间锚定自身显示窗口起点（前序块 pad 后时长之和）"""
        chunks = [(self.SENTENCE, 1.6), ("再见面", 0.9)]
        words = [
            {"word": "你好", "start": 0.0, "end": 0.8},
            {"word": "世界", "start": 0.8, "end": 1.6},
            {"word": "再见", "start": 1.6, "end": 2.2},
            {"word": "面", "start": 2.2, "end": 2.5},
        ]
        timings = subtitles.karaoke_chunk_timings(chunks, words=words)
        second = timings[1]
        assert "".join(t for t, _s, _e in second) == "再见面"
        assert second[0][1] == pytest.approx(0.0, abs=1e-9)  # 锚定块窗口起点
        assert second[-1][2] == pytest.approx(0.9)           # 末 token 到块尾
        # 两块时间无缝衔接：第一块末 token 结束 = 第二块窗口起点（相对各自块为 0）
        assert timings[0][-1][2] == pytest.approx(1.6)

    # ---------- 卡拉OK帧（任务指定：不同时刻帧像素差异 + 时间边界切换） ----------

    def test_frames_differ_between_moments(self, tmp_path, monkeypatch):
        """同一句不同时刻帧像素存在差异（主题色高亮随时间推进移动）"""
        self._karaoke_env(monkeypatch)
        early = self._render_active(tmp_path, 0, text=self.LONG_TEXT)
        late = self._render_active(tmp_path, 8, text=self.LONG_TEXT)
        a, b = self._pixels(early), self._pixels(late)
        assert a.shape == b.shape
        changed = self._changed_pixels(a, b)
        assert changed > 1000, f"不同时刻的卡拉OK帧应大面积不同，实际差异像素 {changed}"

    def test_frames_switch_near_time_boundary(self, tmp_path, monkeypatch):
        """时间边界附近切换正确：0.8s 词边界前后各取一帧，高亮词不同 → 像素不同"""
        self._karaoke_env(monkeypatch)
        timings = subtitles.karaoke_chunk_timings(
            [(self.SENTENCE, 1.6)], words=self.WORDS)[0]
        i_before = subtitles.karaoke_active_index(timings, 0.75)
        i_after = subtitles.karaoke_active_index(timings, 0.85)
        assert i_before == 1 and i_after == 2, "边界前后应分别高亮『好』与『世』"

        f_before = self._render_active(tmp_path, i_before)
        f_after = self._render_active(tmp_path, i_after)
        changed = self._changed_pixels(self._pixels(f_before), self._pixels(f_after))
        assert changed > 500, f"边界前后两帧的高亮词不同，像素应有实质差异，实际 {changed}"

    def test_theme_color_region_grows_with_progress(self, tmp_path, monkeypatch):
        """唱到的词越多，画面上主题色像素越多（已唱+当前词着色的可视化判据）"""
        self._karaoke_env(monkeypatch)
        theme = np.array(config.subtitle_theme_color())
        counts = []
        for k in (0, 2):
            img = self._pixels(self._render_active(tmp_path, k))
            counts.append(int((np.abs(img - theme).sum(axis=2) < 90).sum()))
        assert counts[0] > 0, "卡拉OK帧应出现主题色像素"
        assert counts[1] > counts[0] + 500, (
            f"高亮推进后主题色像素应显著增多：{counts[0]} → {counts[1]}")

    def test_karaoke_style_differs_from_plain(self, tmp_path, monkeypatch):
        """与 plain 并存：同底图同文案，karaoke 帧与 plain 帧像素实质不同"""
        self._karaoke_env(monkeypatch)
        src = _make_image(tmp_path / "cmp_src.jpg", self.SRC_COLOR)

        base = video.prepare_base_image(src, 480, 270)
        karaoke_out = tmp_path / "cmp_karaoke.jpg"
        # 唱到第 10 词：前 10 个 token 主题色 vs plain 全白
        video.render_subtitle_frame(base, self.LONG_TEXT, str(karaoke_out), 480, 270,
                                    active_word=9)

        monkeypatch.setattr(config, "SUBTITLE_STYLE", "plain")
        plain_out = tmp_path / "cmp_plain.jpg"
        video.render_subtitle_frame(base, self.LONG_TEXT, str(plain_out), 480, 270)

        changed = self._changed_pixels(self._pixels(karaoke_out), self._pixels(plain_out))
        assert changed > 1000, f"karaoke 与 plain 应大面积不同，实际差异像素 {changed}"

    def test_overlay_matches_baked_render_karaoke(self, monkeypatch):
        """karaoke 叠加层（Ken Burns 用）与烘焙渲染一致：差分抠像对 karaoke 同样成立"""
        self._karaoke_env(monkeypatch)
        overlay = video._subtitle_overlay_rgba("数字3和加油", 480, 270, active_word=1)
        assert overlay.mode == "RGBA"
        bg = Image.new("RGB", (480, 270), (30, 90, 160))
        composited = Image.alpha_composite(bg.convert("RGBA"), overlay).convert("RGB")
        baked = video._render_subtitle_style(bg.copy(), "数字3和加油", 480, 270, 1)
        diff = np.abs(
            np.asarray(composited, dtype=np.int16) - np.asarray(baked, dtype=np.int16)
        )
        assert diff.max() <= 8, f"叠加与烘焙单点差值过大: {diff.max()}"
        assert diff.mean() < 0.05, f"叠加与烘焙存在系统性偏差: {diff.mean()}"

    # ---------- 上屏单元展开（块 → 逐词帧） ----------

    def test_highlight_segments_cover_silence(self):
        """高亮集合变化点切分：开唱前静音全白(-1)、末词后停在末词、区间无缝"""
        timings = [("你", 0.5, 0.9), ("好", 0.9, 1.3)]
        segments = video._karaoke_highlight_segments(timings, 2.0)
        assert segments[0] == (pytest.approx(0.0), pytest.approx(0.5), -1)  # 开唱前全白
        assert segments[1] == (pytest.approx(0.5), pytest.approx(0.9), 0)
        assert segments[2][2] == 1                                          # 末词
        assert segments[2][1] == pytest.approx(2.0)                         # 尾静音停在末词
        for (_a, b, _i), (a2, _b2, _i2) in zip(segments, segments[1:]):
            assert b == pytest.approx(a2), "相邻区间必须无缝衔接"

    def test_segment_display_units_expand_and_name(self):
        """非 karaoke：单元与块级布局一一对应（旧帧名兼容）；
        karaoke：每块按变化点切成逐词单元，帧名带 _kKK 后缀且无缝铺满"""
        chunks = [("你好。", 1.0)]
        assert video._segment_display_units(chunks, None) == \
            [("你好。", 1.0, None, "")]  # 旧布局一一对应

        timings = [[("你", 0.2, 0.6), ("好。", 0.6, 1.0)]]
        units = video._segment_display_units(chunks, timings)
        assert units[0][:3] == ("你好。", pytest.approx(0.2), -1)   # 开唱前全白
        assert [u[2] for u in units] == [-1, 0, 1]
        assert all(u[0] == "你好。" for u in units)                  # 每帧都是整句文本
        assert [u[3] for u in units] == ["_k00", "_k01", "_k02"]    # 帧名后缀
        assert sum(u[1] for u in units) == pytest.approx(1.0)       # 无缝铺满块窗口

    # ---------- 缓存快照 ----------

    def test_karaoke_cache_snapshot_roundtrip(self, tmp_path, monkeypatch):
        """karaoke 快照含词表：词表一致可复用、词表漂移/旧版快照不复用；
        非 karaoke 样式忽略快照中的 karaoke 字段（plain/card 缓存行为不变）"""
        monkeypatch.setattr(config, "SUBTITLE_STYLE", "karaoke")
        chunks = [(self.SENTENCE, 1.6)]
        karaoke = subtitles.karaoke_chunk_timings(chunks, words=self.WORDS)

        video._save_chunks_meta(0, "", str(tmp_path), chunks, karaoke)
        assert video._cached_chunks_match(0, "", str(tmp_path), chunks, karaoke)

        # 词表漂移（不均分词 durations ≠ 比例兜底词表）→ 不得复用旧帧
        skewed_words = [{"word": "你好", "start": 0.0, "end": 1.2},
                        {"word": "世界", "start": 1.2, "end": 1.6}]
        other = subtitles.karaoke_chunk_timings(chunks, words=skewed_words)
        assert other != karaoke, "前提：不均分词时间表应与均分比例词表不同"
        assert not video._cached_chunks_match(0, "", str(tmp_path), chunks, other)

        # 旧版快照（无 karaoke 字段，D5 之前落盘）不得复用
        with open(tmp_path / "_chunks_002.json", "w", encoding="utf-8") as f:
            json.dump({"chunks": video._chunks_layout(chunks)}, f, ensure_ascii=False)
        assert not video._cached_chunks_match(2, "", str(tmp_path), chunks, karaoke)

        # 换样式（karaoke → plain）不得复用：样式变了帧内容就变，快照带 style 一起比对
        monkeypatch.setattr(config, "SUBTITLE_STYLE", "plain")
        assert not video._cached_chunks_match(0, "", str(tmp_path), chunks, karaoke)
        # 同样式（plain → plain）且快照也记录 plain 时才复用
        video._save_chunks_meta(1, "", str(tmp_path), chunks, karaoke)
        assert video._cached_chunks_match(1, "", str(tmp_path), chunks, karaoke)

    # ---------- 端到端（真实合成，两条合成路径） ----------

    def test_e2e_baked_karaoke_renders_word_frames(self, proj_dir, tmp_path, monkeypatch):
        """烘焙路径（KEN_BURNS=0）× karaoke：出片成功，逐词帧落盘且多于块数"""
        monkeypatch.setattr(config, "VIDEO_FPS", 12)
        monkeypatch.setattr(config, "KEN_BURNS", False)
        monkeypatch.setattr(config, "INTRO_OUTRO", False)
        self._karaoke_env(monkeypatch)
        img = _make_image(tmp_path / "kbk.jpg", (80, 40, 120))
        segments = [{"text": "第一句话。第二句话！", "duration": 2.0,
                     "image_path": img, "audio_path": ""}]
        out = tmp_path / "karaoke_baked.mp4"
        result = video.create_video(segments, str(out), width=480, height=270)
        assert result == str(out)
        assert out.exists()
        # 纯色底 2s@12fps 压缩率高（实测 ~7.6KB），只要求真实非空且可解码
        assert out.stat().st_size > 5_000
        frames = sorted(p.name for p in tmp_path.glob("frame_000_*.jpg"))
        assert len(frames) > 2, "karaoke 应按词切成多帧（多于块数 2）"
        assert all("_k" in name for name in frames), f"karaoke 帧名应带 _k 后缀: {frames[:3]}"

    def test_e2e_kenburns_karaoke(self, proj_dir, tmp_path, monkeypatch):
        """Ken Burns 动效 × karaoke：逐词透明叠加层正常出片、时长不漂移"""
        monkeypatch.setattr(config, "VIDEO_FPS", 12)
        monkeypatch.setattr(config, "INTRO_OUTRO", False)
        monkeypatch.setattr(config, "KEN_BURNS", True)
        monkeypatch.setattr(config, "KEN_BURNS_STRENGTH", 0.06)
        self._karaoke_env(monkeypatch)
        img = _make_image(tmp_path / "kbk2.jpg", (40, 120, 40))
        segments = [{"text": "第一句话。第二句话！", "duration": 2.0,
                     "image_path": img, "audio_path": ""}]
        out = tmp_path / "karaoke_kb.mp4"
        result = video.create_video(segments, str(out), width=480, height=270)
        assert result == str(out)
        assert out.exists()
        assert out.stat().st_size > 5_000  # 纯色底压缩率高，要求真实非空且可解码
        assert list(tmp_path.glob("overlay_000*_k*.png")), "逐词叠加层应落盘"
        with VideoFileClip(str(out)) as v:
            assert v.duration == pytest.approx(2.0, abs=0.35)

    def test_kenburns_karaoke_uses_word_timestamps(self, tmp_path, monkeypatch):
        """seg["words"] 驱动（Ken Burns 叠加层）：开唱前静音全白、词边界按真实
        开口时间切换、末词后停在末词——叠加层起点/时长逐层断言"""
        self._karaoke_env(monkeypatch)
        monkeypatch.setattr(config, "KEN_BURNS", True)
        img = _make_image(tmp_path / "kbw.jpg", (40, 90, 120))
        seg = {"text": self.SENTENCE, "duration": 2.0, "image_path": img, "audio_path": "",
               "words": [{"word": "你好", "start": 0.5, "end": 1.0},
                         {"word": "世界", "start": 1.0, "end": 1.6}]}
        clip = video._build_segment_clip(seg, 0, str(tmp_path), 480, 270)
        try:
            overlays = clip.clips[1:]
            # 高亮集合只在词开口时刻变化：开唱前静音 [0,0.5) 全白 →
            # 你[0.5,0.75) 好[0.75,1.0) 世[1.0,1.3) → 界[1.3 起，尾静音 [1.6,2.0) 一并停在末词
            assert [round(o.start, 3) for o in overlays] == [0.0, 0.5, 0.75, 1.0, 1.3]
            assert [round(o.duration, 3) for o in overlays] == [0.5, 0.25, 0.25, 0.3, 0.7]
            assert clip.duration == pytest.approx(2.0)
        finally:
            clip.close()


class TestCreateVideoEndToEnd:
    """离线端到端：真实合成一个小 MP4，不依赖任何 API"""

    def test_two_segments_silent_video(self, proj_dir, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "VIDEO_FPS", 12)
        img1 = _make_image(tmp_path / "a.jpg", (120, 40, 40))
        img2 = _make_image(tmp_path / "b.jpg", (40, 120, 40))
        segments = [
            {"text": "第一段字幕。这里还有第二句话！", "duration": 1.2, "image_path": img1, "audio_path": ""},
            {"text": "第二段只有一句话。", "duration": 0.8, "image_path": img2, "audio_path": ""},
        ]
        out = tmp_path / "final.mp4"
        result = video.create_video(segments, str(out), width=480, height=270)
        assert result == str(out)
        assert out.exists()
        assert out.stat().st_size > 10_000

    def test_missing_image_uses_black_frame(self, proj_dir, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "VIDEO_FPS", 12)
        segments = [
            {"text": "没有配图的段落。", "duration": 0.6, "image_path": "", "audio_path": ""},
        ]
        out = tmp_path / "black.mp4"
        video.create_video(segments, str(out), width=480, height=270)
        assert out.exists()

    def test_no_segments_raises(self, proj_dir, tmp_path):
        with pytest.raises(ValueError):
            video.create_video([], str(tmp_path / "x.mp4"), width=480, height=270)


class TestIntroOutroCards:
    """片头/片尾卡：时长、渐变底、文案优先级、SRT 时间轴同步（全部离线）"""

    def test_gradient_helper_top_to_bottom(self):
        img = video._make_gradient_image(10, 100, (0, 0, 0), (255, 255, 255))
        assert img.size == (10, 100)
        assert img.getpixel((5, 0)) == (0, 0, 0)
        assert img.getpixel((5, 99)) == (255, 255, 255)

    def test_card_clips_have_fixed_durations(self, tmp_path):
        intro = video._build_intro_card(480, 270, str(tmp_path), "测试主题")
        outro = video._build_outro_card(480, 270, str(tmp_path), "关注我")
        assert intro.duration == pytest.approx(video.INTRO_CARD_DURATION)  # 2.5s
        assert outro.duration == pytest.approx(video.OUTRO_CARD_DURATION)  # 2.0s
        assert (tmp_path / "_intro_card.jpg").exists()
        assert (tmp_path / "_outro_card.jpg").exists()

    def test_card_frame_is_gradient(self, tmp_path):
        out = tmp_path / "card.jpg"
        video._build_card_frame("标题", 480, 270, str(out), ((0, 0, 40), (200, 200, 255)), 36)
        img = Image.open(out)
        top = img.getpixel((240, 2))
        bottom = img.getpixel((240, 267))
        assert sum(top) < sum(bottom) - 100, "卡片底应为上深下浅的渐变"

    def test_resolve_card_texts_priority(self, monkeypatch):
        monkeypatch.setattr(config, "INTRO_TEXT", "自定义片头")
        monkeypatch.setattr(config, "OUTRO_TEXT", "")
        intro, outro = video._resolve_card_texts("视频主题")
        assert intro == "自定义片头"  # INTRO_TEXT 配置优先于主题
        assert outro  # OUTRO_TEXT 为空时回退内置引导关注文案

        monkeypatch.setattr(config, "INTRO_TEXT", "")
        intro, _ = video._resolve_card_texts("视频主题")
        assert intro == "视频主题"  # 没配 INTRO_TEXT 时用主题大字

        monkeypatch.setattr(config, "INTRO_TEXT", " ")
        intro, _ = video._resolve_card_texts("")
        assert intro == video._DEFAULT_INTRO_TEXT  # 全空时兜底

    def test_intro_offset_and_cards_duration_toggle(self, monkeypatch):
        monkeypatch.setattr(config, "INTRO_OUTRO", True)
        assert video.intro_offset() == pytest.approx(1.8)
        assert video.cards_duration() == pytest.approx(3.3)
        monkeypatch.setattr(config, "INTRO_OUTRO", False)
        assert video.intro_offset() == 0.0
        assert video.cards_duration() == 0.0

    def test_total_duration_includes_cards(self, proj_dir, tmp_path, monkeypatch):
        """成片总时长 = 原时长 + 片头卡 2.5s + 片尾卡 2.0s"""
        monkeypatch.setattr(config, "VIDEO_FPS", 12)
        monkeypatch.setattr(config, "INTRO_OUTRO", True)
        img = _make_image(tmp_path / "a.jpg", (10, 60, 120))
        segments = [
            {"text": "只有一句话。", "duration": 2.0, "image_path": img, "audio_path": ""},
        ]
        out = tmp_path / "with_cards.mp4"
        video.create_video(segments, str(out), width=480, height=270, topic="测试主题")
        with VideoFileClip(str(out)) as v:
            assert v.duration == pytest.approx(
                2.0 + video.INTRO_CARD_DURATION + video.OUTRO_CARD_DURATION, abs=0.35
            )

    def test_total_duration_without_cards(self, proj_dir, tmp_path, monkeypatch):
        """INTRO_OUTRO 关闭后成片时长 = 原时长"""
        monkeypatch.setattr(config, "VIDEO_FPS", 12)
        monkeypatch.setattr(config, "INTRO_OUTRO", False)
        segments = [
            {"text": "只有一句话。", "duration": 2.0, "image_path": "", "audio_path": ""},
        ]
        out = tmp_path / "no_cards.mp4"
        video.create_video(segments, str(out), width=480, height=270)
        with VideoFileClip(str(out)) as v:
            assert v.duration == pytest.approx(2.0, abs=0.35)

    def test_srt_first_entry_starts_at_intro_length(self, proj_dir, tmp_path, monkeypatch):
        """SRT 首条字幕起始时间 = 片头卡长（1.8s），与成片对齐"""
        monkeypatch.setattr(config, "INTRO_OUTRO", True)
        segments = [
            {"text": "第一句。", "duration": 1.0},
            {"text": "第二句。", "duration": 1.0},
        ]
        srt = tmp_path / "sub.srt"
        subtitles.generate_srt(segments, str(srt), offset=video.intro_offset())
        first_block = srt.read_text(encoding="utf-8").strip().split("\n\n")[0]
        assert first_block.splitlines()[1] == "00:00:01,800 --> 00:00:02,800"

    def test_srt_default_offset_keeps_original_timeline(self, tmp_path):
        """不传 offset（INTRO_OUTRO 关闭时 intro_offset()=0）保持旧时间轴，兼容旧调用"""
        segments = [{"text": "第一句。", "duration": 2.0}]
        srt = tmp_path / "sub.srt"
        subtitles.generate_srt(segments, str(srt))
        first_block = srt.read_text(encoding="utf-8").strip().split("\n\n")[0]
        assert first_block.splitlines()[1].startswith("00:00:00,000 -->")


class TestKenBurns:
    """Ken Burns 动效：开关/幅度配置、动效背景、透明字幕叠加层、离线端到端（全部离线）"""

    KB_SEGMENTS = [
        {"text": "第一段第一句。第一段还有第二句！", "duration": 1.2, "image_path": "", "audio_path": ""},
        {"text": "第二段只有一句话。", "duration": 0.8, "image_path": "", "audio_path": ""},
    ]

    def _kb_segments(self, tmp_path):
        segs = [dict(s) for s in self.KB_SEGMENTS]
        segs[0]["image_path"] = _make_image(tmp_path / "kb_a.jpg", (120, 40, 40))
        segs[1]["image_path"] = _make_image(tmp_path / "kb_b.jpg", (40, 120, 40))
        return segs

    def _kb_env(self, monkeypatch, enabled=True):
        monkeypatch.setattr(config, "VIDEO_FPS", 12)
        monkeypatch.setattr(config, "INTRO_OUTRO", False)
        monkeypatch.setattr(config, "KEN_BURNS", enabled)
        monkeypatch.setattr(config, "KEN_BURNS_STRENGTH", 0.06)

    def test_config_env_parsing_on_off_and_strength(self, monkeypatch):
        """KEN_BURNS=1 开 / 0|off 关；KEN_BURNS_STRENGTH 按浮点解析（reload config 验证）"""
        before_keys = set(os.environ)
        snapshot = {k: getattr(config, k) for k in ("KEN_BURNS", "KEN_BURNS_STRENGTH")}
        try:
            monkeypatch.setenv("KEN_BURNS", "1")
            importlib.reload(config)
            assert config.KEN_BURNS is True

            monkeypatch.setenv("KEN_BURNS", "0")
            importlib.reload(config)
            assert config.KEN_BURNS is False

            monkeypatch.setenv("KEN_BURNS", "off")
            importlib.reload(config)
            assert config.KEN_BURNS is False

            monkeypatch.setenv("KEN_BURNS", "1")
            monkeypatch.setenv("KEN_BURNS_STRENGTH", "0.12")
            importlib.reload(config)
            assert config.KEN_BURNS_STRENGTH == pytest.approx(0.12)
        finally:
            # 还原环境变量与 config 模块属性，不污染其它用例
            for k in set(os.environ) - before_keys:
                os.environ.pop(k, None)
            for k, v in snapshot.items():
                setattr(config, k, v)

    def test_env_float_fallback_on_bad_value(self, monkeypatch):
        """KEN_BURNS_STRENGTH 非法值回退默认：_env_float 不抛异常"""
        monkeypatch.setenv("KB_TEST_FLOAT", "abc")
        assert config._env_float("KB_TEST_FLOAT", 0.06) == pytest.approx(0.06)
        monkeypatch.setenv("KB_TEST_FLOAT", "")
        assert config._env_float("KB_TEST_FLOAT", 0.06) == pytest.approx(0.06)  # 空串视为未配置
        monkeypatch.setenv("KB_TEST_FLOAT", " 0.12 ")
        assert config._env_float("KB_TEST_FLOAT", 0.06) == pytest.approx(0.12)  # 带空白可解析
        monkeypatch.delenv("KB_TEST_FLOAT")
        assert config._env_float("KB_TEST_FLOAT", 0.06) == pytest.approx(0.06)  # 缺失回退

    def test_bad_strength_env_does_not_crash_import(self):
        """B队 mustFix 回归：KEN_BURNS_STRENGTH 配非数值时 import config 不得崩溃"""
        import subprocess
        import sys

        repo = os.path.dirname(os.path.abspath(config.__file__))
        env = {
            **os.environ,
            "KEN_BURNS_STRENGTH": "abc",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        r = subprocess.run(
            [sys.executable, "-c", "import config; print(config.KEN_BURNS_STRENGTH)"],
            cwd=repo, env=env, capture_output=True, text=True, timeout=60,
        )
        assert r.returncode == 0, f"import config 不应崩溃: {r.stderr}"
        assert "0.06" in r.stdout  # 非法值回退默认 0.06

    def test_strength_clamped(self, monkeypatch):
        monkeypatch.setattr(config, "KEN_BURNS_STRENGTH", 0.2)
        assert video._ken_burns_strength() == pytest.approx(0.2)
        monkeypatch.setattr(config, "KEN_BURNS_STRENGTH", -0.5)
        assert video._ken_burns_strength() == 0.0  # 负值 → 静止画面
        monkeypatch.setattr(config, "KEN_BURNS_STRENGTH", 99)
        assert video._ken_burns_strength() == pytest.approx(0.5)  # 上限保护

    def test_effect_alternates_by_index(self):
        assert video._ken_burns_effect(0) == "zoom"
        assert video._ken_burns_effect(1) == "pan"
        assert video._ken_burns_effect(2) == "zoom"

    def test_zoom_background_grows_and_stays_centered(self, tmp_path):
        """缓推：画面从原尺寸放大到 1+strength，位置从 (0,0) 到反向半余量（居中）"""
        img = _make_image(tmp_path / "kb_zoom.jpg", (10, 120, 60))
        clip = video._ken_burns_background(img, 200, 100, 2.0, 0.06, "zoom")
        assert clip.duration == pytest.approx(2.0)
        assert clip.get_frame(0.0).shape == (100, 200, 3)
        assert clip.get_frame(2.0).shape == (106, 212, 3)  # 放大 6%
        assert clip.pos(0.0) == (0.0, 0.0)
        x, y = clip.pos(2.0)
        assert x == pytest.approx(-6.0)   # -(200*0.06)/2
        assert y == pytest.approx(-3.0)   # -(100*0.06)/2

    def test_pan_background_constant_scale_slides(self, tmp_path):
        """横移：固定放大 1+strength，x 从 0 滑到 -余量，y 居中"""
        img = _make_image(tmp_path / "kb_pan.jpg", (60, 10, 120))
        clip = video._ken_burns_background(img, 200, 100, 2.0, 0.06, "pan")
        assert clip.get_frame(0.0).shape == (106, 212, 3)
        assert clip.get_frame(2.0).shape == (106, 212, 3)  # 尺寸恒定
        assert clip.pos(0.0)[0] == pytest.approx(0.0)
        assert clip.pos(2.0)[0] == pytest.approx(-12.0)  # -200*0.06
        assert clip.pos(2.0)[1] == pytest.approx(-3.0)   # y 居中

    def test_overlay_rgba_transparency_layout(self):
        """叠加层：字幕条以上全透明，条内半透明，文字笔画不透明"""
        overlay = video._subtitle_overlay_rgba("一句字幕", 480, 270)
        assert overlay.mode == "RGBA"
        arr = np.asarray(overlay)
        # 480×270 横屏：bar_y = 270-(54+40)-60 = 116（R5 起横屏底边距 60），其上应全透明
        assert arr[:110, :, 3].max() == 0
        # 字幕条底部远离文字 → 半透明（plain 黑条 alpha≈160）
        bar_alpha = int(arr[268, 240, 3])
        assert 120 <= bar_alpha <= 200, f"字幕条应为半透明，实际 {bar_alpha}"
        # 全图应存在不透明像素（文字/描边）
        assert arr[:, :, 3].max() == 255


    def test_overlay_matches_baked_render_on_any_background(self, monkeypatch):
        """叠加层合成到任意底色上应与直接烘焙渲染一致（差分抠像数学等价）"""
        for style in ("plain", "card"):
            monkeypatch.setattr(config, "SUBTITLE_STYLE", style)
            overlay = video._subtitle_overlay_rgba("数字3和“加油”", 480, 270)
            bg = Image.new("RGB", (480, 270), (30, 90, 160))
            composited = Image.alpha_composite(bg.convert("RGBA"), overlay).convert("RGB")
            baked = video._render_subtitle_style(bg.copy(), "数字3和“加油”", 480, 270)
            diff = np.abs(
                np.asarray(composited, dtype=np.int16) - np.asarray(baked, dtype=np.int16)
            )
            # 允许差分抠像 + PIL 整数舍入带来的 ±1~2 通道误差（仅出现在字形抗锯齿边缘）；
            # 均值阈值捕获任何系统性偏差
            assert diff.max() <= 8, f"{style} 样式叠加与烘焙渲染单点差值过大: {diff.max()}"
            assert diff.mean() < 0.05, f"{style} 样式叠加与烘焙渲染存在系统性偏差: {diff.mean()}"

    def test_segment_clip_duration_unchanged(self, proj_dir, tmp_path, monkeypatch):
        """Ken Burns 开启/关闭，片段时长都等于段落时长（逐句叠加不改时间轴）"""
        self._kb_env(monkeypatch, enabled=True)
        img = _make_image(tmp_path / "kb_seg.jpg", (90, 30, 120))
        seg = {"text": "第一句话。第二句话！第三句话？", "duration": 2.4,
               "image_path": img, "audio_path": ""}
        clip = video._build_segment_clip(seg, 0, str(tmp_path), 480, 270)
        assert clip.duration == pytest.approx(2.4)

        monkeypatch.setattr(config, "KEN_BURNS", False)
        clip_off = video._build_segment_clip(seg, 0, str(tmp_path), 480, 270)
        assert clip_off.duration == pytest.approx(2.4)

    def test_e2e_kenburns_on_composes_and_duration_held(self, proj_dir, tmp_path, monkeypatch):
        """开启 Ken Burns 离线合成成功：尺寸不变、成片时长 = 片段时长之和"""
        self._kb_env(monkeypatch, enabled=True)
        out = tmp_path / "kb.mp4"
        result = video.create_video(self._kb_segments(tmp_path), str(out), width=480, height=270)
        assert result == str(out)
        assert out.exists()
        assert out.stat().st_size > 10_000
        with VideoFileClip(str(out)) as v:
            assert v.size == [480, 270]
            assert v.duration == pytest.approx(2.0, abs=0.35)

    def test_e2e_kenburns_off_still_works(self, proj_dir, tmp_path, monkeypatch):
        """KEN_BURNS=0 走旧烘焙路径：合成仍成功、时长不变（回归保护）"""
        self._kb_env(monkeypatch, enabled=False)
        out = tmp_path / "nokb.mp4"
        video.create_video(self._kb_segments(tmp_path), str(out), width=480, height=270)
        assert out.exists()
        with VideoFileClip(str(out)) as v:
            assert v.duration == pytest.approx(2.0, abs=0.35)

    def test_e2e_kenburns_with_card_style(self, proj_dir, tmp_path, monkeypatch):
        """card 字幕样式 × Ken Burns 组合也能出片（差分叠加层对任意样式透明兼容）"""
        self._kb_env(monkeypatch, enabled=True)
        monkeypatch.setattr(config, "SUBTITLE_STYLE", "card")
        out = tmp_path / "kb_card.mp4"
        video.create_video(self._kb_segments(tmp_path), str(out), width=480, height=270)
        assert out.exists()
        assert out.stat().st_size > 10_000

    def test_e2e_kenburns_survives_overlay_failure(self, proj_dir, tmp_path, monkeypatch):
        """叠加层渲染失败时降级为无字幕动效背景，合成不中断"""
        self._kb_env(monkeypatch, enabled=True)

        def _boom(*args, **kwargs):
            raise RuntimeError("叠加层渲染失败")

        monkeypatch.setattr(video, "_subtitle_overlay_rgba", _boom)
        segments = [{"text": "这一句字幕会渲染失败。", "duration": 1.0,
                     "image_path": _make_image(tmp_path / "c.jpg", (10, 10, 10)),
                     "audio_path": ""}]
        out = tmp_path / "degrade.mp4"
        video.create_video(segments, str(out), width=480, height=270)
        assert out.exists()


class TestSubtitleBottomMargin:
    """质量循环 R5：字幕条底边距安全区（竖屏 240 / 横屏 60,可配置）"""

    def test_auto_margin_portrait_and_landscape(self):
        """竖屏取 240（避开平台底部 UI），横屏取 60"""
        assert video._subtitle_bottom_margin(540, 960) == 240
        assert video._subtitle_bottom_margin(480, 270) == 60

    def test_explicit_config_wins(self, monkeypatch):
        monkeypatch.setattr(config, "SUBTITLE_BOTTOM_MARGIN", 300)
        assert video._subtitle_bottom_margin(540, 960) == 300
        monkeypatch.setattr(config, "SUBTITLE_BOTTOM_MARGIN", 0)
        assert video._subtitle_bottom_margin(540, 960) == 240  # 0 = 回自动

    def test_portrait_overlay_bar_above_platform_ui(self):
        """竖屏叠加层：字幕文字（不透明像素）整体位于底部 240px 安全区之上
        （半透明底条按设计延伸到画面底边，平台 UI 反正覆盖在视频之上）"""
        overlay = video._subtitle_overlay_rgba("一句字幕", 540, 960)
        arr = np.asarray(overlay)
        rows = np.where((arr[:, :, 3] == 255).any(axis=1))[0]  # 文字笔画 = 全不透明
        assert rows.max() <= 960 - 240 + 25, (
            f"字幕文字侵入底部安全区:最低行 {rows.max()} > {960 - 240}")


class TestCrossfade:
    """段间交叉淡化：配置解析、布局计算、成片时长公式、声画同步、SRT 时间轴（全部离线）"""

    XF_SEGMENTS = [
        {"text": "第一段只有一句话。", "duration": 2.0, "image_path": "", "audio_path": ""},
        {"text": "第二段只有一句话。", "duration": 2.0, "image_path": "", "audio_path": ""},
    ]

    def _segments(self, tmp_path):
        segs = [dict(s) for s in self.XF_SEGMENTS]
        segs[0]["image_path"] = _make_image(tmp_path / "xf_a.jpg", (180, 40, 40))
        segs[1]["image_path"] = _make_image(tmp_path / "xf_b.jpg", (40, 180, 40))
        return segs

    def _env(self, monkeypatch, xfade=0.5):
        monkeypatch.setattr(config, "VIDEO_FPS", 12)
        monkeypatch.setattr(config, "INTRO_OUTRO", False)
        monkeypatch.setattr(config, "CROSSFADE_SECONDS", xfade)

    # ---------- 配置解析 ----------

    def test_crossfade_seconds_helper_clamps(self, monkeypatch):
        monkeypatch.setattr(config, "CROSSFADE_SECONDS", 0.8)
        assert video.crossfade_seconds() == pytest.approx(0.8)
        monkeypatch.setattr(config, "CROSSFADE_SECONDS", -3.0)
        assert video.crossfade_seconds() == 0.0  # 负值视为关闭

    def test_config_env_parsing(self, monkeypatch):
        """CROSSFADE_SECONDS 环境变量解析：浮点正常读、非法值回退默认 0.6、负值视为关闭（reload 验证）"""
        before_keys = set(os.environ)
        snapshot = config.CROSSFADE_SECONDS
        try:
            monkeypatch.setenv("CROSSFADE_SECONDS", "0.75")
            importlib.reload(config)
            assert config.CROSSFADE_SECONDS == pytest.approx(0.75)

            monkeypatch.setenv("CROSSFADE_SECONDS", "abc")
            importlib.reload(config)
            assert config.CROSSFADE_SECONDS == pytest.approx(0.6)  # 非法值回退默认，不中断生成

            monkeypatch.setenv("CROSSFADE_SECONDS", "-2")
            importlib.reload(config)
            assert config.CROSSFADE_SECONDS == 0.0  # 负值视为关闭
        finally:
            for k in set(os.environ) - before_keys:
                os.environ.pop(k, None)
            config.CROSSFADE_SECONDS = snapshot

    # ---------- 布局计算 ----------

    def test_layout_starts_overlap_and_total(self):
        overlaps, starts = video._crossfade_layout([2.0, 2.0, 2.0], 0.5)
        assert overlaps == [pytest.approx(0.5), pytest.approx(0.5)]
        assert starts == [pytest.approx(0.0), pytest.approx(1.5), pytest.approx(3.0)]
        # 最终正片时长 = 各段之和 - 重叠
        assert starts[-1] + 2.0 == pytest.approx(6.0 - 2 * 0.5)

    def test_layout_disabled_is_sequential(self):
        overlaps, starts = video._crossfade_layout([2.0, 3.0], 0.0)
        assert overlaps == []
        assert starts == [pytest.approx(0.0), pytest.approx(2.0)]

    def test_layout_clamped_by_short_segment(self):
        """段长不足时重叠按两段中较短者截断，后段起点不会早于前段"""
        overlaps, starts = video._crossfade_layout([1.0, 1.0], 5.0)
        assert overlaps == [pytest.approx(1.0)]
        assert starts == [pytest.approx(0.0), pytest.approx(0.0)]

    def test_overlap_total(self, monkeypatch):
        monkeypatch.setattr(config, "CROSSFADE_SECONDS", 0.5)
        assert video.crossfade_overlap_total([2.0, 2.0, 2.0]) == pytest.approx(1.0)
        monkeypatch.setattr(config, "CROSSFADE_SECONDS", 0)
        assert video.crossfade_overlap_total([2.0, 2.0]) == 0.0

    # ---------- 端到端（真实合成，不依赖任何 API） ----------

    def test_e2e_duration_minus_overlap(self, proj_dir, tmp_path, monkeypatch):
        """成片总时长 = 各段之和 - 段间重叠（无卡片，交叉淡化 0.5s）"""
        self._env(monkeypatch, xfade=0.5)
        out = tmp_path / "xf.mp4"
        result = video.create_video(self._segments(tmp_path), str(out), width=480, height=270)
        assert result == str(out)
        assert out.exists()
        assert out.stat().st_size > 10_000
        with VideoFileClip(str(out)) as v:
            assert v.duration == pytest.approx(4.0 - 0.5, abs=0.35)

    def test_e2e_duration_with_cards(self, proj_dir, tmp_path, monkeypatch):
        """开片头片尾卡 + 交叉淡化：总时长 = 卡片 + 各段之和 - 重叠"""
        self._env(monkeypatch, xfade=0.5)
        monkeypatch.setattr(config, "INTRO_OUTRO", True)
        out = tmp_path / "xfc.mp4"
        video.create_video(self._segments(tmp_path), str(out), width=480, height=270,
                           topic="交叉淡化")
        with VideoFileClip(str(out)) as v:
            assert v.duration == pytest.approx(
                video.cards_duration() + 4.0 - 0.5, abs=0.35
            )

    def test_e2e_single_segment_ignores_crossfade(self, proj_dir, tmp_path, monkeypatch):
        """只有一段时不启用淡化，走旧拼接路径，时长不变"""
        self._env(monkeypatch, xfade=0.5)
        segments = [{"text": "只有一段。", "duration": 1.5,
                     "image_path": _make_image(tmp_path / "solo.jpg", (10, 10, 120)),
                     "audio_path": ""}]
        out = tmp_path / "solo.mp4"
        video.create_video(segments, str(out), width=480, height=270)
        with VideoFileClip(str(out)) as v:
            assert v.duration == pytest.approx(1.5, abs=0.35)

    def test_e2e_midpoint_frame_blends_both_segments(self, proj_dir, tmp_path, monkeypatch):
        """重叠区中点帧应为两段画面的中间色（画面淡入生效）；重叠区外仍是纯色"""
        self._env(monkeypatch, xfade=0.5)
        monkeypatch.setattr(config, "KEN_BURNS", False)  # 纯色帧，排除动效干扰
        out = tmp_path / "xf_blend.mp4"
        video.create_video(self._segments(tmp_path), str(out), width=480, height=270)
        with VideoFileClip(str(out)) as v:
            red = v.get_frame(0.5)[40, 60].astype(int)     # 第一段独占区：纯红
            mid = v.get_frame(1.75)[40, 60].astype(int)    # 重叠中点（1.5~2.0s）：红绿各半
            green = v.get_frame(3.3)[40, 60].astype(int)   # 第二段独占区：纯绿
        assert np.abs(red - np.array([180, 40, 40])).max() < 45, f"首段帧应接近纯红，实际 {red}"
        assert np.abs(mid - np.array([110, 110, 40])).max() < 45, \
            f"重叠中点应为红绿混合（画面淡入生效），实际 {mid}"
        assert np.abs(green - np.array([40, 180, 40])).max() < 45, f"末段帧应接近纯绿，实际 {green}"

    def test_e2e_audio_crossfades_and_duration(self, proj_dir, tmp_path, monkeypatch):
        """带配音的交叉淡化：成片保留音轨，音轨铺满淡化时间轴（= 各段之和 - 重叠）"""
        self._env(monkeypatch, xfade=0.5)
        wav1 = _make_wav(tmp_path / "a.wav", 2.0)
        wav2 = _make_wav(tmp_path / "b.wav", 2.0)
        segments = [
            {"text": "第一段有配音。", "duration": 2.0, "image_path": "", "audio_path": wav1},
            {"text": "第二段有配音。", "duration": 2.0, "image_path": "", "audio_path": wav2},
        ]
        out = tmp_path / "xf_audio.mp4"
        video.create_video(segments, str(out), width=480, height=270)
        with VideoFileClip(str(out)) as v:
            assert v.audio is not None, "交叉淡化合成后应保留音轨"
            # 拼接式音轨会是 4.0s；淡化合成把第二段音轨提前到 1.5s 起，总长 3.5s
            assert v.audio.duration == pytest.approx(3.5, abs=0.35)
            assert v.duration == pytest.approx(3.5, abs=0.35)

    # ---------- SRT 时间轴一致性 ----------

    def test_srt_crossfade_shifts_next_segment_earlier(self, tmp_path):
        """SRT 与淡化时间轴一致：下一段提前 overlap 开始，相邻字幕区间不交叠"""
        segments = [
            {"text": "第一句。", "duration": 2.0},
            {"text": "第二句。", "duration": 2.0},
        ]
        srt = tmp_path / "xf.srt"
        subtitles.generate_srt(segments, str(srt), crossfade=0.5)
        blocks = srt.read_text(encoding="utf-8").strip().split("\n\n")
        assert blocks[0].splitlines()[1] == "00:00:00,000 --> 00:00:01,500"  # 尾块提前 overlap 结束
        assert blocks[1].splitlines()[1] == "00:00:01,500 --> 00:00:03,500"  # 下一段提前 overlap 开始

    def test_srt_crossfade_with_intro_offset(self, tmp_path):
        """片头卡偏移 + 交叉淡化组合：首段从 2.5s 开始，第二段提前 overlap"""
        segments = [
            {"text": "第一句。", "duration": 2.0},
            {"text": "第二句。", "duration": 2.0},
        ]
        srt = tmp_path / "xfc.srt"
        subtitles.generate_srt(segments, str(srt), offset=2.5, crossfade=0.5)
        blocks = srt.read_text(encoding="utf-8").strip().split("\n\n")
        assert blocks[0].splitlines()[1] == "00:00:02,500 --> 00:00:04,000"
        assert blocks[1].splitlines()[1] == "00:00:04,000 --> 00:00:06,000"

    def test_srt_crossfade_clamps_tail_of_multi_chunk_segment(self, tmp_path):
        """多块段落的最后一块字幕截断到下一段开始，中间块保持原时间"""
        segments = [
            {"text": "第一句。第二句！", "duration": 2.0},
            {"text": "第三句。", "duration": 2.0},
        ]
        srt = tmp_path / "xfmc.srt"
        subtitles.generate_srt(segments, str(srt), crossfade=0.5)
        blocks = srt.read_text(encoding="utf-8").strip().split("\n\n")
        assert blocks[0].splitlines()[1] == "00:00:00,000 --> 00:00:01,000"  # 中间块不截断
        assert blocks[1].splitlines()[1] == "00:00:01,000 --> 00:00:01,500"  # 尾块截到下一段开始
        assert blocks[2].splitlines()[1] == "00:00:01,500 --> 00:00:03,500"

    @staticmethod
    def _srt_blocks(path):
        """解析 SRT 为 [(start秒, end秒, 文本), ...]，供区间合法性断言"""
        out = []
        for blk in path.read_text(encoding="utf-8").strip().split("\n\n"):
            lines = blk.splitlines()

            def sec(t):
                h, m, rest = t.split(":")
                return int(h) * 3600 + int(m) + float(rest.replace(",", "."))

            s, e = lines[1].split(" --> ")
            out.append((sec(s), sec(e), lines[2]))
        return out

    def test_srt_blocks_are_always_valid(self, tmp_path):
        """回归（B 队实测缺陷）：xfade 较大时不得出现 end<start、零时长块或越过下一段起点

        旧实现只截尾块 end 不截头，crossfade=1.5 曾产出
        「00:00:01,000 --> 00:00:00,500」的非法块；crossfade=1.0 三块段
        曾产出零时长块。三组触发配置全部固化在此。
        """
        cases = [
            # (标签, segments, crossfade)
            ("a", [{"text": "一二三。四五六。", "duration": 2.0},
                   {"text": "七八九。", "duration": 2.0}], 1.5),
            ("b", [{"text": "一二三。四五六。七八九。", "duration": 3.0},
                   {"text": "十。", "duration": 2.0}], 1.0),
            ("c", [{"text": "一二三。四五六。", "duration": 1.0},
                   {"text": "七八九。", "duration": 1.0}], 0.8),
        ]
        for label, segs, xf in cases:
            srt = tmp_path / f"valid_{label}.srt"
            subtitles.generate_srt(segs, str(srt), crossfade=xf)
            blocks = self._srt_blocks(srt)
            assert blocks, f"{label}: 不应产出空 SRT"
            for s, e, _t in blocks:
                assert e > s, f"{label}: 出现 end<start/零时长块 [{s} --> {e}]"
            for i in range(len(blocks) - 1):
                assert blocks[i][1] <= blocks[i + 1][0], \
                    f"{label}: 第{i+1}块结束 {blocks[i][1]} 越过下一段起点 {blocks[i+1][0]}"
            # 总长守恒：offset(0) + 各段之和 - 重叠总和
            durs = [float(sg["duration"]) for sg in segs]
            overlap_total = sum(min(xf, durs[i], durs[i + 1]) for i in range(len(durs) - 1))
            assert blocks[-1][1] == pytest.approx(sum(durs) - overlap_total)

    def test_srt_overlap_fully_swallowed_segment_drops_chunks(self, tmp_path):
        """整段被重叠吞没时（段长<=xfade），该段字幕块全部丢弃，下一段起点衔接"""
        segments = [
            {"text": "第一句。第二句！", "duration": 1.0},
            {"text": "第三句。", "duration": 2.0},
        ]
        srt = tmp_path / "xfswallow.srt"
        subtitles.generate_srt(segments, str(srt), crossfade=1.0)
        blocks = self._srt_blocks(srt)
        # 第一段 overlap=1.0=段长，展示窗口为空 → 无字幕；第二段从 0.0 起
        assert len(blocks) == 1
        assert blocks[0][2] == "第三句。"
        assert blocks[0][0] == pytest.approx(0.0)
        assert blocks[0][1] == pytest.approx(2.0)

    def test_srt_no_crossfade_keeps_old_timeline(self, tmp_path):
        """不传 crossfade（默认 0）保持旧时间轴，兼容旧调用"""
        segments = [
            {"text": "第一句。", "duration": 2.0},
            {"text": "第二句。", "duration": 2.0},
        ]
        srt = tmp_path / "plain.srt"
        subtitles.generate_srt(segments, str(srt))
        blocks = srt.read_text(encoding="utf-8").strip().split("\n\n")
        assert blocks[0].splitlines()[1] == "00:00:00,000 --> 00:00:02,000"
        assert blocks[1].splitlines()[1] == "00:00:02,000 --> 00:00:04,000"

    def test_srt_end_matches_video_timeline(self, proj_dir, tmp_path, monkeypatch):
        """SRT 末条结束时间（含片头卡偏移与段间重叠）= 成片总时长 - 片尾卡"""
        self._env(monkeypatch, xfade=0.5)
        monkeypatch.setattr(config, "INTRO_OUTRO", True)
        segments = [
            {"text": "第一段话。", "duration": 2.0, "image_path": "", "audio_path": ""},
            {"text": "第二段话。", "duration": 1.5, "image_path": "", "audio_path": ""},
        ]
        out = tmp_path / "sync.mp4"
        video.create_video(segments, str(out), width=480, height=270, topic="同步")

        srt = tmp_path / "sync.srt"
        subtitles.generate_srt(segments, str(srt), offset=video.intro_offset(),
                               crossfade=video.crossfade_seconds())
        last_timing = srt.read_text(encoding="utf-8").strip().split("\n\n")[-1].splitlines()[1]
        end_text = last_timing.split("-->")[1].strip()
        h, m, rest = end_text.split(":")
        srt_end = int(h) * 3600 + int(m) + float(rest.replace(",", "."))
        with VideoFileClip(str(out)) as v:
            # 字幕覆盖到正片结束（片尾卡之后无字幕）
            assert srt_end == pytest.approx(v.duration - video.OUTRO_CARD_DURATION, abs=0.35)


class TestPreviewMode:
    """PREVIEW 预览模式：960×540（横）/ 540×960（竖）快速渲染（全部离线）"""

    def test_video_size_preview_landscape(self, monkeypatch):
        """预览开 + 16:9 → 960×540"""
        monkeypatch.setattr(config, "ASPECT_RATIO", "16:9")
        monkeypatch.setattr(config, "PREVIEW", True)
        assert config.video_size() == (960, 540)

    def test_video_size_preview_portrait(self, monkeypatch):
        """预览开 + 9:16 → 540×960（与全分辨率同画幅，约减半）"""
        monkeypatch.setattr(config, "ASPECT_RATIO", "9:16")
        monkeypatch.setattr(config, "PREVIEW", True)
        assert config.video_size() == (540, 960)

    def test_video_size_preview_off_keeps_full_res(self, monkeypatch):
        """预览关 → 全分辨率（1920×1080 / 1080×1920），保持旧行为"""
        monkeypatch.setattr(config, "PREVIEW", False)
        monkeypatch.setattr(config, "ASPECT_RATIO", "16:9")
        assert config.video_size() == (1920, 1080)
        monkeypatch.setattr(config, "ASPECT_RATIO", "9:16")
        assert config.video_size() == (1080, 1920)

    def test_video_size_explicit_preview_overrides_flag(self, monkeypatch):
        """显式传 preview 参数优先于全局 PREVIEW 开关"""
        monkeypatch.setattr(config, "ASPECT_RATIO", "16:9")
        monkeypatch.setattr(config, "PREVIEW", True)
        assert config.video_size("16:9", preview=False) == (1920, 1080)
        monkeypatch.setattr(config, "PREVIEW", False)
        assert config.video_size("16:9", preview=True) == (960, 540)

    def test_preview_env_parsing(self, monkeypatch):
        """PREVIEW 环境变量解析（reload config 验证）：1/true 开，0 关"""
        before_keys = set(os.environ)
        snapshot = config.PREVIEW
        try:
            monkeypatch.setenv("PREVIEW", "1")
            importlib.reload(config)
            assert config.PREVIEW is True
            assert config.video_size("16:9") == (960, 540)
            assert config.video_size("9:16") == (540, 960)

            monkeypatch.setenv("PREVIEW", "0")
            importlib.reload(config)
            assert config.PREVIEW is False
            assert config.video_size("16:9") == (1920, 1080)
        finally:
            # 还原环境变量与 config 模块属性，不污染其它用例
            for k in set(os.environ) - before_keys:
                os.environ.pop(k, None)
            config.PREVIEW = snapshot

    def test_e2e_preview_landscape_composes_at_960x540(self, proj_dir, tmp_path, monkeypatch):
        """预览模式端到端：create_video 不传宽高时按 960×540 合成"""
        monkeypatch.setattr(config, "VIDEO_FPS", 12)
        monkeypatch.setattr(config, "INTRO_OUTRO", False)
        monkeypatch.setattr(config, "PREVIEW", True)
        # 显式固定横屏，不依赖 .env 的 ASPECT_RATIO（用户改 .env 不应影响离线套件）
        monkeypatch.setattr(config, "ASPECT_RATIO", "16:9")
        img = _make_image(tmp_path / "pv_l.jpg", (60, 120, 60))
        segments = [
            {"text": "预览模式第一段。", "duration": 1.0, "image_path": img, "audio_path": ""},
        ]
        out = tmp_path / "preview_landscape.mp4"
        result = video.create_video(segments, str(out))  # 不传 width/height → 跟随 config.video_size()
        assert result == str(out)
        assert out.exists()
        # 预览帧为纯色底，1s@12fps 压缩后体积很小（实测 ~7KB），只要求非空有效文件
        assert out.stat().st_size > 1_000
        with VideoFileClip(str(out)) as v:
            assert v.size == [960, 540], f"预览横屏应合成 960×540，实际 {v.size}"

    def test_e2e_preview_portrait_composes_at_540x960(self, proj_dir, tmp_path, monkeypatch):
        """预览模式端到端（竖屏）：ASPECT_RATIO=9:16 时按 540×960 合成"""
        monkeypatch.setattr(config, "VIDEO_FPS", 12)
        monkeypatch.setattr(config, "INTRO_OUTRO", False)
        monkeypatch.setattr(config, "PREVIEW", True)
        monkeypatch.setattr(config, "ASPECT_RATIO", "9:16")
        img = _make_image(tmp_path / "pv_p.jpg", (120, 60, 60))
        segments = [
            {"text": "预览模式竖屏一段。", "duration": 1.0, "image_path": img, "audio_path": ""},
        ]
        out = tmp_path / "preview_portrait.mp4"
        video.create_video(segments, str(out))
        assert out.exists()
        with VideoFileClip(str(out)) as v:
            assert v.size == [540, 960], f"预览竖屏应合成 540×960，实际 {v.size}"

    def test_e2e_explicit_size_beats_preview(self, proj_dir, tmp_path, monkeypatch):
        """显式传 width/height 时 create_video 不做二次改写（回归保护）。

        注意（B 队 mustFix 更正）：Web 端宽高本身来自 config.video_size(aspect_ratio)
        （web/app.py:197），PREVIEW=1 时拿到的就是预览分辨率——即 Web 同样会出预览片，
        靠 web 端任务状态里的预览提示兜底（tests/test_web.py 预览用例验证）。
        本用例只证明 create_video 对已传入的 480×270 不再覆盖。
        """
        monkeypatch.setattr(config, "VIDEO_FPS", 12)
        monkeypatch.setattr(config, "INTRO_OUTRO", False)
        monkeypatch.setattr(config, "PREVIEW", True)
        segments = [
            {"text": "显式尺寸不受预览影响。", "duration": 0.8, "image_path": "", "audio_path": ""},
        ]
        out = tmp_path / "explicit.mp4"
        video.create_video(segments, str(out), width=480, height=270)
        with VideoFileClip(str(out)) as v:
            assert v.size == [480, 270]


class TestBatchAspect:
    """--batch-aspect 批量画幅：同一故事稿一次跑出横竖两版（全部离线）。

    设计取舍验证点：
    - TTS 音频与画幅无关 → generate_all_speech 只调 1 次，两版复用同批音频；
    - 配图只在首个画幅真实获取（API 侧原生构图），其余画幅同一底图智能裁切
      （images.derive_crops_for_aspect），不重复调配图 API；
    - 字幕时间轴只由音频时长决定 → 一份 subtitles.srt 全画幅共用；
    - 两画幅各自合成 mp4（尺寸不同）+ 各自封面，输出名带 _16x9/_9x16 后缀。
    """

    BATCH_STORY = (
        "这是批量渲染测试的第一段故事内容，字数足够长不会被分段逻辑过滤掉。\n\n"
        "这是第二段故事内容，同样足够长，用来验证横竖两版一次产出。"
    )

    @pytest.fixture
    def offline_batch(self, proj_dir, tmp_path, monkeypatch):
        """批量管线离线化：TTS/配图换成记录调用次数的本地替身，
        config.video_size 换成小尺寸（16:9→480×270 / 9:16→270×480）加速真实合成"""
        calls = {"speech": 0, "fetch": 0, "fetch_aspects": []}

        def fake_speech(segments, provider="", voice="", project_dir=""):
            calls["speech"] += 1
            for s in segments:
                s["audio_path"] = _make_wav(tmp_path / "voice.wav", 1.0)
                s["duration"] = 1.0
            return segments

        def fake_fetch(segments, image_source="", aspect_ratio="", project_dir=""):
            calls["fetch"] += 1
            calls["fetch_aspects"].append(aspect_ratio)
            for s in segments:
                s["image_path"] = _make_image(tmp_path / "base_img.jpg", (150, 60, 60))
            return segments

        def fake_video_size(aspect_ratio="", preview=None):
            return (270, 480) if aspect_ratio == "9:16" else (480, 270)

        def fake_keywords(segments, topic):
            for i, s in enumerate(segments):
                s["image_keyword"] = f"keyword_{i}"
            return segments

        monkeypatch.setattr(main, "generate_all_speech", fake_speech)
        monkeypatch.setattr(main, "fetch_all_images", fake_fetch)
        monkeypatch.setattr(main, "generate_image_keywords", fake_keywords)
        monkeypatch.setattr(config, "video_size", fake_video_size)
        monkeypatch.setattr(config, "VIDEO_FPS", 12)
        monkeypatch.setattr(config, "INTRO_OUTRO", False)
        monkeypatch.setattr(config, "KEN_BURNS", False)
        return calls

    # ---------- 解析 ----------

    def test_parse_batch_aspects(self, capsys):
        assert main.parse_batch_aspects("16:9,9:16") == ["16:9", "9:16"]
        assert main.parse_batch_aspects(" 9:16 , 9:16 ") == ["9:16"]   # 去重保序
        assert main.parse_batch_aspects("9:16,16:9") == ["9:16", "16:9"]  # 顺序即首个画幅
        assert main.parse_batch_aspects("4:3,abc") == []               # 非法全弃
        assert main.parse_batch_aspects("") == []
        assert main.parse_batch_aspects("16:9,,9:16") == ["16:9", "9:16"]  # 空项跳过
        assert "忽略不支持的画幅" in capsys.readouterr().out            # 非法项有警告

    # ---------- 智能裁切（images.crop_to_fill / derive_crops_for_aspect） ----------

    def test_crop_to_fill_center_keeps_middle(self, tmp_path):
        """640×360（左右 1/3 绿、中间 1/3 红）裁到 270×480：等比铺满后居中裁切，
        整个裁切窗口都落在中央红区"""
        img = Image.new("RGB", (640, 360), (0, 160, 0))
        draw = ImageDraw.Draw(img)
        draw.rectangle([213, 0, 427, 360], fill=(200, 30, 30))
        src = tmp_path / "tri.jpg"
        img.save(src, quality=95)

        dst = tmp_path / "crop.jpg"
        out = images.crop_to_fill(str(src), str(dst), 270, 480)
        assert out == str(dst) and dst.exists()
        with Image.open(dst) as im:
            assert im.size == (270, 480)
            px = im.convert("RGB")
            for x, y in ((135, 240), (10, 10), (259, 469)):  # 中心 + 两角
                r, g, _b = px.getpixel((x, y))
                assert r > 150 and g < 100, f"({x},{y}) 应为中央红色，实际 {(r, g, _b)}"

    def test_crop_to_fill_failure_returns_none(self, tmp_path):
        """源文件损坏时返回 None，不抛异常（调用方回退底图）"""
        bad = tmp_path / "bad.jpg"
        bad.write_bytes(b"not an image")
        assert images.crop_to_fill(str(bad), str(tmp_path / "o.jpg"), 100, 100) is None

    def test_derive_crops_for_aspect(self, proj_dir, tmp_path, monkeypatch):
        """批量第二画幅：底图派生 image_XXX_9x16.jpg 并改指 image_path；
        无底图段保持原样；派生图尺寸 = 该画幅 video_size"""
        monkeypatch.setattr(config, "video_size",
                            lambda aspect="", preview=None: (270, 480))
        base = _make_image(tmp_path / "img.jpg", (90, 90, 90))
        segs = [{"image_path": base}, {"image_path": ""}]  # 第二段无底图
        out = images.derive_crops_for_aspect([dict(s) for s in segs], "9:16",
                                             project_dir=str(tmp_path))
        assert out[0]["image_path"] == str(tmp_path / "image_000_9x16.jpg")
        assert (tmp_path / "image_000_9x16.jpg").exists()
        with Image.open(tmp_path / "image_000_9x16.jpg") as im:
            assert im.size == (270, 480)
        assert out[1]["image_path"] == ""  # 无底图：黑底帧兜底，不派生

    # ---------- 端到端：两尺寸文件都生成 ----------

    def test_batch_renders_both_sizes(self, offline_batch, tmp_path):
        """核心验收：--batch-aspect "16:9,9:16" 一趟跑出横竖两个成片，
        尺寸互不相同；TTS 只调 1 次；配图只真实获取 1 次（首个画幅）"""
        result = main._produce_video(self.BATCH_STORY, "批量主题",
                                     batch_aspects=["16:9", "9:16"])
        assert isinstance(result, list) and len(result) == 2
        assert [os.path.basename(p) for p in result] == \
            ["批量主题_16x9.mp4", "批量主题_9x16.mp4"]
        assert all(os.path.exists(p) and os.path.getsize(p) > 10_000 for p in result)

        # TTS 复用：音频与画幅无关，两版只生成一次语音
        assert offline_batch["speech"] == 1
        # 配图只在首个画幅真实获取（API 侧原生构图），第二画幅走智能裁切
        assert offline_batch["fetch"] == 1
        assert offline_batch["fetch_aspects"] == ["16:9"]

        # 两尺寸文件都生成：真实读取成片验证画幅
        with VideoFileClip(result[0]) as v:
            assert v.size == [480, 270], f"横版应 480×270，实际 {v.size}"
        with VideoFileClip(result[1]) as v:
            assert v.size == [270, 480], f"竖版应 270×480，实际 {v.size}"

        # 第二画幅配图由底图智能裁切派生（离线、零 API 花费）
        proj = next(d for d in tmp_path.iterdir()
                    if d.is_dir() and "批量主题" in d.name)
        assert (proj / "image_000_9x16.jpg").exists()
        assert (proj / "image_001_9x16.jpg").exists()
        # 字幕一份全画幅共用；封面按画幅各自一份
        assert (proj / "subtitles.srt").exists()
        assert (proj / "cover_16x9.jpg").exists()
        assert (proj / "cover_9x16.jpg").exists()

    def test_batch_preview_sizes_follow_preview(self, offline_batch, tmp_path, monkeypatch):
        """PREVIEW=1 与批量组合：各画幅取预览分辨率（同画幅约减半）"""
        monkeypatch.setattr(config, "video_size",
                            lambda aspect="", preview=None:
                            (540, 960) if aspect == "9:16" else (960, 540))
        result = main._produce_video(self.BATCH_STORY, "预览批量",
                                     batch_aspects=["16:9", "9:16"])
        with VideoFileClip(result[0]) as v:
            assert v.size == [960, 540]
        with VideoFileClip(result[1]) as v:
            assert v.size == [540, 960]

    def test_single_aspect_backward_compat(self, offline_batch, tmp_path):
        """回归保护：不传 batch_aspects 走旧单画幅路径——输出名无画幅后缀、
        cover.jpg 不带后缀、返回字符串而非列表"""
        result = main._produce_video(self.BATCH_STORY, "单画幅主题")
        assert isinstance(result, str)
        assert os.path.basename(result) == "单画幅主题.mp4"
        assert os.path.exists(result)
        proj = next(d for d in tmp_path.iterdir()
                    if d.is_dir() and "单画幅主题" in d.name)
        assert (proj / "cover.jpg").exists()
        assert not (proj / "cover_16x9.jpg").exists()

    def test_batch_single_aspect_list_behaves_like_single(self, offline_batch, tmp_path):
        """batch_aspects 传单元素列表：等价旧行为（无后缀、返回 str）"""
        result = main._produce_video(self.BATCH_STORY, "单元素批量",
                                     batch_aspects=["9:16"])
        assert isinstance(result, str)
        assert os.path.basename(result) == "单元素批量.mp4"

    def test_cli_accepts_batch_aspect_flag(self, offline_batch, tmp_path, monkeypatch, capsys):
        """CLI 端到端：--batch-aspect "16:9,9:16" 走 --script-file 模式出两版"""
        import sys
        script = tmp_path / "s.txt"
        script.write_text(self.BATCH_STORY, encoding="utf-8")
        monkeypatch.setattr(sys, "argv",
                            ["main.py", "--script-file", str(script),
                             "--batch-aspect", "16:9,9:16", "--output", "out.mp4"])
        monkeypatch.setattr(config, "LLM_API_KEY", "")
        monkeypatch.setattr(config, "TTS_PROVIDER", "stepfun")
        monkeypatch.setattr(config, "STEPFUN_API_KEY", "fake-key")
        monkeypatch.setattr(config, "IMAGE_SOURCE", "minimax")
        monkeypatch.setattr(config, "MINIMAX_API_KEY", "")

        main.main()
        out = capsys.readouterr().out
        assert "批量画幅模式: 16:9 + 9:16" in out
        assert "批量渲染完成" in out
        proj = next(d for d in tmp_path.iterdir()
                    if d.is_dir() and (d / "out_16x9.mp4").exists())
        produced = sorted(p.name for p in proj.glob("out_*.mp4"))
        assert produced == ["out_16x9.mp4", "out_9x16.mp4"]

    def test_cli_rejects_invalid_batch_aspect(self, monkeypatch, capsys):
        """--batch-aspect 全非法时 argparse error 退出（不静默回退单画幅）"""
        import sys
        monkeypatch.setattr(sys, "argv",
                            ["main.py", "主题", "--batch-aspect", "4:3"])
        with pytest.raises(SystemExit):
            main.main()


class TestCorruptImageFallback:
    """单段配图损坏（断点续跑留下的半截文件）不得让整个合成崩溃。

    B 队复核确认的缺陷（overnight_reports/A12_resume.md「遗留 1」）：
    复用判据只看「存在且非空」时，损坏非空 jpg 会被当成品复用，
    Image.open 在合成阶段抛异常穿透 create_video，整次渲染白费。
    B6 修复 = 复用判定加完整解码校验（images._is_valid_image_file，
    坏图删除重取）+ 合成层黑底帧兜底（video._image_readable / _build_segment_clip）。
    """

    CORRUPT_BYTES = b"\xff\xd8\xff\xe0" + b"truncated-not-a-real-jpeg" * 4

    def test_image_readable_rejects_corrupt_file(self, tmp_path):
        good = _make_image(tmp_path / "good.jpg", (10, 20, 30))
        bad = tmp_path / "bad.jpg"
        bad.write_bytes(self.CORRUPT_BYTES)
        assert video._image_readable(good) is True
        assert video._image_readable(str(bad)) is False
        assert video._image_readable(str(tmp_path / "missing.jpg")) is False

    def test_corrupt_reused_image_is_deleted_and_refetched(self, tmp_path, monkeypatch):
        """复用判定：损坏的非空坏图不当成品——删除后按缺失段重新获取"""
        from PIL import Image as PILImage

        monkeypatch.setattr(config, "PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(config, "TEMP_DIR", str(tmp_path))
        bad = tmp_path / "image_000.jpg"
        bad.write_bytes(self.CORRUPT_BYTES)

        def fake_fetch(keyword, index, image_source="", aspect_ratio="", project_dir=""):
            path = tmp_path / f"image_{index:03d}.jpg"
            PILImage.new("RGB", (64, 64), (5, 5, 5)).save(path, quality=90)
            return str(path)

        monkeypatch.setattr(images, "fetch_image_for_segment", fake_fetch)

        result = images.fetch_all_images(
            [{"text": "s", "duration": 5.0, "image_keyword": "k"}],
            project_dir=str(tmp_path),
        )
        assert result[0]["image_path"] == str(bad)
        assert result[0].get("image_reused") is None  # 不是复用，是重新获取
        assert video._image_readable(str(bad))  # 落盘的是可解码的新图

    def test_segment_clip_corrupt_image_falls_back_to_black_frame(self, tmp_path, monkeypatch):
        """合成层兜底（Ken Burns 默认路径）：坏图段回退黑底帧，正常返回片段"""
        bad = tmp_path / "image_000.jpg"
        bad.write_bytes(self.CORRUPT_BYTES)
        seg = {"text": "一句话", "duration": 0.5,
               "image_path": str(bad), "audio_path": ""}
        clip = video._build_segment_clip(seg, 0, str(tmp_path), 160, 90)
        try:
            assert clip is not None
            assert clip.duration == pytest.approx(0.5, abs=0.2)
        finally:
            clip.close()

    def test_segment_clip_corrupt_image_baked_path_falls_back(self, tmp_path, monkeypatch):
        """合成层兜底（KEN_BURNS=0 烘焙路径）：坏图段同样回退黑底帧"""
        monkeypatch.setattr(config, "KEN_BURNS", False)
        bad = tmp_path / "image_001.jpg"
        bad.write_bytes(self.CORRUPT_BYTES)
        seg = {"text": "一句话", "duration": 0.5,
               "image_path": str(bad), "audio_path": ""}
        clip = video._build_segment_clip(seg, 1, str(tmp_path), 160, 90)
        try:
            assert clip is not None
            assert clip.duration == pytest.approx(0.5, abs=0.2)
        finally:
            clip.close()

    def test_create_video_survives_corrupt_segment_image(self, proj_dir, tmp_path, monkeypatch):
        """端到端：一段坏图 + 一段好图，create_video 必须完整出片而不是整体崩溃"""
        monkeypatch.setattr(config, "INTRO_OUTRO", False)  # 缩短渲染：不出片头片尾卡
        monkeypatch.setattr(config, "CROSSFADE_SECONDS", 0.0)
        monkeypatch.setattr(config, "BGM_VOLUME", 0.0)

        bad = tmp_path / "image_000.jpg"
        bad.write_bytes(self.CORRUPT_BYTES)
        good = _make_image(tmp_path / "image_001.jpg", (120, 40, 200))
        segments = [
            {"text": "坏图段", "duration": 0.5, "image_path": str(bad), "audio_path": ""},
            {"text": "好图段", "duration": 0.5, "image_path": good, "audio_path": ""},
        ]
        out = tmp_path / "corrupt_seg.mp4"
        result = video.create_video(segments, str(out), width=480, height=270,
                                    project_dir=str(tmp_path), topic="坏图回归")
        assert result == str(out)
        assert out.exists() and out.stat().st_size > 1000
        clip = VideoFileClip(str(out))
        try:
            assert clip.duration == pytest.approx(1.0, abs=0.3)
            assert [clip.w, clip.h] == [480, 270]  # B7 修正：原稿引用未定义变量 w/h（NameError）
        finally:
            clip.close()


class TestReuseFramesRerender:
    """换图局部重渲（D4）：reuse_frames 复用已渲染字幕帧/叠加层，
    rerender_segments 强制重渲选中段；布局漂移或坏缓存帧回退重渲。
    真实离线合成小 MP4（480×270、12fps）验证，不依赖任何 API。"""

    def _segments(self, tmp_path):
        img1 = _make_image(tmp_path / "a.jpg", (120, 40, 40))
        img2 = _make_image(tmp_path / "b.jpg", (40, 120, 40))
        return [
            {"text": "第一段甲。第一段乙！", "duration": 1.2, "image_path": img1, "audio_path": ""},
            {"text": "第二段独句。", "duration": 0.8, "image_path": img2, "audio_path": ""},
        ]

    def test_plain_path_reuses_frames_except_selected_segment(
            self, proj_dir, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "VIDEO_FPS", 12)
        monkeypatch.setattr(config, "KEN_BURNS", False)
        monkeypatch.setattr(config, "INTRO_OUTRO", False)
        segments = self._segments(tmp_path)
        out1, out2 = tmp_path / "v1.mp4", tmp_path / "v2.mp4"

        video.create_video(segments, str(out1), width=480, height=270)
        # 首轮落盘「字幕切块布局快照」，供复用前校验
        assert (tmp_path / "_chunks_000.json").exists()
        old_seg1_frame = (tmp_path / "frame_001_00.jpg").read_bytes()

        renders = []
        orig = video.render_subtitle_frame

        def counting_render(base, text, output_path, width, height):
            renders.append(os.path.basename(output_path))
            return orig(base, text, output_path, width, height)

        monkeypatch.setattr(video, "render_subtitle_frame", counting_render)

        # 换第 0 段配图后局部重渲：只有第 0 段的两块帧被重画
        segments[0]["image_path"] = _make_image(tmp_path / "a2.jpg", (200, 10, 10))
        video.create_video(segments, str(out2), width=480, height=270,
                           reuse_frames=True, rerender_segments={0})

        assert renders == ["frame_000_00.jpg", "frame_000_01.jpg"]
        assert (tmp_path / "frame_001_00.jpg").read_bytes() == old_seg1_frame
        # 复用缓存帧的成片压缩率更高（部分画面与首轮相同），只验产物真实非空
        assert out2.exists() and out2.stat().st_size > 1_000

    def test_plain_path_reuses_everything_when_nothing_selected(
            self, proj_dir, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "VIDEO_FPS", 12)
        monkeypatch.setattr(config, "KEN_BURNS", False)
        monkeypatch.setattr(config, "INTRO_OUTRO", False)
        segments = self._segments(tmp_path)
        video.create_video(segments, str(tmp_path / "v1.mp4"), width=480, height=270)

        renders = []

        def must_not_render(*a, **k):
            renders.append(a)
            raise AssertionError("未选中任何段时不应重渲任何字幕帧")

        monkeypatch.setattr(video, "render_subtitle_frame", must_not_render)
        video.create_video(segments, str(tmp_path / "v2.mp4"), width=480, height=270,
                           reuse_frames=True)
        assert renders == []

    def test_corrupt_cached_frame_falls_back_to_rerender(
            self, proj_dir, tmp_path, monkeypatch):
        """坏缓存帧（半截文件）不能当缓存用：该段整段重渲，其余段照常复用"""
        monkeypatch.setattr(config, "VIDEO_FPS", 12)
        monkeypatch.setattr(config, "KEN_BURNS", False)
        monkeypatch.setattr(config, "INTRO_OUTRO", False)
        segments = self._segments(tmp_path)
        video.create_video(segments, str(tmp_path / "v1.mp4"), width=480, height=270)
        (tmp_path / "frame_001_00.jpg").write_bytes(b"corrupt-half-file")

        renders = []
        orig = video.render_subtitle_frame

        def counting_render(base, text, output_path, width, height):
            renders.append(os.path.basename(output_path))
            return orig(base, text, output_path, width, height)

        monkeypatch.setattr(video, "render_subtitle_frame", counting_render)
        video.create_video(segments, str(tmp_path / "v2.mp4"), width=480, height=270,
                           reuse_frames=True)

        assert renders == ["frame_001_00.jpg"]  # 只有坏帧所在段重渲

    def test_layout_drift_falls_back_to_rerender(
            self, proj_dir, tmp_path, monkeypatch):
        """切块布局变化（换文案/词级时间片）时不得复用旧帧：宁可重渲不可错位"""
        monkeypatch.setattr(config, "VIDEO_FPS", 12)
        monkeypatch.setattr(config, "KEN_BURNS", False)
        monkeypatch.setattr(config, "INTRO_OUTRO", False)
        segments = self._segments(tmp_path)
        video.create_video(segments, str(tmp_path / "v1.mp4"), width=480, height=270)

        renders = []
        orig = video.render_subtitle_frame

        def counting_render(base, text, output_path, width, height):
            renders.append(os.path.basename(output_path))
            return orig(base, text, output_path, width, height)

        monkeypatch.setattr(video, "render_subtitle_frame", counting_render)
        segments[1]["text"] = "完全不同的新文案，长度也足够切块。"
        video.create_video(segments, str(tmp_path / "v2.mp4"), width=480, height=270,
                           reuse_frames=True)

        assert renders == ["frame_001_00.jpg"]  # 第 0 段复用，第 1 段布局变了重渲

    def test_kenburns_overlay_reuse_except_selected_segment(
            self, proj_dir, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "VIDEO_FPS", 12)
        monkeypatch.setattr(config, "KEN_BURNS", True)  # 默认开，显式防环境干扰
        monkeypatch.setattr(config, "INTRO_OUTRO", False)
        segments = self._segments(tmp_path)
        video.create_video(segments, str(tmp_path / "v1.mp4"), width=480, height=270)
        assert list(tmp_path.glob("overlay_000*.png"))

        renders = []
        orig = video._subtitle_overlay_rgba

        def counting_overlay(text, width, height):
            renders.append(text)
            return orig(text, width, height)

        monkeypatch.setattr(video, "_subtitle_overlay_rgba", counting_overlay)

        segments[0]["image_path"] = _make_image(tmp_path / "a2.jpg", (200, 10, 10))
        video.create_video(segments, str(tmp_path / "v2.mp4"), width=480, height=270,
                           reuse_frames=True, rerender_segments={0})

        # 只有第 0 段的两块叠加层重渲；第 1 段叠加层复用（背景每段都要重建）
        assert renders == ["第一段甲。", "第一段乙！"]


class TestCrossfadeSubtitleWindow:
    """交叉淡化重叠区字幕窗口化：前段字幕提前收起，重叠区不再两层同框（全部离线）"""

    def _units(self):
        return [("第一句。", 1.0, None, ""), ("第二句！", 1.0, None, "")]

    def test_trim_units_shortens_last_and_keeps_rest(self):
        trimmed = video._trim_units_to_window(self._units(), 0.6)
        assert [u[0] for u in trimmed] == ["第一句。", "第二句！"]
        assert [u[1] for u in trimmed] == [pytest.approx(1.0), pytest.approx(0.4)]

    def test_trim_units_drops_emptied_units(self):
        trimmed = video._trim_units_to_window(self._units(), 1.2)
        assert [u[0] for u in trimmed] == ["第一句。"]
        assert trimmed[0][1] == pytest.approx(0.8)

    def test_trim_units_full_clear_returns_empty(self):
        assert video._trim_units_to_window(self._units(), 5.0) == []

    def test_trim_zero_returns_same_list(self):
        units = self._units()
        assert video._trim_units_to_window(units, 0.0) is units

    def test_kenburns_clip_subtitle_ends_before_tail(self, tmp_path, monkeypatch):
        """Ken Burns 路径：tail_clearance=0.5 时最后一个叠加层在段尾 0.5s 前收起，段时长不变"""
        monkeypatch.setattr(config, "KEN_BURNS", True)
        monkeypatch.setattr(config, "KEN_BURNS_STRENGTH", 0.0)  # 静止画面，排除动效干扰
        img = _make_image(tmp_path / "xfw.jpg", (50, 50, 50))
        clip = video._build_segment_clip(
            {"text": "第一句。第二句！", "duration": 2.0, "image_path": img, "audio_path": ""},
            0, str(tmp_path), 480, 270, tail_clearance=0.5)
        try:
            assert clip.duration == pytest.approx(2.0)
            overlays = clip.clips[1:]
            assert overlays, "应有字幕叠加层"
            last_end = max(o.start + o.duration for o in overlays)
            assert last_end == pytest.approx(1.5), "字幕应在段尾 0.5s 前全部收起"
        finally:
            clip.close()

    def test_baked_clip_tail_frame_holds_duration(self, tmp_path, monkeypatch):
        """烘焙路径：字幕收起后补无字幕垫帧，段总时长保持不变（配音时间轴不漂移）"""
        monkeypatch.setattr(config, "KEN_BURNS", False)
        img = _make_image(tmp_path / "xfw2.jpg", (60, 40, 40))
        seg = {"text": "第一句。第二句！", "duration": 2.0, "image_path": img, "audio_path": ""}
        clip = video._build_segment_clip(seg, 0, str(tmp_path), 480, 270, tail_clearance=0.5)
        try:
            assert clip.duration == pytest.approx(2.0)
            assert (tmp_path / "frame_000_tail.jpg").exists()
        finally:
            clip.close()

    def test_baked_tail_frame_is_clean_base_without_subtitle(self, tmp_path, monkeypatch):
        """垫帧内容 = 无字幕底图（重叠区里不应残留前段字幕）"""
        monkeypatch.setattr(config, "KEN_BURNS", False)
        img = _make_image(tmp_path / "xfw3.jpg", (60, 40, 40))
        seg = {"text": "第一句。第二句！", "duration": 2.0, "image_path": img, "audio_path": ""}
        video._build_segment_clip(seg, 0, str(tmp_path), 480, 270, tail_clearance=0.5)
        ref = Image.open(img).convert("RGB").resize((480, 270), Image.Resampling.LANCZOS)
        tail = Image.open(tmp_path / "frame_000_tail.jpg").convert("RGB")
        diff = np.abs(np.asarray(tail, dtype=int) - np.asarray(ref, dtype=int))
        assert diff.max() < 25, f"垫帧应是无字幕底图，与纯 resize 底图差异过大: {diff.max()}"

    def test_e2e_crossfade_windowing_keeps_timeline(self, proj_dir, tmp_path, monkeypatch):
        """端到端：交叉淡化开启时成片时长 = 各段之和 - 重叠（窗口化不影响时间轴公式）"""
        monkeypatch.setattr(config, "VIDEO_FPS", 12)
        monkeypatch.setattr(config, "KEN_BURNS", False)
        monkeypatch.setattr(config, "INTRO_OUTRO", False)
        monkeypatch.setattr(config, "CROSSFADE_SECONDS", 0.5)
        img1 = _make_image(tmp_path / "xa.jpg", (180, 40, 40))
        img2 = _make_image(tmp_path / "xb.jpg", (40, 180, 40))
        segments = [
            {"text": "第一段只有一句话。", "duration": 2.0, "image_path": img1, "audio_path": ""},
            {"text": "第二段只有一句话。", "duration": 2.0, "image_path": img2, "audio_path": ""},
        ]
        out = tmp_path / "xf_window.mp4"
        video.create_video(segments, str(out), width=480, height=270)
        with VideoFileClip(str(out)) as v:
            assert v.duration == pytest.approx(4.0 - 0.5, abs=0.35)
        # 第一段的重叠区垫帧存在且无字幕
        assert (tmp_path / "frame_000_tail.jpg").exists()


class TestChunksMetaStyleSize:
    """缓存快照带字幕样式与分辨率：换样式/换分辨率不得复用旧帧"""

    def test_snapshot_records_style_and_size(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "SUBTITLE_STYLE", "card")
        chunks = [("一句。", 1.0)]
        video._save_chunks_meta(0, "", str(tmp_path), chunks, size=(480, 270))
        assert video._cached_chunks_match(0, "", str(tmp_path), chunks, size=(480, 270))
        # 分辨率变了 → 不复用
        assert not video._cached_chunks_match(0, "", str(tmp_path), chunks, size=(960, 540))
        # 样式变了 → 不复用（plain/card 共用帧名，不比对会复用错样式帧）
        monkeypatch.setattr(config, "SUBTITLE_STYLE", "plain")
        assert not video._cached_chunks_match(0, "", str(tmp_path), chunks, size=(480, 270))

    def test_older_snapshot_without_style_invalidated(self, tmp_path, monkeypatch):
        """旧版快照（无 style/size 字段）一律判不匹配 → 整段重渲一次后恢复复用"""
        chunks = [("一句。", 1.0)]
        with open(tmp_path / "_chunks_000.json", "w", encoding="utf-8") as f:
            json.dump({"chunks": video._chunks_layout(chunks)}, f, ensure_ascii=False)
        assert not video._cached_chunks_match(0, "", str(tmp_path), chunks, size=(480, 270))
        video._save_chunks_meta(0, "", str(tmp_path), chunks, size=(480, 270))
        assert video._cached_chunks_match(0, "", str(tmp_path), chunks, size=(480, 270))


class TestVideoSegmentMedia:
    """本地视频素材段：截取/循环/静音/解码失败回退（全部离线，真实 ffmpeg 编解码）"""

    @staticmethod
    def _make_video(path, duration=1.0, size=(64, 64), color=(10, 120, 200)):
        ColorClip(size, color=color).with_duration(duration).write_videofile(
            str(path), fps=12, logger=None)
        return str(path)

    def test_video_background_trims_long_source(self, tmp_path):
        src = self._make_video(tmp_path / "v_long.mp4", duration=3.0)
        bg = video._video_background(src, 480, 270, 2.0)
        assert bg is not None
        assert bg.duration == pytest.approx(2.0)
        assert tuple(bg.size) == (480, 270)
        bg.close()

    def test_video_background_loops_short_source(self, tmp_path):
        src = self._make_video(tmp_path / "v_short.mp4", duration=0.5)
        bg = video._video_background(src, 480, 270, 2.0)
        assert bg is not None
        assert bg.duration == pytest.approx(2.0), "短于段时长的视频应循环补齐"
        assert tuple(bg.size) == (480, 270)
        bg.close()

    def test_video_background_muted(self, tmp_path):
        """视频原声一律静音（旁白为主）： without_audio 后无音轨"""
        src = self._make_video(tmp_path / "v_mute.mp4", duration=1.0)
        bg = video._video_background(src, 480, 270, 1.0)
        assert bg is not None
        assert bg.audio is None
        bg.close()

    def test_video_background_corrupt_returns_none(self, tmp_path):
        bad = tmp_path / "bad.mp4"
        bad.write_bytes(b"not a video file")
        assert video._video_background(str(bad), 480, 270, 1.0) is None

    def test_segment_clip_uses_video_and_holds_duration(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "KEN_BURNS", True)
        img = _make_image(tmp_path / "vi.jpg", (90, 90, 90))
        src = self._make_video(tmp_path / "vs.mp4", duration=0.6)
        seg = {"text": "一句话。", "duration": 1.5, "image_path": img,
               "audio_path": "", "video_path": src}
        clip = video._build_segment_clip(seg, 0, str(tmp_path), 480, 270)
        try:
            assert clip.duration == pytest.approx(1.5)
            assert clip.clips, "视频背景 + 字幕叠加层应为复合片段"
        finally:
            clip.close()

    def test_segment_clip_falls_back_to_image_when_video_missing(self, tmp_path, monkeypatch):
        """视频文件缺失：回退图片路径（素材分配时视频段照常配了图），不抛异常"""
        monkeypatch.setattr(config, "KEN_BURNS", True)
        img = _make_image(tmp_path / "vi2.jpg", (90, 90, 90))
        seg = {"text": "一句话。", "duration": 1.0, "image_path": img,
               "audio_path": "", "video_path": str(tmp_path / "nope.mp4")}
        clip = video._build_segment_clip(seg, 0, str(tmp_path), 480, 270)
        try:
            assert clip.duration == pytest.approx(1.0)
        finally:
            clip.close()

    def test_e2e_video_segment_composes(self, proj_dir, tmp_path, monkeypatch):
        """端到端：含视频段的成片真实落地（12fps 小尺寸）"""
        monkeypatch.setattr(config, "VIDEO_FPS", 12)
        monkeypatch.setattr(config, "INTRO_OUTRO", False)
        img = _make_image(tmp_path / "e2e_img.jpg", (90, 90, 90))
        src = self._make_video(tmp_path / "e2e_v.mp4", duration=0.8)
        segments = [
            {"text": "视频段。", "duration": 1.0, "image_path": img,
             "audio_path": "", "video_path": src},
            {"text": "图片段。", "duration": 1.0, "image_path": img, "audio_path": ""},
        ]
        out = tmp_path / "vid_seg.mp4"
        result = video.create_video(segments, str(out), width=480, height=270)
        assert result == str(out)
        assert out.exists() and out.stat().st_size > 5_000
        with VideoFileClip(str(out)) as v:
            assert v.duration == pytest.approx(2.0, abs=0.35)
