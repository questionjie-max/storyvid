"""web/app.py：接口与两阶段任务流（管线全部 mock，离线运行）"""
import io
import os
import time
from pathlib import Path

import pytest
from PIL import Image

import config
import segment_state
from web import app as webapp


STORY_TEXT = "这是测试故事稿的第一段，字数足够长不会被过滤掉。\n\n这是第二段，同样足够长，包含多个句子。"


@pytest.fixture
def client(tmp_path, monkeypatch):
    import research
    import story
    import tts
    import images
    import video

    # 项目目录指向临时目录
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setattr(config, "PROJECT_DIR", None)
    monkeypatch.setattr(config, "TEMP_DIR", None)
    # 上传素材目录指向临时目录（/api/upload 落盘到 GLOBAL_TEMP_DIR/uploads/）
    monkeypatch.setattr(config, "GLOBAL_TEMP_DIR", str(tmp_path / "temp"))

    # 阶段一：搜索与写稿
    monkeypatch.setattr(research, "search_topic", lambda *a, **k: [])
    monkeypatch.setattr(research, "analyze_content",
                        lambda *a, **k: {"is_hot": False, "is_safe": True, "risk_level": "low",
                                         "reason": "测试", "suggestions": ""})
    monkeypatch.setattr(story, "generate_story_with_retry", lambda *a, **k: STORY_TEXT)

    def fake_keywords(segments, topic):
        for i, s in enumerate(segments):
            s["image_keyword"] = f"keyword_{i}"
        return segments

    monkeypatch.setattr(story, "generate_image_keywords", fake_keywords)

    # 阶段二：管线各步（marker 文件用于验证各步收到的是同一个任务目录）
    def fake_speech(segments, provider="", voice="", project_dir=""):
        if project_dir:
            with open(os.path.join(project_dir, "tts_marker.txt"), "w") as f:
                f.write("ok")
        for s in segments:
            s["audio_path"] = ""
            s["duration"] = 1.0
        return segments

    def fake_images(segments, image_source="", aspect_ratio="", project_dir=""):
        if project_dir:
            with open(os.path.join(project_dir, "img_marker.txt"), "w") as f:
                f.write("ok")
        for s in segments:
            s["image_path"] = None
        return segments

    def fake_video(segments, output_path, width=0, height=0, project_dir="", topic="",
                   bgm_path="", frame_tag=""):
        if project_dir:
            with open(os.path.join(project_dir, "video_marker.txt"), "w") as f:
                f.write("ok")
        with open(output_path, "wb") as f:
            f.write(b"fake mp4 content" * 100)
        return output_path

    monkeypatch.setattr(tts, "generate_all_speech", fake_speech)
    monkeypatch.setattr(images, "fetch_all_images", fake_images)
    monkeypatch.setattr(video, "create_video", fake_video)

    webapp._tasks.clear()
    return webapp.app.test_client()


def wait_status(client, task_id, want, timeout=10):
    deadline = time.time() + timeout
    data = {}
    while time.time() < deadline:
        resp = client.get(f"/api/status/{task_id}")
        assert resp.status_code == 200
        data = resp.get_json()
        if data.get("status") == want:
            break
        time.sleep(0.05)
    else:
        raise AssertionError(f"任务未在 {timeout}s 内进入 {want}: {data}")
    # 终态只是工作线程的中间写入（后面还有 message/traceback 等收尾），
    # 必须 join 到线程真正退出，否则守护线程的收尾写入会与下个用例的
    # _tasks.clear() 竞态，产生偶发 KeyError 线程异常
    for t in list(webapp._worker_threads):
        t.join(timeout=timeout)
    return data


class TestBasicRoutes:
    def test_index(self, client):
        resp = client.get("/")
        assert resp.status_code == 200

    def test_generate_requires_topic(self, client):
        resp = client.post("/api/generate", json={"topic": "  "})
        assert resp.status_code == 400

    def test_status_unknown_task(self, client):
        assert client.get("/api/status/nope").status_code == 404

    def test_download_rejects_traversal(self, client):
        resp = client.get("/api/download/..%2f..%2fetc%2fpasswd")
        assert resp.status_code in (403, 404)


