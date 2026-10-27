"""cover.py：封面生成离线测试（不依赖任何 API）"""
import os

import pytest
from PIL import Image, ImageFont

import config
import cover


def _make_image(path, color):
    Image.new("RGB", (640, 360), color).save(path, quality=90)
    return str(path)


class TestResolveTitle:
    def test_cover_title_env_overrides_topic(self, monkeypatch):
        monkeypatch.setenv("COVER_TITLE", "自定义标题")
        assert cover.resolve_title("原始主题") == "自定义标题"

    def test_falls_back_to_topic(self, monkeypatch):
        monkeypatch.delenv("COVER_TITLE", raising=False)
        assert cover.resolve_title("原始主题") == "原始主题"


class TestFormatDuration:
    def test_seconds(self):
        assert cover.format_duration(42) == "42 秒"

    def test_minutes(self):
        assert cover.format_duration(65) == "1 分 05 秒"

    def test_zero_is_safe(self):
        assert cover.format_duration(0) == "0 秒"


class TestGenerateCover:
    def test_generates_jpg(self, tmp_path):
        out = tmp_path / "cover.jpg"
        result = cover.generate_cover([], str(out), topic="横版封面主题",
                                      total_duration=125.0, width=1920, height=1080)
        assert result == str(out)
        assert out.exists()
        assert out.stat().st_size > 1000

    def test_vertical_adapts(self, tmp_path):
        out = tmp_path / "cover.jpg"
        cover.generate_cover([], str(out), topic="竖版封面主题",
                             total_duration=42.0, width=1080, height=1920)
        with Image.open(out) as img:
            assert img.size == (1080, 1920)

    @pytest.mark.parametrize("ratio", ["16:9", "9:16"])
    def test_size_matches_video_size(self, tmp_path, monkeypatch, ratio):
        """不显式传宽高时，封面尺寸必须与 config.video_size() 一致（横竖两种比例）"""
        monkeypatch.setattr(config, "ASPECT_RATIO", ratio)
        out = tmp_path / f"cover_{ratio.replace(':', 'x')}.jpg"
        cover.generate_cover([], str(out), topic="主题")
        with Image.open(out) as img:
            assert img.size == config.video_size()

    def test_uses_segment_image(self, tmp_path):
        img_path = _make_image(tmp_path / "seg0.jpg", (200, 40, 40))
        segments = [{"text": "第一段", "image_path": img_path}]
        out = tmp_path / "cover.jpg"
        cover.generate_cover(segments, str(out), topic="主题", width=640, height=360)
        assert out.exists()

    def test_missing_image_file_falls_back_to_gradient(self, tmp_path):
        segments = [{"text": "第一段", "image_path": str(tmp_path / "nope.jpg")}]
        out = tmp_path / "cover.jpg"
        cover.generate_cover(segments, str(out), topic="主题",
                             total_duration=0.0, width=640, height=360)
        assert out.exists()

    def test_cover_title_env_affects_render(self, tmp_path, monkeypatch):
        """COVER_TITLE 改的不只是文案函数，渲染出来的图也要跟着变"""
        monkeypatch.delenv("COVER_TITLE", raising=False)
        a = tmp_path / "a.jpg"
        cover.generate_cover([], str(a), topic="默认主题标题", width=640, height=360)
        monkeypatch.setenv("COVER_TITLE", "另一个完全不同的标题")
        b = tmp_path / "b.jpg"
        cover.generate_cover([], str(b), topic="默认主题标题", width=640, height=360)
        assert a.read_bytes() != b.read_bytes()


class TestRealFont:
    def test_uses_real_font_file_not_default_bitmap(self):
        """标题字体必须是磁盘上的真实矢量字体，而不是 Pillow 内置兜底
        （Pillow 11 的 load_default() 也返回 FreeTypeFont，但 path 是内存
        BytesIO；真实字体才有一个存在磁盘上的字符串路径）"""
        font = cover._get_system_font(120)
        assert isinstance(font, ImageFont.FreeTypeFont)
        path = getattr(font, "path", None)
        assert isinstance(path, str), "兜底字体来自内存(BytesIO)，真实字体应有磁盘路径"
        assert os.path.exists(path)


class TestBaseImage:
    """用户上传封面底图（web /api/upload → base_image 参数）优先于默认背景"""

    def test_base_image_used_as_background(self, tmp_path):
        """纯红底图做底：成片中心像素应明显偏红（而非主题渐变/配图混合色）"""
        base = _make_image(tmp_path / "base.jpg", (220, 30, 30))
        out = tmp_path / "cover.jpg"
        cover.generate_cover([], str(out), topic="红色主题",
                             width=640, height=360, base_image=base)
        assert out.exists()
        with Image.open(out) as img:
            px = img.convert("RGB").getpixel((320, 180))
        # 遮罩在画面中部仍有部分压暗，但红通道必须显著高于绿蓝通道
        assert px[0] > 100 and px[0] > 2 * px[1] and px[0] > 2 * px[2], \
            f"封面底图应全幅铺满作底，中心像素 {px} 应偏红"

    def test_base_image_overrides_segment_image(self, tmp_path):
        """底图与配图同时存在时：底图优先，配图不再混入"""
        base = _make_image(tmp_path / "base.jpg", (20, 30, 220))    # 蓝
        seg_img = _make_image(tmp_path / "seg0.jpg", (220, 30, 30))  # 红
        segments = [{"text": "第一段", "image_path": seg_img}]
        out = tmp_path / "cover.jpg"
        cover.generate_cover(segments, str(out), topic="主题",
                             width=640, height=360, base_image=base)
        with Image.open(out) as img:
            px = img.convert("RGB").getpixel((320, 180))
        assert px[2] > 2 * px[0], f"应使用上传底图（蓝），而不是配图（红）：{px}"

    def test_missing_base_image_falls_back_to_default(self, tmp_path):
        """底图路径不存在：回退默认背景，封面照常产出不中断"""
        out = tmp_path / "cover.jpg"
        result = cover.generate_cover([], str(out), topic="主题",
                                      width=640, height=360,
                                      base_image=str(tmp_path / "nope.jpg"))
        assert result == str(out)
        assert out.exists()

    def test_corrupt_base_image_falls_back_to_default(self, tmp_path):
        """底图文件损坏（非图片内容）：回退默认背景，封面照常产出不中断"""
        bad = tmp_path / "bad.jpg"
        bad.write_bytes(b"not-an-image-at-all")
        out = tmp_path / "cover.jpg"
        result = cover.generate_cover([], str(out), topic="主题",
                                      width=640, height=360, base_image=str(bad))
        assert result == str(out)
        assert out.exists()
