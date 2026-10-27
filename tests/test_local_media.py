"""本地素材优先（images.scan_media_dir / assign_local_media）：
递归扫描、顺序填充、关键词匹配、视频分布、候选落位、异常降级（全部离线）"""
import os

from PIL import Image

import images


def _img(path, color=(120, 40, 40), size=(320, 180)):
    img = Image.new("RGB", size, color)
    img.save(str(path), quality=90)
    return str(path)


def _segments(n, keyword="story"):
    return [{"text": f"第{i}段内容。", "duration": 3.0, "image_keyword": keyword}
            for i in range(n)]


class TestScanMediaDir:
    def test_scans_recursively_sorted_and_skips_other_exts(self, tmp_path):
        (tmp_path / "sub").mkdir()
        _img(tmp_path / "b.jpg")
        _img(tmp_path / "sub" / "a.png")
        (tmp_path / "v.mp4").write_bytes(b"x" * 100)
        (tmp_path / "note.txt").write_text("非素材，跳过")
        (tmp_path / ".hidden.jpg").write_bytes(b"hidden file, skipped")

        imgs, vids = images.scan_media_dir(str(tmp_path))
        assert [os.path.basename(p) for p in imgs] == ["b.jpg", "a.png"]  # 全局路径排序（b < sub/）
        assert [os.path.basename(p) for p in vids] == ["v.mp4"]

    def test_corrupt_image_filtered_out(self, tmp_path):
        (tmp_path / "bad.jpg").write_bytes(b"\xff\xd8\xff\xe0truncated-not-real")
        imgs, _ = images.scan_media_dir(str(tmp_path))
        assert imgs == []

    def test_oversize_file_skipped_with_limit(self, tmp_path, monkeypatch):
        monkeypatch.setattr(images, "_LOCAL_MEDIA_FILE_LIMIT", 10)
        _img(tmp_path / "big.jpg")  # jpeg 必然 > 10 字节
        imgs, vids = images.scan_media_dir(str(tmp_path))
        assert imgs == [] and vids == []


