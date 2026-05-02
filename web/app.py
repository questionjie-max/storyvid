"""
视频自动生成 Web 界面（两阶段：先生成故事稿审核，再生成视频）
用法: python web/app.py
然后在浏览器打开 http://localhost:5000
"""
import os
import sys
import uuid
import threading
from pathlib import Path

# 确保能 import 到项目根目录的模块
_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(_PROJECT_ROOT))

from flask import Flask, render_template, request, jsonify, send_from_directory

from config import OUTPUT_DIR, TEMP_DIR, setup_project, get_project_dir

app = Flask(__name__, template_folder="templates", static_folder="static")

# 任务状态存储（内存中，重启会丢失）
_tasks = {}


def _apply_config_override(aspect_ratio: str, voice: str):
    """临时覆盖视频和语音配置"""
    import config
    config.ASPECT_RATIO = aspect_ratio
    config.TTS_VOICE = voice
    if aspect_ratio == "9:16":
        config.VIDEO_WIDTH = 1080
        config.VIDEO_HEIGHT = 1920
    else:
        config.VIDEO_WIDTH = 1920
        config.VIDEO_HEIGHT = 1080


def _generate_story_task(task_id: str, topic: str, aspect_ratio: str, voice: str, extra: str):
    """阶段一：搜索 + 审核 + 生成故事稿"""
    import story
    import config
    import research

    try:
        _tasks[task_id]["status"] = "running"

        _apply_config_override(aspect_ratio, voice)

        # Step 0: 搜索 + 审核
        _tasks[task_id]["progress"] = 5
        _tasks[task_id]["message"] = "正在搜索相关资料..."
        search_results = research.search_topic(topic)

        _tasks[task_id]["progress"] = 15
        _tasks[task_id]["message"] = "正在进行内容安全审核..."
        analysis = research.analyze_content(topic, search_results)

        if not analysis.get("is_safe", True):
            _tasks[task_id]["status"] = "failed"
            _tasks[task_id]["message"] = f"内容审核未通过: {analysis.get('reason')}"
            _tasks[task_id]["error"] = analysis.get("suggestions", "")
            return

        # Step 1: 生成故事稿
        _tasks[task_id]["progress"] = 25
        _tasks[task_id]["message"] = "正在生成故事稿..."

        if search_results:
            story_text = research.generate_story_with_research(topic, search_results, extra)
            review = story.self_review_story(story_text, topic)
            if not review.get("passed"):
                _tasks[task_id]["message"] = "故事稿自我校验中..."
                story_text = research.generate_story_with_research(topic, search_results, extra)
        else:
            story_text = story.generate_story_with_retry(topic, extra)

        _tasks[task_id]["story"] = story_text
        _tasks[task_id]["story_preview"] = story_text[:500]

        # 全自动模式：跳过确认，直接生成视频
        if _tasks[task_id].get("auto"):
            _tasks[task_id]["progress"] = 32
            _tasks[task_id]["message"] = "全自动模式，直接生成视频..."
            _generate_video_task(task_id, topic, story_text, aspect_ratio, voice)
        else:
            _tasks[task_id]["status"] = "awaiting_confirmation"
            _tasks[task_id]["progress"] = 30
            _tasks[task_id]["message"] = "故事稿已生成，等待确认..."

    except Exception as e:
        _tasks[task_id]["status"] = "failed"
        _tasks[task_id]["message"] = f"生成失败: {str(e)}"
        _tasks[task_id]["error"] = str(e)
        import traceback
        _tasks[task_id]["traceback"] = traceback.format_exc()


