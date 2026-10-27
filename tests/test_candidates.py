"""配图多候选（D4）：Unsplash 相关度选图、MiniMax n>1 多候选落盘、
候选列表与换位（images.py），以及「人工换图被 A12 断点续跑继承」。

Web 接口（GET /api/candidates、POST /api/select）的 flask test client
测试在 tests/test_web.py 的 TestImageCandidatesAPI。
"""
import os

import pytest
from PIL import Image

import config
import images


def _make_jpg(path, color=(80, 80, 80)):
    """生成真实可解码的 jpg（换位与断点续跑复用判据都要求可解码）"""
    Image.new("RGB", (32, 24), color).save(str(path), quality=90)
    return str(path)


class _FakeResp:
    """requests.Response 极简替身：搜索接口走 .json()，图片下载走 .content"""

    def __init__(self, payload=None, content=b""):
        self._payload = payload
        self.content = content

    def json(self):
        return self._payload


class TestUnsplashRelevance:
    """R1 §一#7：Unsplash 默认按相关度取第一张；random_pick=True 恢复随机选图"""

    RESULTS = [
        {"urls": {"regular": "https://img.example/first.jpg"}},
        {"urls": {"regular": "https://img.example/second.jpg"}},
        {"urls": {"regular": "https://img.example/third.jpg"}},
    ]

    def _install(self, monkeypatch, downloads):
        def fake_get(url, headers=None, params=None, timeout=0):
            if url.endswith("/search/photos"):
                return _FakeResp(payload={"results": [dict(r) for r in self.RESULTS]})
            downloads.append(url)
            return _FakeResp(content=b"jpg-bytes")

        monkeypatch.setattr(images.requests, "get", fake_get)

    def test_default_picks_first_result_by_relevance(self, tmp_path, monkeypatch):
        downloads = []
        self._install(monkeypatch, downloads)

        out = images.fetch_unsplash_image("城市", 0, project_dir=str(tmp_path))

        assert out == str(tmp_path / "image_000.jpg")
        assert os.path.getsize(out) > 0
        # 相关度第一张：下载的正是 results[0] 的 regular url
        assert downloads == ["https://img.example/first.jpg"]

    def test_random_pick_flag_restores_random_choice(self, tmp_path, monkeypatch):
        downloads = []
        self._install(monkeypatch, downloads)
        # 替身 random.choice 固定返回第 2 条，验证 random_pick 参数确实接通
        monkeypatch.setattr(images.random, "choice", lambda seq: seq[1])

        out = images.fetch_unsplash_image("城市", 0, project_dir=str(tmp_path),
                                          random_pick=True)

        assert out == str(tmp_path / "image_000.jpg")
        assert downloads == ["https://img.example/second.jpg"]