class TestTwoPhaseFlow:
    def test_confirm_then_complete(self, client, tmp_path, monkeypatch):
        # 外挂 SRT 默认关闭（同目录 SRT 会被播放器自动加载、与烧录字幕叠成
        # 两层，用户两次反馈的根因）；本用例显式开启以验证落盘行为
        monkeypatch.setattr(config, "SUBTITLES_SRT", True)
        resp = client.post("/api/generate", json={"topic": "测试主题", "auto": False})
        assert resp.status_code == 200
        task_id = resp.get_json()["task_id"]

        data = wait_status(client, task_id, "awaiting_confirmation")
        assert "测试故事稿" in data["story"]

        resp = client.post(f"/api/confirm/{task_id}")
        assert resp.status_code == 200

        data = wait_status(client, task_id, "completed")
        assert data["progress"] == 100
        # 产物真实落盘
        project_dir = tmp_path / "output"
        videos = list(project_dir.rglob("*.mp4"))
        assert len(videos) == 1
        assert videos[0].stat().st_size > 0
        assert (videos[0].parent / "story.txt").exists()
        assert (videos[0].parent / "subtitles.srt").exists()

    def test_srt_not_written_by_default(self, client, tmp_path):
        """回归（用户反馈「两层字幕」）：默认不在项目目录落盘 subtitles.srt
        ——字幕已烧录进画面，同目录 SRT 会被播放器自动加载叠成第二层"""
        resp = client.post("/api/generate", json={"topic": "无外挂字幕主题", "auto": True})
        task_id = resp.get_json()["task_id"]
        wait_status(client, task_id, "completed")

        videos = list((tmp_path / "output").rglob("*.mp4"))
        assert len(videos) == 1
        assert not (videos[0].parent / "subtitles.srt").exists(), \
            "默认关闭外挂 SRT：播放器会自动加载同目录 SRT,与烧录字幕叠成两层"
        # 管线本身正常跑完（段级状态落盘；字幕烧录由 video 层负责，
        # 本用例的 create_video 是替身，不产出字幕帧文件）
        assert (videos[0].parent / "segments.json").exists()

    def test_auto_mode_runs_through(self, client, tmp_path):
        resp = client.post("/api/generate", json={"topic": "全自动主题", "auto": True})
        task_id = resp.get_json()["task_id"]
        data = wait_status(client, task_id, "completed")
        assert data["output_file"].endswith(".mp4")

    def test_pipeline_writes_into_task_project_dir(self, client, tmp_path):
        """回归测试：TTS/配图/合成三步必须收到同一个任务目录，而不是回读全局 PROJECT_DIR"""
        resp = client.post("/api/generate", json={"topic": "目录隔离主题", "auto": True})
        task_id = resp.get_json()["task_id"]
        wait_status(client, task_id, "completed")

        videos = list((tmp_path / "output").rglob("*.mp4"))
        assert len(videos) == 1
        project_dir = videos[0].parent
        assert (project_dir / "tts_marker.txt").exists()
        assert (project_dir / "img_marker.txt").exists()
        assert (project_dir / "video_marker.txt").exists()

    def test_preview_env_flags_task_status_and_size(self, client, monkeypatch):
        """回归（B 队 mustFix）：PREVIEW=1 时 Web 出片同样降为预览分辨率——
        web/app.py 的宽高来自 config.video_size()（跟随全局开关）再显式传
        create_video。此时任务状态必须明示预览，不得静默出预览片：
        create_video 收到的宽高 = 960×540（16:9 默认）、完成态 message
        含预览标注、preview 字段为 True。
        """
        monkeypatch.setattr(config, "PREVIEW", True)
        captured = {}
        import video as video_mod

        def fake_video(segments, output_path, width=0, height=0, project_dir="", topic="",
                   bgm_path="", frame_tag=""):
            captured["size"] = (width, height)
            with open(output_path, "wb") as f:
                f.write(b"fake mp4 content" * 100)
            return output_path

        monkeypatch.setattr(video_mod, "create_video", fake_video)

        resp = client.post("/api/generate", json={"script": STORY_TEXT, "topic": "预览任务"})
        assert resp.status_code == 200
        task_id = resp.get_json()["task_id"]
        data = wait_status(client, task_id, "completed")

        assert captured["size"] == (960, 540), \
            f"PREVIEW=1 时 Web 合成应取预览横屏 960×540，实际 {captured['size']}"
        assert data.get("preview") is True
        assert "预览" in data["message"], f"完成态 message 应标注预览: {data['message']}"

    def test_no_preview_env_keeps_full_size_and_silent(self, client, monkeypatch):
        """回归（B 队 mustFix 对照面）：PREVIEW 关闭时 Web 走全分辨率、任务无 preview 标注"""
        monkeypatch.setattr(config, "PREVIEW", False)
        captured = {}
        import video as video_mod

        def fake_video(segments, output_path, width=0, height=0, project_dir="", topic="",
                   bgm_path="", frame_tag=""):
            captured["size"] = (width, height)
            with open(output_path, "wb") as f:
                f.write(b"fake mp4 content" * 100)
            return output_path

        monkeypatch.setattr(video_mod, "create_video", fake_video)

        resp = client.post("/api/generate", json={"script": STORY_TEXT, "topic": "全分辨率任务"})
        task_id = resp.get_json()["task_id"]
        data = wait_status(client, task_id, "completed")

        assert captured["size"] == (1920, 1080)
        assert "preview" not in data
        assert "预览" not in data["message"]

    def test_regenerate_resets_story(self, client):
        resp = client.post("/api/generate", json={"topic": "重生成主题", "auto": False})
        task_id = resp.get_json()["task_id"]
        wait_status(client, task_id, "awaiting_confirmation")

        resp = client.post(f"/api/regenerate/{task_id}")
        assert resp.status_code == 200
        data = wait_status(client, task_id, "awaiting_confirmation")
        assert data["story"] == STORY_TEXT


class TestScriptDirect:
    """文稿直出：/api/generate 的 script 字段与 topic 二选一，
    跳过搜索/审核/LLM 写稿，直接进视频管线，全程不调 llm.chat"""

    def test_script_without_topic_completes_without_llm(self, client, tmp_path, monkeypatch):
        # 外挂 SRT 默认关闭（双层字幕根因）；本用例显式开启以验证落盘行为
        monkeypatch.setattr(config, "SUBTITLES_SRT", True)
        import llm

        llm_calls = []
        monkeypatch.setattr(llm, "chat", lambda *a, **k: llm_calls.append("called"))

        import story as story_mod

        def no_llm_keywords(*a, **k):
            raise AssertionError("文稿直出模式不应调用 LLM 配图关键词")

        monkeypatch.setattr(story_mod, "generate_image_keywords", no_llm_keywords)

        resp = client.post("/api/generate", json={"script": STORY_TEXT, "auto": False})
        assert resp.status_code == 200
        task_id = resp.get_json()["task_id"]

        data = wait_status(client, task_id, "completed")
        assert data["progress"] == 100
        assert data.get("script_mode") is True
        # 文稿直出不开确认环节（稿子是用户自己写的），auto=False 也直接出片
        assert llm_calls == []

        videos = list((tmp_path / "output").rglob("*.mp4"))
        assert len(videos) == 1
        assert (videos[0].parent / "story.txt").read_text(encoding="utf-8") == STORY_TEXT
        assert (videos[0].parent / "subtitles.srt").exists()

    def test_script_with_explicit_topic(self, client, tmp_path):
        resp = client.post("/api/generate", json={"script": STORY_TEXT, "topic": "我的主题"})
        assert resp.status_code == 200
        task_id = resp.get_json()["task_id"]
        data = wait_status(client, task_id, "completed")
        assert data["topic"] == "我的主题"

        project_dirs = [p.name for p in (tmp_path / "output").iterdir() if p.is_dir()]
        assert any("我的主题" in d for d in project_dirs)

    def test_script_topic_derived_from_first_line(self, client):
        script = "自定义首行标题\n\n正文段落，字数足够长不会被分段逻辑过滤掉。"
        resp = client.post("/api/generate", json={"script": script})
        task_id = resp.get_json()["task_id"]
        data = wait_status(client, task_id, "completed")
        assert data["topic"] == "自定义首行标题"

    def test_neither_script_nor_topic_rejected(self, client):
        resp = client.post("/api/generate", json={})
        assert resp.status_code == 400
        assert "至少提供" in resp.get_json()["error"]

        resp = client.post("/api/generate", json={"topic": "  ", "script": "   "})
        assert resp.status_code == 400

    def test_regenerate_rejected_for_script_task(self, client):
        resp = client.post("/api/generate", json={"script": STORY_TEXT, "auto": False})
        task_id = resp.get_json()["task_id"]
        wait_status(client, task_id, "completed")

        resp = client.post(f"/api/regenerate/{task_id}")
        assert resp.status_code == 400
        assert "文稿直出" in resp.get_json()["error"]