def _generate_video_task(task_id: str, topic: str, story_text: str, aspect_ratio: str, voice: str):
    """阶段二：从已确认的故事稿继续生成视频"""
    import story
    import tts
    import images
    import subtitles
    import video

    try:
        _tasks[task_id]["status"] = "running"
        _tasks[task_id]["message"] = "开始生成视频..."
        _tasks[task_id]["progress"] = 35

        _apply_config_override(aspect_ratio, voice)

        # 创建项目目录
        project_dir = setup_project(topic)
        _tasks[task_id]["project_dir"] = project_dir

        # 保存故事文稿
        story_path = os.path.join(project_dir, "story.txt")
        with open(story_path, "w", encoding="utf-8") as f:
            f.write(story_text)

        segments = story.split_story_into_segments(story_text)
        segments = story.generate_image_keywords(segments, topic)
        _tasks[task_id]["progress"] = 40
        _tasks[task_id]["message"] = f"共 {len(segments)} 段，正在生成语音..."

        # 语音
        segments = tts.generate_all_speech(segments)
        total_duration = sum(s.get("duration", 0) for s in segments)
        _tasks[task_id]["progress"] = 65
        _tasks[task_id]["message"] = f"语音生成完成（{total_duration:.0f}秒），正在配图..."

        # 配图
        segments = images.fetch_all_images(segments)
        _tasks[task_id]["progress"] = 85
        _tasks[task_id]["message"] = "配图完成，正在合成视频..."

        # 字幕 + 视频（全部直接放在项目目录）
        srt_path = os.path.join(get_project_dir(), "subtitles.srt")
        subtitles.generate_srt(segments, srt_path)

        safe_name = "".join(c if c.isalnum() or c in "_- " else "_" for c in topic)
        output_name = safe_name[:30].strip() + ".mp4"
        output_path = os.path.join(get_project_dir(), output_name)
        video.create_video(segments, output_path)

        # 计算相对于 OUTPUT_DIR 的路径，用于下载
        rel_path = os.path.relpath(output_path, OUTPUT_DIR)

        _tasks[task_id]["status"] = "completed"
        _tasks[task_id]["progress"] = 100
        _tasks[task_id]["message"] = "视频生成完成！"
        _tasks[task_id]["output_file"] = output_name
        _tasks[task_id]["output_rel_path"] = rel_path
        _tasks[task_id]["duration"] = total_duration

    except Exception as e:
        _tasks[task_id]["status"] = "failed"
        _tasks[task_id]["message"] = f"生成失败: {str(e)}"
        _tasks[task_id]["error"] = str(e)
        import traceback
        _tasks[task_id]["traceback"] = traceback.format_exc()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/generate", methods=["POST"])
def api_generate():
    data = request.get_json() or {}
    topic = data.get("topic", "").strip()
    if not topic:
        return jsonify({"error": "主题不能为空"}), 400

    task_id = str(uuid.uuid4())[:8]
    aspect_ratio = data.get("aspect_ratio", "16:9")
    voice = data.get("voice", "cixingnansheng")
    extra = data.get("extra", "")
    auto = data.get("auto", False)

    _tasks[task_id] = {
        "id": task_id,
        "status": "queued",
        "progress": 0,
        "message": "排队中...",
        "topic": topic,
        "aspect_ratio": aspect_ratio,
        "voice": voice,
        "extra": extra,
        "auto": auto,
    }

    thread = threading.Thread(
        target=_generate_story_task,
        args=(task_id, topic, aspect_ratio, voice, extra),
        daemon=True,
    )
    thread.start()

    return jsonify({"task_id": task_id})


@app.route("/api/confirm/<task_id>", methods=["POST"])
def api_confirm(task_id):
    task = _tasks.get(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404

    if task.get("status") != "awaiting_confirmation":
        return jsonify({"error": "任务不处于待确认状态"}), 400

    story_text = task.get("story", "")
    if not story_text:
        return jsonify({"error": "故事稿不存在"}), 400

    task["status"] = "queued"
    task["progress"] = 32
    task["message"] = "已确认，开始生成视频..."

    thread = threading.Thread(
        target=_generate_video_task,
        args=(task_id, task["topic"], story_text, task["aspect_ratio"], task["voice"]),
        daemon=True,
    )
    thread.start()

    return jsonify({"success": True})


@app.route("/api/regenerate/<task_id>", methods=["POST"])
def api_regenerate(task_id):
    """重新生成故事稿"""
    task = _tasks.get(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404

    # 重置状态重新生成故事稿
    task["status"] = "queued"
    task["progress"] = 0
    task["message"] = "重新生成故事稿..."
    task.pop("story", None)
    task.pop("story_preview", None)
    task.pop("project_dir", None)

    thread = threading.Thread(
        target=_generate_story_task,
        args=(task_id, task["topic"], task["aspect_ratio"], task["voice"], task.get("extra", "")),
        daemon=True,
    )
    thread.start()

    return jsonify({"success": True})


@app.route("/api/status/<task_id>")
def api_status(task_id):
    task = _tasks.get(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    # 不暴露完整 traceback 给前端
    resp = {k: v for k, v in task.items() if k != "traceback"}
    return jsonify(resp)


@app.route("/api/download/<path:filename>")
def api_download(filename):
    """支持从项目子目录下载文件"""
    # 安全检查：防止目录遍历
    safe_path = os.path.normpath(filename)
    if safe_path.startswith("..") or safe_path.startswith("/"):
        return jsonify({"error": "非法路径"}), 403
    return send_from_directory(OUTPUT_DIR, safe_path, as_attachment=True)


if __name__ == "__main__":
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(TEMP_DIR, exist_ok=True)
    print(f"工作流目录: {_PROJECT_ROOT}")
    print(f"输出目录: {OUTPUT_DIR}")
    print("启动服务: http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