class TestMinimaxMultiCandidate:
    """MiniMax n>1 多候选落盘（R1 §五 P1-1）：主图 image_XXX.jpg + alt_N 候选"""

    URLS = ["https://img.minimax/1.png", "https://img.minimax/2.png",
            "https://img.minimax/3.png"]

    def _install(self, monkeypatch, downloads, captured_payloads, urls=None,
                 fail_urls=()):
        urls = urls if urls is not None else self.URLS

        def fake_post(url, headers=None, json=None, timeout=0):
            captured_payloads.append(dict(json))
            return _FakeResp(payload={
                "base_resp": {"status_code": 0, "status_msg": "success"},
                "data": {"image_urls": list(urls)},
            })

        def fake_get(url, headers=None, timeout=0):
            if url in fail_urls:
                raise RuntimeError(f"download boom: {url}")
            downloads.append(url)
            # 内容带 url 序号，便于断言落盘顺序
            return _FakeResp(content=f"img-{urls.index(url) + 1}".encode())

        monkeypatch.setattr(images.requests, "post", fake_post)
        monkeypatch.setattr(images.requests, "get", fake_get)
        monkeypatch.setattr(config, "MINIMAX_API_KEY", "test-key")
        monkeypatch.setattr(config, "MINIMAX_BASE_URL", "https://minimax.example")

    def test_n3_saves_primary_and_two_alts(self, tmp_path, monkeypatch):
        downloads, payloads = [], []
        self._install(monkeypatch, downloads, payloads)

        out = images.generate_minimax_image("城市", 0, project_dir=str(tmp_path), n=3)

        assert out == str(tmp_path / "image_000.jpg")
        assert payloads[0]["n"] == 3  # 请求参数确实带 n>1
        assert (tmp_path / "image_000.jpg").read_bytes() == b"img-1"
        assert (tmp_path / "image_000_alt_1.jpg").read_bytes() == b"img-2"
        assert (tmp_path / "image_000_alt_2.jpg").read_bytes() == b"img-3"
        assert downloads == self.URLS

    def test_config_default_n_drives_request(self, tmp_path, monkeypatch):
        downloads, payloads = [], []
        self._install(monkeypatch, downloads, payloads)
        monkeypatch.setattr(config, "MINIMAX_IMAGE_N", 2)

        out = images.generate_minimax_image("城市", 0, project_dir=str(tmp_path))

        assert payloads[0]["n"] == 2
        assert (tmp_path / "image_000.jpg").read_bytes() == b"img-1"
        assert (tmp_path / "image_000_alt_1.jpg").read_bytes() == b"img-2"
        assert not (tmp_path / "image_000_alt_2.jpg").exists()

    def test_default_n1_keeps_old_behavior_no_alts(self, tmp_path, monkeypatch):
        downloads, payloads = [], []
        self._install(monkeypatch, downloads, payloads)
        monkeypatch.setattr(config, "MINIMAX_IMAGE_N", 1)

        out = images.generate_minimax_image("城市", 0, project_dir=str(tmp_path))

        assert payloads[0]["n"] == 1
        assert (tmp_path / "image_000.jpg").read_bytes() == b"img-1"
        assert list(tmp_path.glob("image_000_alt_*")) == []

    def test_primary_download_failure_promotes_next_url(self, tmp_path, monkeypatch):
        downloads, payloads = [], []
        self._install(monkeypatch, downloads, payloads, fail_urls=(self.URLS[0],))

        out = images.generate_minimax_image("城市", 0, project_dir=str(tmp_path), n=3)

        # 第一张下载失败：下一张顶上主图位，其余依次作候选，不整单作废
        assert out == str(tmp_path / "image_000.jpg")
        assert (tmp_path / "image_000.jpg").read_bytes() == b"img-2"
        assert (tmp_path / "image_000_alt_1.jpg").read_bytes() == b"img-3"
        assert not (tmp_path / "image_000_alt_2.jpg").exists()

    def test_all_downloads_fail_returns_none(self, tmp_path, monkeypatch):
        downloads, payloads = [], []
        self._install(monkeypatch, downloads, payloads,
                      fail_urls=tuple(self.URLS))

        out = images.generate_minimax_image("城市", 0, project_dir=str(tmp_path), n=3)

        assert out is None
        assert list(tmp_path.glob("image_000*")) == []

    def test_api_error_returns_none(self, tmp_path, monkeypatch):
        def fake_post(url, headers=None, json=None, timeout=0):
            return _FakeResp(payload={"base_resp": {"status_code": 1004,
                                                    "status_msg": "invalid key"}})

        monkeypatch.setattr(images.requests, "post", fake_post)
        monkeypatch.setattr(config, "MINIMAX_API_KEY", "test-key")

        out = images.generate_minimax_image("城市", 0, project_dir=str(tmp_path), n=2)

        assert out is None


class TestListCandidates:
    def test_primary_first_then_alts_numeric_sorted(self, tmp_path):
        for name in ("image_000.jpg", "image_000_alt_10.jpg", "image_000_alt_2.jpg",
                     "image_000_alt_1.jpg", "image_001.jpg", "image_001_alt_1.jpg",
                     "image_999_alt_1.jpg"):
            (tmp_path / name).write_bytes(b"x")

        assert images.list_candidates(str(tmp_path), 0) == [
            "image_000.jpg", "image_000_alt_1.jpg", "image_000_alt_2.jpg",
            "image_000_alt_10.jpg"]
        assert images.list_candidates(str(tmp_path), 1) == [
            "image_001.jpg", "image_001_alt_1.jpg"]

    def test_missing_primary_still_lists_alts(self, tmp_path):
        (tmp_path / "image_000_alt_1.jpg").write_bytes(b"x")
        assert images.list_candidates(str(tmp_path), 0) == ["image_000_alt_1.jpg"]

    def test_missing_dir_returns_empty(self, tmp_path):
        assert images.list_candidates(str(tmp_path / "nope"), 0) == []
        assert images.list_candidates("", 0) == []