class TestFailurePath:
    def test_story_generation_failure_marks_failed(self, client, monkeypatch):
        import story

        def boom(*a, **k):
            raise RuntimeError("LLM 挂了")
        monkeypatch.setattr(story, "generate_story_with_retry", boom)

        resp = client.post("/api/generate", json={"topic": "失败主题", "auto": False})
        task_id = resp.get_json()["task_id"]
        data = wait_status(client, task_id, "failed")
        assert "LLM 挂了" in data["message"]

    def test_unsafe_content_blocked(self, client, monkeypatch):
        import research
        monkeypatch.setattr(research, "search_topic", lambda *a, **k: [{"title": "t", "body": "b"}])
        monkeypatch.setattr(research, "analyze_content",
                            lambda *a, **k: {"is_hot": True, "is_safe": False, "risk_level": "high",
                                             "reason": "敏感内容", "suggestions": "换个主题"})
        resp = client.post("/api/generate", json={"topic": "危险主题", "auto": True})
        task_id = resp.get_json()["task_id"]
        data = wait_status(client, task_id, "failed")
        assert "敏感内容" in data["message"]


class TestConcurrencyGuards:
    def test_regenerate_rejected_while_running(self, client):
        """排队中/运行中的任务不能重开线程：两个线程会同时写同一任务的状态和目录"""
        webapp._tasks["t-busy"] = {"id": "t-busy", "status": "running", "topic": "x"}
        resp = client.post("/api/regenerate/t-busy")
        assert resp.status_code == 400
        assert "正在处理" in resp.get_json()["error"]

        webapp._tasks["t-queued"] = {"id": "t-queued", "status": "queued", "topic": "x"}
        resp = client.post("/api/regenerate/t-queued")
        assert resp.status_code == 400

    def test_prune_only_removes_finished_tasks(self, client, monkeypatch):
        """清理超限任务时绝不能拿掉还在跑的任务（工作线程要往里写状态）"""
        monkeypatch.setattr(webapp, "_MAX_TASKS", 2)
        webapp._tasks["t-done"] = {"id": "t-done", "status": "completed"}
        webapp._tasks["t-busy"] = {"id": "t-busy", "status": "running"}

        resp = client.post("/api/generate", json={"topic": "清理测试主题"})
        assert resp.status_code == 200
        new_id = resp.get_json()["task_id"]

        assert "t-done" not in webapp._tasks      # 终态任务被清理
        assert "t-busy" in webapp._tasks          # 运行中的任务必须保留
        assert new_id in webapp._tasks            # 新任务在
        assert len(webapp._tasks) == 2

        # 本测试不 mock 打断线程：必须等后台线程走完再结束，
        # 否则 monkeypatch 拆除后守护线程仍持真实模块运行，会与下个用例竞态
        wait_status(client, new_id, "awaiting_confirmation")

    def test_prune_tasks_snapshot_iteration(self, monkeypatch):
        """回归测试（B队检测场景1）：遍历 _tasks 期间其他线程插入新任务，
        不得抛 RuntimeError("dictionary changed size during iteration")"""
        import threading

        monkeypatch.setattr(webapp, "_MAX_TASKS", 3)
        webapp._tasks.clear()
        # 预填超过上限的终态任务，让每次 prune 都真的走遍历分支
        for i in range(50):
            webapp._tasks[f"t-{i}"] = {"id": f"t-{i}", "status": "completed"}

        stop = threading.Event()
        insert_errors = []

        def inserter():
            i = 0
            while not stop.is_set():
                try:
                    webapp._tasks[f"tmp-{i}"] = {"id": f"tmp-{i}", "status": "completed"}
                except Exception as e:  # pragma: no cover - 防御
                    insert_errors.append(e)
                i += 1

        th = threading.Thread(target=inserter)
        th.start()
        prune_errors = []
        try:
            for _ in range(500):
                try:
                    webapp._prune_tasks()
                except Exception as e:
                    prune_errors.append(e)
        finally:
            stop.set()
            th.join()

        assert prune_errors == []
        assert insert_errors == []

    def test_concurrent_generate_tasks_all_start(self, client, monkeypatch):
        """回归测试（B队检测场景2）：并发 /api/generate 下所有任务都必须成功
        启动工作线程并推进状态，不允许任何任务永久卡在 queued"""
        import threading

        monkeypatch.setattr(webapp, "_MAX_TASKS", 5)  # 压到 5，让每次请求都触发 prune 遍历
        errors = []
        task_ids = []
        ids_lock = threading.Lock()

        def fire(n):
            try:
                resp = client.post("/api/generate", json={"topic": f"并发主题{n}"})
                with ids_lock:
                    task_ids.append(resp.get_json()["task_id"])
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=fire, args=(n,)) for n in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert len(task_ids) == 20

        # 等全部任务离开 queued（工作线程都被成功拉起并推进到待确认）
        deadline = time.time() + 10
        statuses = {}
        while time.time() < deadline:
            statuses = {
                tid: webapp._tasks[tid]["status"]
                for tid in task_ids if tid in webapp._tasks
            }
            if len(statuses) == 20 and all(s != "queued" for s in statuses.values()):
                break
            time.sleep(0.05)
        assert len(statuses) == 20, f"有任务从 _tasks 消失: {20 - len(statuses)} 个"
        stuck = [tid for tid, s in statuses.items() if s == "queued"]
        assert stuck == [], f"有任务永久卡在 queued: {stuck}"