class TestAssignLocalMedia:
    def test_missing_or_empty_dir_returns_zero(self, tmp_path):
        segs = _segments(1)
        assert images.assign_local_media(segs, "", project_dir=str(tmp_path)) == 0
        assert images.assign_local_media(
            segs, str(tmp_path / "nope"), project_dir=str(tmp_path)) == 0
        empty = tmp_path / "empty"
        empty.mkdir()
        assert images.assign_local_media(segs, str(empty), project_dir=str(tmp_path)) == 0
        assert segs[0].get("image_path") is None

    def test_images_copied_to_primary_slots_in_order(self, tmp_path):
        media = tmp_path / "media"
        media.mkdir()
        proj = tmp_path / "proj"
        proj.mkdir()
        _img(media / "a.jpg", color=(200, 10, 10))
        _img(media / "b.jpg", color=(10, 200, 10))

        segs = _segments(2)
        assigned = images.assign_local_media(segs, str(media), project_dir=str(proj))

        assert assigned == 2
        assert segs[0]["image_path"] == str(proj / "image_000.jpg")
        assert segs[1]["image_path"] == str(proj / "image_001.jpg")
        # 目录顺序即分配顺序：a→第1段、b→第2段（按首像素颜色区分）
        p0 = Image.open(str(proj / "image_000.jpg")).convert("RGB").getpixel((5, 5))
        p1 = Image.open(str(proj / "image_001.jpg")).convert("RGB").getpixel((5, 5))
        assert p0[0] > 150 and p0[1] < 60, "第 1 段应分到 a.jpg（红）"
        assert p1[1] > 150 and p1[0] < 60, "第 2 段应分到 b.jpg（绿）"

    def test_keyword_match_beats_folder_order(self, tmp_path):
        """文件名命中段关键词的图片优先配对，不被顺序填充抢先占用"""
        media = tmp_path / "media"
        media.mkdir()
        proj = tmp_path / "proj"
        proj.mkdir()
        _img(media / "gate.jpg", color=(200, 10, 10))   # 目录序第 1，但关键词属第 2 段
        _img(media / "pool.jpg", color=(10, 10, 200))

        segs = _segments(2, keyword="story")
        segs[1]["image_keyword"] = "community gate entrance"
        images.assign_local_media(segs, str(media), project_dir=str(proj))

        p1 = Image.open(str(proj / "image_001.jpg")).convert("RGB").getpixel((5, 5))
        assert p1[0] > 150 and p1[2] < 60, "gate.jpg 应命中第 2 段关键词，而非按序给第 1 段"

    def test_videos_distributed_evenly_referenced_not_copied(self, tmp_path):
        media = tmp_path / "media"
        media.mkdir()
        proj = tmp_path / "proj"
        proj.mkdir()
        (media / "v1.mp4").write_bytes(b"x" * 100)
        (media / "v2.mp4").write_bytes(b"x" * 100)

        segs = _segments(4)
        assigned = images.assign_local_media(segs, str(media), project_dir=str(proj))

        assert assigned == 2  # 2 条视频分到 4 段中的 2 段
        assert segs[0]["video_path"] == str(media / "v1.mp4")
        assert segs[2]["video_path"] == str(media / "v2.mp4")
        assert not segs[1].get("video_path") and not segs[3].get("video_path")
        # 视频只记路径不拷贝：项目目录里没有任何视频副本
        assert not list(proj.glob("*.mp4"))

    def test_video_segments_also_get_fallback_image(self, tmp_path):
        """视频段也照常配图（视频解码失败的兜底画面 + 封面素材）"""
        media = tmp_path / "media"
        media.mkdir()
        proj = tmp_path / "proj"
        proj.mkdir()
        _img(media / "a.jpg")
        _img(media / "b.jpg")
        (media / "v.mp4").write_bytes(b"x" * 100)

        segs = _segments(2)
        images.assign_local_media(segs, str(media), project_dir=str(proj))
        assert segs[0].get("video_path")
        assert segs[0]["image_path"] == str(proj / "image_000.jpg")
        assert segs[1]["image_path"] == str(proj / "image_001.jpg")

    def test_spares_become_swap_candidates(self, tmp_path):
        """剩余图片按段落成 alt 候选（每段上限内），接入既有换图约定"""
        media = tmp_path / "media"
        media.mkdir()
        proj = tmp_path / "proj"
        proj.mkdir()
        for name, color in (("a.jpg", (200, 10, 10)),
                            ("b.jpg", (10, 200, 10)),
                            ("c.jpg", (10, 10, 200))):
            _img(media / name, color=color)

        segs = _segments(1)
        images.assign_local_media(segs, str(media), project_dir=str(proj))

        names = sorted(p.name for p in proj.glob("image_000_alt_*.jpg"))
        assert len(names) == 2, f"3 张图 1 个段：主图位 + 2 张候选，实际 {names}"
        assert images.list_candidates(str(proj), 0) == \
            ["image_000.jpg"] + names  # 主图优先、alt 升序

    def test_alt_candidates_capped_per_segment(self, tmp_path, monkeypatch):
        monkeypatch.setattr(images, "_LOCAL_ALT_PER_SEGMENT", 2)
        media = tmp_path / "media"
        media.mkdir()
        proj = tmp_path / "proj"
        proj.mkdir()
        for i in range(5):
            _img(media / f"m{i}.jpg")
        segs = _segments(1)
        images.assign_local_media(segs, str(media), project_dir=str(proj))
        assert len(list(proj.glob("image_000_alt_*.jpg"))) == 2

    def test_more_segments_than_images_leaves_rest_to_cloud(self, tmp_path):
        media = tmp_path / "media"
        media.mkdir()
        proj = tmp_path / "proj"
        proj.mkdir()
        _img(media / "only.jpg")
        segs = _segments(3)
        assigned = images.assign_local_media(segs, str(media), project_dir=str(proj))
        assert assigned == 1
        assert segs[0]["image_path"] == str(proj / "image_000.jpg")
        assert not segs[1].get("image_path") and not segs[2].get("image_path")