class TestSwapImageCandidate:
    """换位语义：候选顶入主图位，原主图挪到空余 alt 槽位（仍可换回）"""

    def test_swap_moves_primary_to_free_alt_slot(self, tmp_path):
        _make_jpg(tmp_path / "image_000.jpg", (200, 10, 10))
        _make_jpg(tmp_path / "image_000_alt_1.jpg", (10, 200, 10))
        _make_jpg(tmp_path / "image_000_alt_2.jpg", (10, 10, 200))
        old_primary = (tmp_path / "image_000.jpg").read_bytes()
        picked = (tmp_path / "image_000_alt_2.jpg").read_bytes()

        out = images.swap_image_candidate(str(tmp_path), 0, "image_000_alt_2.jpg")

        assert out == str(tmp_path / "image_000.jpg")
        assert (tmp_path / "image_000.jpg").read_bytes() == picked
        # 原主图挪到现有最大 alt 槽位之后（alt_3），候选 alt_2 槽位腾空
        assert (tmp_path / "image_000_alt_3.jpg").read_bytes() == old_primary
        assert not (tmp_path / "image_000_alt_2.jpg").exists()

    def test_swap_without_primary_just_promotes(self, tmp_path):
        _make_jpg(tmp_path / "image_000_alt_1.jpg", (10, 200, 10))

        out = images.swap_image_candidate(str(tmp_path), 0, "image_000_alt_1.jpg")

        assert out == str(tmp_path / "image_000.jpg")
        assert (tmp_path / "image_000.jpg").stat().st_size > 0
        assert not (tmp_path / "image_000_alt_1.jpg").exists()

    def test_swap_current_primary_is_idempotent(self, tmp_path):
        _make_jpg(tmp_path / "image_000.jpg", (200, 10, 10))
        before = (tmp_path / "image_000.jpg").read_bytes()

        out = images.swap_image_candidate(str(tmp_path), 0, "image_000.jpg")

        assert out == str(tmp_path / "image_000.jpg")
        assert (tmp_path / "image_000.jpg").read_bytes() == before
        assert list(tmp_path.glob("image_000_alt_*")) == []

    def test_swap_rejects_foreign_or_bad_names(self, tmp_path):
        _make_jpg(tmp_path / "image_000.jpg")
        _make_jpg(tmp_path / "image_001_alt_1.jpg")

        with pytest.raises(ValueError):
            images.swap_image_candidate(str(tmp_path), 0, "image_001_alt_1.jpg")
        with pytest.raises(ValueError):
            images.swap_image_candidate(str(tmp_path), 0, "../image_000_alt_1.jpg")
        with pytest.raises(ValueError):
            images.swap_image_candidate(str(tmp_path), 0, "sub/dir/image_000_alt_1.jpg")
        with pytest.raises(FileNotFoundError):
            images.swap_image_candidate(str(tmp_path), 0, "image_000_alt_9.jpg")

    def test_selection_survives_a12_resume(self, tmp_path, monkeypatch):
        """换图后 image_XXX.jpg 即选中内容：A12 断点续跑复用主图位，
        不再触发任何配图来源（人工选择被继承，不会被随机/重选覆盖）"""
        _make_jpg(tmp_path / "image_000.jpg", (200, 10, 10))
        _make_jpg(tmp_path / "image_000_alt_1.jpg", (10, 200, 10))
        images.swap_image_candidate(str(tmp_path), 0, "image_000_alt_1.jpg")

        def must_not_fetch(*a, **k):
            raise AssertionError("换图后断点续跑不应再触发任何配图来源")

        monkeypatch.setattr(images, "fetch_image_for_segment", must_not_fetch)

        result = images.fetch_all_images(
            [{"text": "s", "duration": 5.0, "image_keyword": "k"}],
            project_dir=str(tmp_path))

        assert result[0]["image_path"] == str(tmp_path / "image_000.jpg")
        assert result[0].get("image_reused") is True
        assert (tmp_path / "image_000.jpg").read_bytes() != b""  # 未被覆盖