class TestUpload:
    """素材上传 /api/upload：类型白名单 / 20MB 上限 / 文件名清洗防穿越 /
    与 /api/generate 的 upload_id 联动（BGM/封面/文稿优先使用已上传素材）"""

    def _upload(self, client, upload_type, filename, content):
        return client.post("/api/upload", data={
            "type": upload_type,
            "file": (io.BytesIO(content), filename),
        })

    def _uploads_dir(self, upload_type) -> Path:
        return Path(config.GLOBAL_TEMP_DIR) / "uploads" / upload_type

    # ---------- 合法上传 ----------

    def test_upload_bgm_valid(self, client):
        resp = self._upload(client, "bgm", "夜曲.mp3", b"ID3fake-mp3-bytes")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["type"] == "bgm"
        assert data["filename"] == "夜曲.mp3"
        assert data["size"] == len(b"ID3fake-mp3-bytes")
        stored = self._uploads_dir("bgm") / data["upload_id"]
        assert stored.is_file(), f"上传文件应落盘在 uploads/bgm/ 下: {stored}"
        assert stored.read_bytes() == b"ID3fake-mp3-bytes"

    def test_upload_cover_valid_case_insensitive_ext(self, client):
        for name in ("封面.JPG", "base.png"):
            resp = self._upload(client, "cover", name, b"\x89PNG-fake")
            assert resp.status_code == 200, f"{name} 应在白名单内（大小写不敏感）"
            assert resp.get_json()["type"] == "cover"

    def test_upload_script_valid(self, client):
        resp = self._upload(client, "script", "稿子.txt", "第一行\n\n第二段".encode("utf-8"))
        assert resp.status_code == 200
        assert resp.get_json()["type"] == "script"

    def test_upload_boundary_exactly_20mb_accepted(self, client):
        """单文件上限语义是 ≤20MB：恰好 20MB 必须成功"""
        content = b"x" * (20 * 1024 * 1024)
        resp = self._upload(client, "bgm", "big.wav", content)
        assert resp.status_code == 200
        assert resp.get_json()["size"] == 20 * 1024 * 1024

    # ---------- 非法类型 / 参数缺失 ----------

    def test_upload_rejects_extension_outside_whitelist(self, client):
        cases = [("bgm", "song.exe"), ("bgm", "photo.png"), ("cover", "song.mp3"),
                 ("cover", "doc.pdf"), ("script", "image.jpg")]
        for upload_type, name in cases:
            resp = self._upload(client, upload_type, name, b"data")
            assert resp.status_code == 400, f"{upload_type} + {name} 应被白名单拒绝"
            assert "不支持的文件类型" in resp.get_json()["error"]

    def test_upload_rejects_no_extension(self, client):
        resp = self._upload(client, "bgm", "没有扩展名", b"data")
        assert resp.status_code == 400

    def test_upload_rejects_unknown_type(self, client):
        resp = self._upload(client, "video", "clip.mp4", b"data")
        assert resp.status_code == 400
        assert "type" in resp.get_json()["error"]

    def test_upload_requires_file(self, client):
        resp = client.post("/api/upload", data={"type": "bgm"})
        assert resp.status_code == 400
        assert "缺少上传文件" in resp.get_json()["error"]

    def test_upload_rejects_empty_file(self, client):
        resp = self._upload(client, "bgm", "empty.mp3", b"")
        assert resp.status_code == 400
        assert "空文件" in resp.get_json()["error"]

    # ---------- 超大文件 ----------

    def test_upload_rejects_oversized_and_cleans_partial(self, client):
        """超过 20MB 拒绝（400），且不留下半成品文件"""
        content = b"x" * (20 * 1024 * 1024 + 1)
        resp = self._upload(client, "bgm", "too_big.mp3", content)
        assert resp.status_code == 400
        assert "20MB" in resp.get_json()["error"]
        assert list(self._uploads_dir("bgm").glob("*")) == [], "超限半成品必须被清理"

    # ---------- 文件名清洗防路径穿越 ----------

    def test_upload_sanitizes_traversal_filename(self, client):
        """穿越文件名只保留 basename：文件必须落在 uploads/bgm/ 内，路径不含分隔符"""
        for evil in ("../../evil.mp3", r"..\..\evil2.mp3", "a/b/c/浅层.mp3"):
            resp = self._upload(client, "bgm", evil, b"ID3x")
            assert resp.status_code == 200, f"穿越文件名应被清洗而非拒绝: {evil}"
            upload_id = resp.get_json()["upload_id"]
            assert "/" not in upload_id and "\\" not in upload_id
            stored = self._uploads_dir("bgm") / upload_id
            assert stored.is_file(), f"落盘路径应在上传目录内: {stored}"

    def test_upload_dotdot_name_falls_back(self, client):
        """清洗后为空（如 ".." 无扩展名）不应逃逸出上传目录"""
        resp = self._upload(client, "bgm", "..", b"ID3x")
        assert resp.status_code == 400  # 无合法扩展名，白名单拒绝

    # ---------- /api/generate 联动 ----------

    def test_generate_rejects_unknown_upload_id(self, client):
        resp = client.post("/api/generate",
                           json={"topic": "x", "bgm_upload_id": "../../etc/passwd"})
        assert resp.status_code == 400
        assert "BGM" in resp.get_json()["error"]

        resp = client.post("/api/generate",
                           json={"topic": "x", "cover_upload_id": "ghost.png"})
        assert resp.status_code == 400
        assert "封面" in resp.get_json()["error"]

        resp = client.post("/api/generate",
                           json={"topic": "x", "script_upload_id": "ghost.txt"})
        assert resp.status_code == 400
        assert "文稿" in resp.get_json()["error"]

    def test_generate_wires_uploaded_bgm_and_cover(self, client, monkeypatch):
        """上传的 BGM/封面底图随任务进入项目目录，管线收到拷贝后的路径"""
        import video as video_mod
        import cover as cover_mod

        captured = {}

        def fake_video(segments, output_path, width=0, height=0, project_dir="", topic="",
                       bgm_path="", frame_tag=""):
            captured["bgm_path"] = bgm_path
            with open(output_path, "wb") as f:
                f.write(b"fake mp4 content" * 100)
            return output_path

        def fake_cover(segments, output_path, topic="", total_duration=0.0, width=0,
                       height=0, project_dir="", base_image=""):
            captured["cover_base"] = base_image
            with open(output_path, "wb") as f:
                f.write(b"fake jpg")
            return output_path

        monkeypatch.setattr(video_mod, "create_video", fake_video)
        monkeypatch.setattr(cover_mod, "generate_cover", fake_cover)

        bgm_id = self._upload(client, "bgm", "my.mp3", b"ID3fake").get_json()["upload_id"]
        cover_id = self._upload(client, "cover", "底图.png", b"\x89PNGfake").get_json()["upload_id"]

        resp = client.post("/api/generate", json={
            "topic": "素材主题", "auto": True,
            "bgm_upload_id": bgm_id, "cover_upload_id": cover_id})
        assert resp.status_code == 200
        task_id = resp.get_json()["task_id"]
        data = wait_status(client, task_id, "completed")

        bgm_path = Path(captured["bgm_path"])
        cover_base = Path(captured["cover_base"])
        assert bgm_path.name == "bgm_user.mp3"
        assert bgm_path.read_bytes() == b"ID3fake"
        assert cover_base.name == "cover_base.png"
        assert cover_base.read_bytes() == b"\x89PNGfake"
        assert bgm_path.parent == cover_base.parent, "BGM 与底图应拷进同一项目目录"
        assert data["user_bgm"] == "bgm_user.mp3"
        assert data["user_cover"] == "cover_base.png"

    def test_confirm_flow_passes_uploads_through(self, client, monkeypatch):
        """两阶段流程：上传的 BGM 在 confirm 阶段也要传给视频任务"""
        import video as video_mod

        captured = {}

        def fake_video(segments, output_path, width=0, height=0, project_dir="", topic="",
                       bgm_path="", frame_tag=""):
            captured["bgm_path"] = bgm_path
            with open(output_path, "wb") as f:
                f.write(b"fake mp4 content" * 100)
            return output_path

        monkeypatch.setattr(video_mod, "create_video", fake_video)

        bgm_id = self._upload(client, "bgm", "confirm.mp3", b"ID3confirm").get_json()["upload_id"]
        resp = client.post("/api/generate",
                           json={"topic": "确认流素材", "auto": False, "bgm_upload_id": bgm_id})
        task_id = resp.get_json()["task_id"]
        wait_status(client, task_id, "awaiting_confirmation")

        assert client.post(f"/api/confirm/{task_id}").status_code == 200
        wait_status(client, task_id, "completed")
        assert Path(captured["bgm_path"]).name == "bgm_user.mp3"
        assert Path(captured["bgm_path"]).read_bytes() == b"ID3confirm"

    def test_script_upload_drives_script_mode(self, client, monkeypatch, tmp_path):
        """上传 txt 文稿 → 文稿直出：不调 LLM，主题取首行，story.txt 与上传内容一致"""
        import llm

        calls = []
        monkeypatch.setattr(llm, "chat", lambda *a, **k: calls.append("called"))

        script = "上传文稿主题\n\n" + STORY_TEXT
        up = self._upload(client, "script", "稿子.txt", script.encode("utf-8")).get_json()
        resp = client.post("/api/generate", json={"script_upload_id": up["upload_id"]})
        assert resp.status_code == 200
        task_id = resp.get_json()["task_id"]
        data = wait_status(client, task_id, "completed")

        assert data.get("script_mode") is True
        assert data["topic"] == "上传文稿主题"
        assert calls == [], "文稿直出模式全程不应调用 llm.chat"
        story_files = list((tmp_path / "output").rglob("story.txt"))
        assert len(story_files) == 1
        assert story_files[0].read_text(encoding="utf-8") == script


class TestBatchAspectsWeb:
    """画幅多选批量渲染（aspect_ratios）：一次任务出横竖两版，
    TTS 只调一次（音频与画幅无关），第二画幅配图走智能裁切（管线全部 mock，离线）"""

    def test_multi_aspect_renders_all(self, client, tmp_path, monkeypatch):
        # 外挂 SRT 默认关闭（双层字幕根因）；本用例显式开启以验证「字幕一份共用」
        monkeypatch.setattr(config, "SUBTITLES_SRT", True)
        import tts as tts_mod
        import video as video_mod

        calls = {"speech": 0, "video": []}

        def counting_speech(segments, provider="", voice="", project_dir=""):
            calls["speech"] += 1
            for s in segments:
                s["audio_path"] = ""
                s["duration"] = 1.0
            return segments

        monkeypatch.setattr(tts_mod, "generate_all_speech", counting_speech)

        def fake_video(segments, output_path, width=0, height=0, project_dir="", topic="",
                       bgm_path="", frame_tag=""):
            calls["video"].append((os.path.basename(output_path), width, height))
            with open(output_path, "wb") as f:
                f.write(b"fake mp4 content" * 100)
            return output_path

        monkeypatch.setattr(video_mod, "create_video", fake_video)

        resp = client.post("/api/generate", json={
            "script": STORY_TEXT, "topic": "batch",
            "aspect_ratios": ["16:9", "9:16"], "auto": False})
        assert resp.status_code == 200
        task_id = resp.get_json()["task_id"]
        data = wait_status(client, task_id, "completed")

        # TTS 复用：两个画幅只生成一次语音
        assert calls["speech"] == 1
        # 两个画幅各自成片，尺寸互不相同（预览关 → 全分辨率）
        assert [o["aspect"] for o in data["outputs"]] == ["16:9", "9:16"]
        assert {o["file"] for o in data["outputs"]} == {"batch_16x9.mp4", "batch_9x16.mp4"}
        assert calls["video"] == [
            ("batch_16x9.mp4", 1920, 1080),
            ("batch_9x16.mp4", 1080, 1920),
        ]
        # 兼容字段指向首个画幅
        assert data["output_file"] == "batch_16x9.mp4"
        assert data["aspect_ratios"] == ["16:9", "9:16"]
        # 两个成片真实落盘
        files = {p.name for p in (tmp_path / "output").rglob("*.mp4")}
        assert files == {"batch_16x9.mp4", "batch_9x16.mp4"}
        # 字幕一份共用；完成态 message 为批量文案
        srts = list((tmp_path / "output").rglob("subtitles.srt"))
        assert len(srts) == 1
        assert "批量渲染完成" in data["message"]

    def test_legacy_single_aspect_field_unchanged(self, client, tmp_path):
        """兼容旧行为：只传 aspect_ratio 单值 → 单画幅输出、无画幅后缀"""
        resp = client.post("/api/generate", json={
            "script": STORY_TEXT, "topic": "legacy", "aspect_ratio": "9:16"})
        task_id = resp.get_json()["task_id"]
        data = wait_status(client, task_id, "completed")
        assert data["aspect_ratios"] == ["9:16"]
        assert data["output_file"] == "legacy.mp4"  # 单画幅不加 _9x16 后缀
        assert "outputs" not in data or len(data["outputs"]) == 1
        assert len(list((tmp_path / "output").rglob("*.mp4"))) == 1

    def test_invalid_aspects_fall_back_to_landscape(self, client):
        """全非法画幅回退 ["16:9"]（与 config.video_size 旧兜底一致），不 500"""
        resp = client.post("/api/generate", json={
            "script": STORY_TEXT, "topic": "inv",
            "aspect_ratios": ["4:3", "", 42]})
        assert resp.status_code == 200
        task_id = resp.get_json()["task_id"]
        data = wait_status(client, task_id, "completed")
        assert data["aspect_ratios"] == ["16:9"]

    def test_normalize_aspects_shapes(self):
        """_normalize_aspects：字符串/逗号串/列表/元组/非法形态都归一化"""
        assert webapp._normalize_aspects("9:16") == ["9:16"]
        assert webapp._normalize_aspects("16:9,9:16") == ["16:9", "9:16"]
        assert webapp._normalize_aspects(["16:9", "9:16", "16:9"]) == ["16:9", "9:16"]
        assert webapp._normalize_aspects(("9:16",)) == ["9:16"]
        assert webapp._normalize_aspects("") == ["16:9"]      # 空回退默认
        assert webapp._normalize_aspects(None) == ["16:9"]
        assert webapp._normalize_aspects("4:3") == ["16:9"]   # 非法回退默认


class TestSegmentEditAPI:
    """分段编辑局部重跑（D6，R1 §五 P1-3 / §一#8）：GET /api/segments 列出段状态、
    POST /api/segment/edit 改词后只重新 TTS 该段（其余段走 A12 断点续跑复用），
    再按 D4 的 reuse_frames 局部重渲合成；产物更新且其他段不受影响"""

    def _resume_speech(self, monkeypatch):
        """断点续跑语义的 TTS 替身：项目目录里已存在且非空的 segment_XXX.mp3
        直接复用（打 audio_reused），缺失的才重新「生成」——音频内容随文本变化，
        便于验证只重跑了被改段。generated 为累计清单、last 为最近一次调用的清单。"""
        import tts as tts_mod

        state = {"generated": [], "last": []}

        def resume_speech(segments, provider="", voice="", project_dir=""):
            generated_now = []
            for i, s in enumerate(segments):
                path = os.path.join(project_dir, f"segment_{i:03d}.mp3")
                if os.path.isfile(path) and os.path.getsize(path) > 0:
                    s["audio_path"] = path
                    s["duration"] = 1.0
                    s["audio_reused"] = True
                else:
                    with open(path, "wb") as f:
                        f.write(f"audio::{s['text']}".encode("utf-8"))
                    s["audio_path"] = path
                    s["duration"] = 2.0
                    s.pop("audio_reused", None)
                    generated_now.append(i)
            state["last"] = generated_now
            state["generated"].extend(generated_now)
            return segments

        monkeypatch.setattr(tts_mod, "generate_all_speech", resume_speech)
        return state

    def _completed_task(self, client, monkeypatch):
        """跑完一个真实带 segment_XXX.mp3 的已完成任务（TTS 替身遵循断点续跑语义）"""
        state = self._resume_speech(monkeypatch)
        resp = client.post("/api/generate",
                           json={"script": STORY_TEXT, "topic": "改词主题", "auto": False})
        assert resp.status_code == 200
        task_id = resp.get_json()["task_id"]
        data = wait_status(client, task_id, "completed")
        return task_id, Path(data["project_dir"]), data, state

    def _record_video(self, monkeypatch):
        import video as video_mod

        calls = []

        def recording_video(segments, output_path, **kwargs):
            calls.append({"kwargs": kwargs, "output": output_path,
                          "texts": [s.get("text") for s in segments]})
            Path(output_path).write_bytes(b"fake mp4 content" * 100)
            return output_path

        monkeypatch.setattr(video_mod, "create_video", recording_video)
        return calls

    # ---------- GET /api/segments ----------

    def test_get_segments_lists_all(self, client, monkeypatch):
        task_id, project_dir, data, _ = self._completed_task(client, monkeypatch)
        resp = client.get(f"/api/segments/{task_id}")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["task_id"] == task_id
        assert body["project_dir"] == str(project_dir)
        segments = body["segments"]
        assert len(segments) == 2
        assert [s["index"] for s in segments] == [0, 1]
        assert segments[0]["text"].startswith("这是测试故事稿的第一段")
        assert segments[0]["audio_path"].endswith("segment_000.mp3")
        assert segments[0]["duration"] == 2.0
        # script 模式不调 LLM，配图关键词退化为主题兜底（story.fill_fallback_keywords）
        assert segments[0]["image_keyword"] == "改词主题"

    def test_get_segments_unknown_task_404(self, client):
        assert client.get("/api/segments/nope").status_code == 404

    def test_get_segments_rejected_before_video_generated(self, client):
        resp = client.post("/api/generate", json={"topic": "没出片的主题", "auto": False})
        task_id = resp.get_json()["task_id"]
        wait_status(client, task_id, "awaiting_confirmation")
        resp = client.get(f"/api/segments/{task_id}")
        assert resp.status_code == 400
        assert "暂无分段信息" in resp.get_json()["error"]

    # ---------- POST /api/segment/edit：参数与状态闸 ----------

    def test_edit_rejects_bad_inputs(self, client, monkeypatch):
        task_id, _, _, _ = self._completed_task(client, monkeypatch)

        # 缺 task_id / index 非整数 / 布尔 / 越界 / 文本为空或缺失
        assert client.post("/api/segment/edit", json={"index": 0, "text": "x"}).status_code == 400
        assert client.post("/api/segment/edit",
                           json={"task_id": "nope", "index": 0, "text": "x"}).status_code == 404
        assert client.post("/api/segment/edit",
                           json={"task_id": task_id, "index": "0", "text": "x"}).status_code == 400
        assert client.post("/api/segment/edit",
                           json={"task_id": task_id, "index": True, "text": "x"}).status_code == 400
        resp = client.post("/api/segment/edit",
                           json={"task_id": task_id, "index": 5, "text": "x"})
        assert resp.status_code == 404
        assert "超出范围" in resp.get_json()["error"]
        assert client.post("/api/segment/edit",
                           json={"task_id": task_id, "index": 0, "text": "   "}).status_code == 400
        assert client.post("/api/segment/edit",
                           json={"task_id": task_id, "index": 0}).status_code == 400

    def test_edit_rejects_running_task(self, client, monkeypatch):
        task_id, _, _, _ = self._completed_task(client, monkeypatch)
        webapp._tasks[task_id]["status"] = "running"
        resp = client.post("/api/segment/edit",
                           json={"task_id": task_id, "index": 0, "text": "新词"})
        assert resp.status_code == 400
        assert "正在处理中" in resp.get_json()["error"]

    # ---------- POST /api/segment/edit：只重跑被改段主流程 ----------

    def test_edit_reruns_only_edited_segment_and_updates_artifacts(
            self, client, monkeypatch):
        # 外挂 SRT 默认关闭（双层字幕根因）；本用例显式开启以验证改词后重建
        monkeypatch.setattr(config, "SUBTITLES_SRT", True)
        task_id, project_dir, _, state = self._completed_task(client, monkeypatch)
        calls = self._record_video(monkeypatch)

        # 前置：初次生成两个段的 mp3 都真实落盘
        assert state["generated"] == [0, 1]
        seg1_before = (project_dir / "segment_001.mp3").read_bytes()
        mp4_before = (project_dir / "改词主题.mp4").stat().st_mtime_ns

        new_text = "这是改过词的第一段，只重跑这一段。"
        resp = client.post("/api/segment/edit",
                           json={"task_id": task_id, "index": 0, "text": new_text})
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

        data = wait_status(client, task_id, "completed")
        assert "重新配音" in data["message"] or "重新合成" in data["message"]

        # 只重新 TTS 被改的第 0 段；第 1 段走 A12 断点续跑复用（不重写）
        assert state["last"] == [0]
        assert (project_dir / "segment_000.mp3").read_bytes() == f"audio::{new_text}".encode("utf-8")
        assert (project_dir / "segment_001.mp3").read_bytes() == seg1_before

        # 合成：reuse_frames=True 且只强制重渲第 0 段字幕帧
        assert len(calls) == 1
        kwargs = calls[0]["kwargs"]
        assert kwargs["reuse_frames"] is True
        assert kwargs["rerender_segments"] == {0}
        assert kwargs["frame_tag"] == ""
        # 传给合成的是更新后的段文本（第 0 段新词，第 1 段原词）
        assert calls[0]["texts"] == [new_text, STORY_TEXT.split("\n\n")[1]]

        # 成片重新落盘（内容更新）
        assert (project_dir / "改词主题.mp4").stat().st_mtime_ns > mp4_before

        # 字幕按新文稿重建
        srt = (project_dir / "subtitles.srt").read_text(encoding="utf-8")
        assert new_text in srt

        # segments.json 落盘同步：第 0 段新文本 + 新时长，第 1 段原样
        on_disk = segment_state.load_segments_state(str(project_dir))
        assert on_disk[0]["text"] == new_text
        assert on_disk[0]["duration"] == 2.0
        assert on_disk[0]["audio_path"] == str(project_dir / "segment_000.mp3")
        assert on_disk[1]["text"] == STORY_TEXT.split("\n\n")[1]
        assert on_disk[1]["duration"] == 1.0  # 复用段时长来自断点续跑替身

        # 内存任务清单同步：改段的文本/时长/无复用标记，其余段原样 + 复用标记
        segments = webapp._tasks[task_id]["segments"]
        assert segments[0]["text"] == new_text
        assert segments[0]["duration"] == 2.0
        assert not segments[0].get("audio_reused")
        assert segments[1]["text"] == STORY_TEXT.split("\n\n")[1]
        assert segments[1].get("audio_reused") is True
        # 任务级时长与产物字段刷新
        assert data["duration"] == 3.0
        assert data["output_file"] == "改词主题.mp4"

    def test_edit_uses_segments_json_so_second_edit_keeps_first(self, client, monkeypatch):
        """段状态以项目目录 segments.json 为事实源：连改两段，第一次的修改不被覆盖"""
        task_id, project_dir, _, state = self._completed_task(client, monkeypatch)
        self._record_video(monkeypatch)

        first_new = "第一段改成这样，字数足够。"
        assert client.post("/api/segment/edit",
                           json={"task_id": task_id, "index": 0,
                                 "text": first_new}).status_code == 200
        wait_status(client, task_id, "completed")

        second_new = "第二段也改一下，字数足够长。"
        assert client.post("/api/segment/edit",
                           json={"task_id": task_id, "index": 1,
                                 "text": second_new}).status_code == 200
        wait_status(client, task_id, "completed")

        on_disk = segment_state.load_segments_state(str(project_dir))
        assert on_disk[0]["text"] == first_new
        assert on_disk[1]["text"] == second_new
        # 第二次只重跑了第 1 段（第 0 段的 mp3 在第一次编辑后已存在 → 复用）
        assert state["last"] == [1]
        assert state["generated"] == [0, 1, 0, 1]

    def test_edit_multi_aspect_reruns_with_frame_tags(self, client, monkeypatch):
        task_id, project_dir, _, _ = self._completed_task_with_aspects(client, monkeypatch)
        calls = self._record_video(monkeypatch)

        resp = client.post("/api/segment/edit",
                           json={"task_id": task_id, "index": 1, "text": "竖屏也要改的段落。"})
        assert resp.status_code == 200
        data = wait_status(client, task_id, "completed")

        assert [c["kwargs"]["frame_tag"] for c in calls] == ["16x9", "9x16"]
        assert all(c["kwargs"]["reuse_frames"] is True for c in calls)
        assert all(c["kwargs"]["rerender_segments"] == {1} for c in calls)
        assert [o["aspect"] for o in data["outputs"]] == ["16:9", "9:16"]
        assert {o["file"] for o in data["outputs"]} == {"改词主题_16x9.mp4", "改词主题_9x16.mp4"}
        assert all((project_dir / o["file"]).exists() for o in data["outputs"])

    def _completed_task_with_aspects(self, client, monkeypatch):
        state = self._resume_speech(monkeypatch)
        resp = client.post("/api/generate",
                           json={"script": STORY_TEXT, "topic": "改词主题", "auto": False,
                                 "aspect_ratios": ["16:9", "9:16"]})
        assert resp.status_code == 200
        task_id = resp.get_json()["task_id"]
        data = wait_status(client, task_id, "completed")
        return task_id, Path(data["project_dir"]), data, state


class TestImageCandidatesAPI:

    """配图多候选接口（D4，R1 §五 P1-1）：GET /api/candidates 列出候选、
    POST /api/select 换图后仅重渲该段字幕帧并重新合成（flask test client，管线全 mock）"""

    ALT_COLORS = {1: (10, 200, 10), 2: (10, 10, 200), 3: (200, 200, 10)}

    # ---------- 任务与替身辅助 ----------

    def _fake_images_with_files(self, monkeypatch):
        """fetch_all_images 替身：往任务目录写真实 jpg 并回填 image_path，
        让候选列表/换位/断点续跑都作用在真实文件上"""
        import images as images_mod

        def fake_images(segments, image_source="", aspect_ratio="", project_dir=""):
            if project_dir:
                with open(os.path.join(project_dir, "img_marker.txt"), "w") as f:
                    f.write("ok")
            for i, s in enumerate(segments):
                path = os.path.join(project_dir, f"image_{i:03d}.jpg")
                Image.new("RGB", (64, 48), (60, 120, 180)).save(path, quality=90)
                s["image_path"] = path
            return segments

        monkeypatch.setattr(images_mod, "fetch_all_images", fake_images)

    def _completed_task(self, client, monkeypatch, body=None):
        self._fake_images_with_files(monkeypatch)
        payload = {"script": STORY_TEXT, "topic": "候选主题", "auto": False}
        payload.update(body or {})
        resp = client.post("/api/generate", json=payload)
        assert resp.status_code == 200
        task_id = resp.get_json()["task_id"]
        data = wait_status(client, task_id, "completed")
        return task_id, Path(data["project_dir"]), data

    def _add_alts(self, project_dir, seg_index, numbers):
        """在项目目录造真实可解码的候选图（MiniMax n>1 落盘产物的替身）"""
        made = {}
        for n in numbers:
            path = project_dir / f"image_{seg_index:03d}_alt_{n}.jpg"
            Image.new("RGB", (64, 48), self.ALT_COLORS[n % len(self.ALT_COLORS)]).save(path, quality=90)
            made[n] = path.read_bytes()
        return made

    def _record_video(self, monkeypatch):
        import video as video_mod

        calls = []

        def recording_video(segments, output_path, **kwargs):
            calls.append({
                "kwargs": kwargs,
                "output": output_path,
                "image_paths": [s.get("image_path") for s in segments],
            })
            Path(output_path).write_bytes(b"fake mp4 content" * 100)
            return output_path

        monkeypatch.setattr(video_mod, "create_video", recording_video)
        return calls

    # ---------- GET /api/candidates ----------

    def test_candidates_unknown_task_404(self, client):
        assert client.get("/api/candidates/nope/0").status_code == 404

    def test_candidates_rejected_before_video_generated(self, client):
        resp = client.post("/api/generate", json={"topic": "还没出片的主题", "auto": False})
        task_id = resp.get_json()["task_id"]
        wait_status(client, task_id, "awaiting_confirmation")
        resp = client.get(f"/api/candidates/{task_id}/0")
        assert resp.status_code == 400
        assert "暂无候选图" in resp.get_json()["error"]

    def test_candidates_seg_index_out_of_range(self, client, monkeypatch):
        task_id, project_dir, _ = self._completed_task(client, monkeypatch)
        assert client.get(f"/api/candidates/{task_id}/5").status_code == 404

    def test_candidates_lists_primary_first_then_alts_sorted(self, client, monkeypatch):
        task_id, project_dir, _ = self._completed_task(client, monkeypatch)
        self._add_alts(project_dir, 0, [2, 1])  # 乱序落盘，接口应按 N 升序

        resp = client.get(f"/api/candidates/{task_id}/0")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["seg_index"] == 0
        assert data["current"] == "image_000.jpg"
        files = [c["file"] for c in data["candidates"]]
        assert files == ["image_000.jpg", "image_000_alt_1.jpg", "image_000_alt_2.jpg"]
        assert [c["is_primary"] for c in data["candidates"]] == [True, False, False]
        assert [c["is_current"] for c in data["candidates"]] == [True, False, False]
        assert all(c["size"] > 0 for c in data["candidates"])
        # 其它段的候选不串台
        resp1 = client.get(f"/api/candidates/{task_id}/1")
        assert [c["file"] for c in resp1.get_json()["candidates"]] == ["image_001.jpg"]

    # ---------- POST /api/select：参数与状态闸 ----------

    def test_select_rejects_bad_inputs(self, client, monkeypatch):
        task_id, project_dir, _ = self._completed_task(client, monkeypatch)

        # 未知任务 / 非整数段号 / 带路径的文件名
        assert client.post("/api/select", json={"task_id": "nope", "seg_index": 0,
                                                "file": "image_000.jpg"}).status_code == 404
        assert client.post("/api/select", json={"task_id": task_id, "seg_index": "0",
                                                "file": "image_000.jpg"}).status_code == 400
        assert client.post("/api/select", json={"task_id": task_id, "seg_index": 9,
                                                "file": "image_000.jpg"}).status_code == 404
        assert client.post("/api/select", json={"task_id": task_id, "seg_index": 0,
                                                "file": "../image_000.jpg"}).status_code == 400

        # 候选不存在 / 跨段引用别的段的候选
        assert client.post("/api/select", json={"task_id": task_id, "seg_index": 0,
                                                "file": "image_000_alt_9.jpg"}).status_code == 400
        self._add_alts(project_dir, 1, [1])
        resp = client.post("/api/select", json={"task_id": task_id, "seg_index": 0,
                                                "file": "image_001_alt_1.jpg"})
        assert resp.status_code == 400
        assert "不属于" in resp.get_json()["error"]

    def test_select_rejects_running_task(self, client, monkeypatch):
        task_id, project_dir, _ = self._completed_task(client, monkeypatch)
        self._add_alts(project_dir, 0, [1])
        webapp._tasks[task_id]["status"] = "running"

        resp = client.post("/api/select", json={"task_id": task_id, "seg_index": 0,
                                                "file": "image_000_alt_1.jpg"})
        assert resp.status_code == 400
        assert "正在处理中" in resp.get_json()["error"]

    # ---------- POST /api/select：换位 + 局部重渲主流程 ----------

    def test_select_swaps_candidate_and_rerenders_selected_segment_only(
            self, client, monkeypatch):
        task_id, project_dir, _ = self._completed_task(client, monkeypatch)
        alts = self._add_alts(project_dir, 0, [1, 2])
        old_primary = (project_dir / "image_000.jpg").read_bytes()
        calls = self._record_video(monkeypatch)

        resp = client.post("/api/select", json={"task_id": task_id, "seg_index": 0,
                                                "file": "image_000_alt_1.jpg"})
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

        data = wait_status(client, task_id, "completed")
        assert "重新合成完成" in data["message"]

        # 换位：选中候选顶入主图位；原主图挪到空余 alt_3 槽位（可换回）
        assert (project_dir / "image_000.jpg").read_bytes() == alts[1]
        assert (project_dir / "image_000_alt_3.jpg").read_bytes() == old_primary
        assert not (project_dir / "image_000_alt_1.jpg").exists()

        # 局部重渲：reuse_frames=True 且只强制重渲第 0 段；单画幅无 frame_tag
        assert len(calls) == 1
        kwargs = calls[0]["kwargs"]
        assert kwargs["reuse_frames"] is True
        assert kwargs["rerender_segments"] == {0}
        assert kwargs["frame_tag"] == ""
        assert kwargs["width"] == 1920 and kwargs["height"] == 1080
        # 段清单同步：第 0 段底图指回主图位（换图后的内容），其余段不动
        segments = webapp._tasks[task_id]["segments"]
        assert segments[0]["image_path"].endswith("image_000.jpg")
        assert segments[1]["image_path"].endswith("image_001.jpg")
        # 成片重新落盘
        assert (project_dir / "候选主题.mp4").exists()

        # 换图后候选列表：主图位即当前选中，原主图出现在 alt_3
        resp = client.get(f"/api/candidates/{task_id}/0")
        listing = resp.get_json()
        assert listing["current"] == "image_000.jpg"
        files = [c["file"] for c in listing["candidates"]]
        assert files == ["image_000.jpg", "image_000_alt_2.jpg", "image_000_alt_3.jpg"]

    def test_select_multi_aspect_rerenders_with_frame_tags(self, client, monkeypatch):
        task_id, project_dir, gen_data = self._completed_task(
            client, monkeypatch, body={"aspect_ratios": ["16:9", "9:16"]})
        self._add_alts(project_dir, 0, [1])
        calls = self._record_video(monkeypatch)

        resp = client.post("/api/select", json={"task_id": task_id, "seg_index": 0,
                                                "file": "image_000_alt_1.jpg"})
        assert resp.status_code == 200
        data = wait_status(client, task_id, "completed")

        # 两个画幅各自重新合成：frame_tag 按画幅分开，rerender 都只针对第 0 段
        assert [c["kwargs"]["frame_tag"] for c in calls] == ["16x9", "9x16"]
        assert all(c["kwargs"]["reuse_frames"] is True for c in calls)
        assert all(c["kwargs"]["rerender_segments"] == {0} for c in calls)
        # 第二画幅用新主图重新派生裁切（image_000_9x16.jpg）
        assert calls[1]["image_paths"][0].endswith("image_000_9x16.jpg")
        assert [o["aspect"] for o in data["outputs"]] == ["16:9", "9:16"]
        assert {o["file"] for o in data["outputs"]} == {"候选主题_16x9.mp4", "候选主题_9x16.mp4"}
        assert all((project_dir / o["file"]).exists() for o in data["outputs"])
