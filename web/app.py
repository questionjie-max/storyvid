"""
视频自动生成 Web 界面（两阶段：先生成故事稿审核，再生成视频）
用法: python web/app.py
然后在浏览器打开 http://127.0.0.1:5000

画幅比例和音色按任务显式传参（不再改全局配置），多任务并发互不干扰。
"""
import os
import shutil
import sys
import uuid
import threading
from pathlib import Path

# 确保能 import 到项目根目录的模块
_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(_PROJECT_ROOT))

from flask import Flask, render_template, request, jsonify, send_from_directory

import config
import segment_state
from config import OUTPUT_DIR, TEMP_DIR, setup_project

app = Flask(__name__, template_folder="templates", static_folder="static")

# 任务状态存储（内存中，重启会丢失）；最多保留 50 条，防止无限增长
_tasks: dict = {}
_MAX_TASKS = 50

# 保护 _tasks / _worker_threads 的并发访问。Flask dev server 默认 threaded，
# /api/generate、/api/status 与工作线程会同时读写；用 RLock 是因为
# api_generate 需要在持锁状态下再调 _prune_tasks（它自己也取锁）。
_tasks_lock = threading.RLock()

# 后台工作线程登记（便于测试 join、未来做优雅退出）；引用会在 spawn 时清理
_worker_threads: list[threading.Thread] = []


def _spawn_worker(target, *args) -> threading.Thread:
    """启动后台工作线程并登记。daemon 线程不阻塞进程退出。"""
    with _tasks_lock:
        _worker_threads[:] = [t for t in _worker_threads if t.is_alive()]
        thread = threading.Thread(target=target, args=args, daemon=True)
        _worker_threads.append(thread)
    thread.start()
    return thread

# 终态任务可以被清理；排队中/运行中的任务绝不能清（工作线程还在往里写状态）
_TERMINAL_STATUSES = ("completed", "failed")


def _prune_tasks():
    """超过上限时只清理已结束的任务。

    旧实现按插入顺序无差别弹最旧的：正在运行的任务被弹掉后，
    工作线程写 `_tasks[task_id]["status"]` 会 KeyError，前端也查不到状态。
    全部处于活跃态时暂不清理（上限 50，不会无限增长）。

    并发安全（B 队检测）：遍历必须在锁内并基于 list() 快照，
    否则并发 /api/generate 插入新任务会触发
    RuntimeError("dictionary changed size during iteration")；
    整体再包一层兜底——清理只是维护动作，任何异常都不能外泄，
    否则 api_generate 在 _spawn_worker 之前中断，任务会永久卡在 queued。
    """
    try:
        with _tasks_lock:
            if len(_tasks) <= _MAX_TASKS:
                return
            finished = [
                tid for tid, t in list(_tasks.items()) if t.get("status") in _TERMINAL_STATUSES
            ]
            for tid in finished:
                if len(_tasks) <= _MAX_TASKS:
                    break
                _tasks.pop(tid, None)
    except Exception as e:
        print(f"[Tasks] 任务列表清理失败（忽略，不影响任务创建）: {e}")


def _normalize_aspects(raw) -> list[str]:
    """画幅参数归一化：接受 "16:9" / "16:9,9:16" / ["16:9","9:16"] 等形态，
    返回去重保序的有效画幅列表（只认 16:9 / 9:16，与 config.video_size 一致）。

    兼容旧客户端只传 aspect_ratio 单值；缺省或全部非法时回退 ["16:9"]（旧行为）。
    """
    if isinstance(raw, str):
        items = raw.split(",")
    elif isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    else:
        items = []
    seen: set[str] = set()
    aspects: list[str] = []
    for item in items:
        ratio = item.strip()
        if ratio in ("16:9", "9:16") and ratio not in seen:
            seen.add(ratio)
            aspects.append(ratio)
    return aspects or ["16:9"]


def _generate_story_task(task_id: str, topic: str, aspect_ratios: list[str], voice: str, extra: str,
                         user_bgm_path: str = "", user_cover_path: str = "",
                         media_dir: str = ""):
    """阶段一：搜索 + 审核 + 生成故事稿

    user_bgm_path / user_cover_path / media_dir：用户上传素材的绝对路径与本地
    素材目录（可为空），全自动模式下随任务透传给阶段二。
    """
    import story
    import research

    try:
        _tasks[task_id]["status"] = "running"

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
            _generate_video_task(task_id, topic, story_text, aspect_ratios, voice,
                                 user_bgm_path=user_bgm_path,
                                 user_cover_path=user_cover_path,
                                 media_dir=media_dir)
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


def _generate_video_task(task_id: str, topic: str, story_text: str, aspect_ratios,
                         voice: str, skip_llm_keywords: bool = False,
                         user_bgm_path: str = "", user_cover_path: str = "",
                         media_dir: str = ""):
    """阶段二：从已确认的故事稿继续生成视频。画幅/音色/项目目录只对本任务生效。

    aspect_ratios 支持多画幅批量（如 ["16:9","9:16"]，也兼容单个字符串，内部归一化）：
    分段/关键词/TTS 只做一次（音频与画幅无关，直接复用），配图只在首个画幅真实
    获取（API 侧原生构图），其余画幅用同一底图智能裁切派生（不重复调配图 API），
    字幕时间轴与画幅无关（一份 SRT 共用），最后按画幅各自合成 mp4 + 封面。

    project_dir 必须显式传给管线各步：setup_project 会改全局 PROJECT_DIR，
    并发任务若各自回读全局值，会把文件写进别的任务目录。

    skip_llm_keywords=True（文稿直出模式）：不调用 llm.chat 生成配图关键词，
    直接用主题兜底（story.fill_fallback_keywords）。

    user_bgm_path / user_cover_path：用户通过 /api/upload 上传的 BGM（mp3/wav）
    与封面底图（jpg/png）绝对路径；非空且存在时拷入本项目目录，
    管线（video.create_video / cover.generate_cover）优先使用已上传素材。

    media_dir：用户自备本地素材目录（web 表单「本地素材目录」），语音生成后
    把其中的图片/视频优先分配给各段（图片拷入项目目录、视频记 video_path），
    未覆盖的段照旧走云端配图降级链。
    """
    import story
    import tts
    import images
    import subtitles
    import video
    import cover

    try:
        aspects = _normalize_aspects(aspect_ratios)
        multi = len(aspects) > 1
        # 多画幅时后半程进度按画幅均分；单画幅保持旧刻度（65/85/100）
        slot = (100 - 50) / len(aspects) if multi else 0

        _tasks[task_id]["status"] = "running"
        _tasks[task_id]["message"] = "开始生成视频..."
        _tasks[task_id]["progress"] = 35

        # 创建项目目录
        project_dir = setup_project(topic)
        _tasks[task_id]["project_dir"] = project_dir

        # 用户上传素材拷入本项目目录（uploads/ 里的原件保持不动，可跨任务复用）：
        # 拷贝失败不中断生成——只回退管线默认行为（合成垫乐 / 渐变封面）
        user_bgm = user_cover = ""
        try:
            if user_bgm_path and os.path.isfile(user_bgm_path):
                dst = os.path.join(project_dir, "bgm_user"
                                   + os.path.splitext(user_bgm_path)[1].lower())
                shutil.copyfile(user_bgm_path, dst)
                user_bgm = dst
                _tasks[task_id]["user_bgm"] = os.path.basename(dst)
            if user_cover_path and os.path.isfile(user_cover_path):
                dst = os.path.join(project_dir, "cover_base"
                                   + os.path.splitext(user_cover_path)[1].lower())
                shutil.copyfile(user_cover_path, dst)
                user_cover = dst
                _tasks[task_id]["user_cover"] = os.path.basename(dst)
        except Exception as e:
            print(f"[Web] 用户素材拷贝失败，回退默认 BGM/封面: {e}")
            user_bgm = user_cover = ""

        # 保存故事文稿
        story_path = os.path.join(project_dir, "story.txt")
        with open(story_path, "w", encoding="utf-8") as f:
            f.write(story_text)

        segments = story.split_story_into_segments(story_text)
        if skip_llm_keywords:
            # 文稿直出：不调 llm.chat，配图关键词用主题兜底
            segments = story.fill_fallback_keywords(segments, topic)
        else:
            segments = story.generate_image_keywords(segments, topic)
        _tasks[task_id]["progress"] = 40
        _tasks[task_id]["message"] = f"共 {len(segments)} 段，正在生成语音..."

        # 语音（音色只对本任务生效；音频与画幅无关——多画幅只生成一次，后续复用）
        segments = tts.generate_all_speech(segments, voice=voice, project_dir=project_dir)
        total_duration = sum(s.get("duration", 0) for s in segments)

        # 本地素材优先：图片拷入项目目录主图位（后续 fetch_all_images 按断点
        # 续跑语义直接复用、不再调云端），视频记 video_path 由合成层截取；
        # 分配失败只告警，未覆盖的段照旧走云端配图降级链
        if media_dir:
            assigned = images.assign_local_media(segments, media_dir, project_dir=project_dir)
            if assigned:
                _tasks[task_id]["message"] = (
                    f"语音生成完成（{total_duration:.0f}秒），"
                    f"已使用本地素材覆盖 {assigned}/{len(segments)} 段...")

        if multi:
            _tasks[task_id]["progress"] = 50
            _tasks[task_id]["message"] = (
                f"语音生成完成（{total_duration:.0f}秒），"
                f"开始批量渲染 {len(aspects)} 个画幅...")
        else:
            _tasks[task_id]["progress"] = 65
            _tasks[task_id]["message"] = f"语音生成完成（{total_duration:.0f}秒），正在配图..."

        # 字幕（时间轴只由音频时长决定，与画幅无关——一份 SRT 全画幅共用）。
        # 外挂 SRT 默认关闭：字幕已烧录进画面，同目录 SRT 会被播放器自动加载、
        # 与烧录字幕叠成两层（见 config.SUBTITLES_SRT）
        if config.SUBTITLES_SRT:
            srt_path = os.path.join(project_dir, "subtitles.srt")
            # 片头卡会把正片整体后移，段间交叉淡化会让后一段提前开始——
            # 字幕时间轴同步偏移 + 段间重叠才对得上
            subtitles.generate_srt(segments, srt_path, offset=video.intro_offset(),
                                   crossfade=video.crossfade_seconds())

        safe_name = "".join(c if c.isalnum() or c in "_- " else "_" for c in topic)
        stem = safe_name[:30].strip()
        # 宽高来自 config.video_size()：全局 PREVIEW 开（环境变量 PREVIEW=1）时，
        # 这里拿到的就是预览分辨率（横 960×540 / 竖 540×960）。宽高显式传入
        # create_video 会跳过 video.py 回退分支的预览提示，所以 Web 端必须在
        # 任务状态里自行标注，避免用户静默拿到预览片。
        if config.PREVIEW:
            _tasks[task_id]["preview"] = True

        outputs = []
        for i, aspect in enumerate(aspects):
            width, height = config.video_size(aspect)
            tag = aspect.replace(":", "x")
            if multi:
                _tasks[task_id]["progress"] = int(50 + slot * i)
                _tasks[task_id]["message"] = (
                    f"[{i+1}/{len(aspects)}] 正在渲染 {aspect}（{width}×{height}）...")
            elif config.PREVIEW:
                _tasks[task_id]["message"] = (
                    f"预览模式: 以 {width}×{height} 快速渲染，正在合成视频...")
                print(f"[Web] 预览模式: 任务 {task_id} 以 {width}×{height} 快速渲染"
                      f"（PREVIEW=1，正式出片请关闭）")

            if i == 0:
                # 首个画幅：配图真实获取（API 侧按该画幅返回原生构图）
                segs = [dict(s) for s in segments]
                segs = images.fetch_all_images(segs, aspect_ratio=aspect,
                                               project_dir=project_dir)
                base_segments = [dict(s) for s in segs]  # 底图快照，后续画幅裁切复用
                if not multi:
                    _tasks[task_id]["progress"] = 85
                    _tasks[task_id]["message"] = "配图完成，正在合成视频..."
            else:
                # 其余画幅：同一底图智能裁切，不重复调配图 API（多画幅批量渲染设计取舍）
                segs = images.derive_crops_for_aspect(
                    [dict(s) for s in base_segments], aspect, project_dir=project_dir)

            output_name = stem + (f"_{tag}" if multi else "") + ".mp4"
            output_path = os.path.join(project_dir, output_name)
            # bgm_path：用户上传 BGM 优先，空值时 create_video 内部走合成垫乐旧路径。
            # frame_tag：多画幅时缓存字幕帧按画幅分开命名（frame_XXX_16x9_00.jpg），
            # 防止后一画幅覆盖前一画幅的帧，/api/select 换图重合成的缓存复用才可靠
            video.create_video(segs, output_path, width=width, height=height,
                               project_dir=project_dir, topic=topic, bgm_path=user_bgm,
                               frame_tag=tag if multi else "")

            # 封面（画幅与项目目录只对本任务生效；失败不影响已合成的视频）
            # base_image：用户上传封面底图优先，空值/读取失败时回退渐变+配图混合
            cover_path = os.path.join(project_dir, f"cover_{tag}.jpg" if multi
                                      else "cover.jpg")
            cover_rel = ""
            try:
                cover.generate_cover(segs, cover_path, topic=topic,
                                     total_duration=total_duration,
                                     width=width, height=height, project_dir=project_dir,
                                     base_image=user_cover)
                cover_rel = os.path.relpath(cover_path, OUTPUT_DIR)
            except Exception as e:
                print(f"[Cover] 封面生成失败（不影响视频）: {e}")

            outputs.append({
                "aspect": aspect,
                "file": output_name,
                "rel_path": os.path.relpath(output_path, OUTPUT_DIR),
                "cover_rel_path": cover_rel,
                # 换图重合成（/api/select）按当年渲染的实际尺寸出片，
                # 不受 PREVIEW 开关在两次任务之间被改动的影响
                "width": width,
                "height": height,
            })

        _tasks[task_id]["status"] = "completed"
        _tasks[task_id]["progress"] = 100
        done_msg = ("批量渲染完成！" if multi else "视频生成完成！")
        if _tasks[task_id].get("preview"):
            done_msg += "（预览分辨率，PREVIEW=1；正式出片请关闭后重跑）"
        _tasks[task_id]["message"] = done_msg
        # outputs 为全画幅产物列表；output_file/output_rel_path/cover_rel_path
        # 兼容旧前端/旧测试（单画幅时与旧行为完全一致，多画幅时指向首个画幅）
        _tasks[task_id]["outputs"] = outputs
        _tasks[task_id]["output_file"] = outputs[0]["file"]
        _tasks[task_id]["output_rel_path"] = outputs[0]["rel_path"]
        if outputs[0]["cover_rel_path"]:
            _tasks[task_id]["cover_rel_path"] = outputs[0]["cover_rel_path"]
        _tasks[task_id]["duration"] = total_duration

        # 段级清单（换图局部重渲 D4 用）：记录每段的文本/时长/音频/配图关键词/
        # 底图路径（base_segments = 配图真实获取后的快照，多画幅时不含派生裁切），
        # /api/select 换图后据此只重渲该段字幕帧并重新合成，不重跑 TTS/配图。
        # 只存 JSON 可序列化的标量（路径/文本/数字），随 /api/status 一并可见。
        _tasks[task_id]["segments"] = [
            {
                "index": i,
                "text": s.get("text", ""),
                "duration": float(s.get("duration") or 0.0),
                "audio_path": s.get("audio_path") or "",
                "image_path": s.get("image_path") or "",
                "image_keyword": s.get("image_keyword", ""),
                "words": s.get("words") or [],
            }
            for i, s in enumerate(base_segments)
        ]
        # 段级状态落盘（D6）：segments.json 记录各段文本/配音/配图/时长，
        # /api/segment/edit 改词重跑以它为段状态事实源；落盘失败不阻塞任务
        segment_state.save_segments_state(base_segments, project_dir)
        _tasks[task_id]["video_stem"] = stem
        _tasks[task_id]["bgm_used"] = user_bgm
        _tasks[task_id]["cover_base_used"] = user_cover

    except Exception as e:
        _tasks[task_id]["status"] = "failed"
        _tasks[task_id]["message"] = f"生成失败: {str(e)}"
        _tasks[task_id]["error"] = str(e)
        import traceback
        _tasks[task_id]["traceback"] = traceback.format_exc()


# ---------- 换图局部重渲（D4：/api/select 的后台工作线程） ----------

def _reselect_task(task_id: str, seg_index: int, candidate_filename: str):
    """换图局部重渲：把候选图换入主图位后，仅重渲该段字幕帧并按画幅重新合成。

    全程不调任何配图/TTS API——候选图与音频都是上一轮已落盘的成品，正是
    A12 断点续跑「已花钱的调用不重复」约定的延伸；project_dir 显式传给
    derive_crops_for_aspect / create_video（沿用多任务并发不串目录的约定）。

    video.create_video(reuse_frames=True, rerender_segments={seg_index})：
    选中段强制重渲字幕帧（画面已随新配图变化），其余段复用上一轮的缓存帧
    （切块布局快照一致 + 文件可解码才复用，见 video._build_segment_clip）。
    多画幅任务：首个画幅用新主图，其余画幅先用新主图重新派生裁切再合成，
    缓存帧按画幅后缀分开命名互不污染。
    封面按新配图重出（失败仅告警，不影响成片）。
    """
    import images
    import video
    import cover

    try:
        with _tasks_lock:
            task = _tasks.get(task_id)
            if task is None:
                print(f"[Web] 换图重合成中止：任务 {task_id} 已被清理")
                return
            segments_meta = [dict(s) for s in task.get("segments") or []]
            project_dir = task.get("project_dir") or ""
            aspects = _normalize_aspects(task.get("aspect_ratios")
                                         or task.get("aspect_ratio") or "16:9")
            multi = len(aspects) > 1
            stem = task.get("video_stem") or "video"
            topic = task.get("topic", "")
            bgm_path = task.get("bgm_used", "")
            cover_base = task.get("cover_base_used", "")
            stored_outputs = {o.get("aspect"): o
                              for o in (task.get("outputs") or []) if o.get("aspect")}

        _tasks[task_id]["status"] = "running"
        _tasks[task_id]["progress"] = 90
        _tasks[task_id]["message"] = (
            f"第 {seg_index + 1} 段换用候选图 {candidate_filename}，"
            f"正在重渲该段字幕帧并重新合成...")

        # 1) 候选换入主图位（原主图挪到空余 alt 槽位，断点续跑复用 image_XXX.jpg
        #    时自然继承人工选择）；选定当前主图则原样返回（幂等重渲）
        primary_path = images.swap_image_candidate(project_dir, seg_index, candidate_filename)

        # 2) 更新段清单的底图路径（主图位路径固定，内容已换）
        segments_meta[seg_index]["image_path"] = primary_path

        total_duration = sum(float(s.get("duration") or 0.0) for s in segments_meta)

        outputs = []
        for i, aspect in enumerate(aspects):
            stored = stored_outputs.get(aspect) or {}
            width = int(stored.get("width") or 0)
            height = int(stored.get("height") or 0)
            if width <= 0 or height <= 0:
                width, height = config.video_size(aspect)
            tag = aspect.replace(":", "x")

            if i == 0:
                segs = [dict(s) for s in segments_meta]
            else:
                # 其余画幅：新主图重新派生裁切（derive_crops 每次全量重算，
                # 纯 PIL 离线操作，不调 API）
                segs = images.derive_crops_for_aspect(
                    [dict(s) for s in segments_meta], aspect, project_dir=project_dir)

            output_name = stem + (f"_{tag}" if multi else "") + ".mp4"
            output_path = os.path.join(project_dir, output_name)
            video.create_video(segs, output_path, width=width, height=height,
                               project_dir=project_dir, topic=topic, bgm_path=bgm_path,
                               reuse_frames=True, rerender_segments={seg_index},
                               frame_tag=tag if multi else "")

            cover_path = os.path.join(project_dir, f"cover_{tag}.jpg" if multi
                                      else "cover.jpg")
            cover_rel = ""
            try:
                cover.generate_cover(segs, cover_path, topic=topic,
                                     total_duration=total_duration,
                                     width=width, height=height, project_dir=project_dir,
                                     base_image=cover_base)
                cover_rel = os.path.relpath(cover_path, OUTPUT_DIR)
            except Exception as e:
                print(f"[Cover] 换图后封面重出失败（不影响视频）: {e}")

            outputs.append({
                "aspect": aspect,
                "file": output_name,
                "rel_path": os.path.relpath(output_path, OUTPUT_DIR),
                "cover_rel_path": cover_rel,
                "width": width,
                "height": height,
            })

        _tasks[task_id]["segments"] = segments_meta
        # 段状态落盘同步（D6）：换图不改文本/配音，但主图位内容变了，
        # segments.json 的 image_path 语义（当前主图）要保持最新
        segment_state.save_segments_state(segments_meta, project_dir)
        _tasks[task_id]["outputs"] = outputs
        _tasks[task_id]["output_file"] = outputs[0]["file"]
        _tasks[task_id]["output_rel_path"] = outputs[0]["rel_path"]
        if outputs[0]["cover_rel_path"]:
            _tasks[task_id]["cover_rel_path"] = outputs[0]["cover_rel_path"]
        _tasks[task_id]["status"] = "completed"
        _tasks[task_id]["progress"] = 100
        _tasks[task_id]["message"] = (
            f"第 {seg_index + 1} 段已换用 {candidate_filename}，重新合成完成！")

    except Exception as e:
        import traceback
        with _tasks_lock:
            task = _tasks.get(task_id)
        if task is None:
            print(f"[Web] 换图重合成失败但任务 {task_id} 已被清理，状态无处可写: {e}")
            return
        task["status"] = "failed"
        task["message"] = f"换图重合成失败: {str(e)}"
        task["error"] = str(e)
        task["traceback"] = traceback.format_exc()


# ---------- 用户素材上传（BGM / 封面底图 / 文稿） ----------
# 类型白名单：键为 upload type，值为允许的扩展名（统一小写比较），白名单之外一律拒绝
_UPLOAD_TYPES: dict[str, set[str]] = {
    "bgm": {".mp3", ".wav"},
    "cover": {".jpg", ".jpeg", ".png"},
    "script": {".txt"},
}
# 单文件上限 20MB；app.config["MAX_CONTENT_LENGTH"] 是整个请求体的兜底闸（超出直接 413）
_MAX_UPLOAD_BYTES = 20 * 1024 * 1024
_UPLOAD_CHUNK = 1024 * 1024  # 流式落盘的读取块大小（1MB）
# 用户上传 BGM 在 BGM_VOLUME=0（全局默认关）时的混音音量：上传即代表想用，不能静默忽略
_USER_BGM_FALLBACK_VOLUME = 0.25

app.config["MAX_CONTENT_LENGTH"] = _MAX_UPLOAD_BYTES + 1024 * 1024  # 21MB


def _upload_root() -> str:
    """上传素材根目录：全局临时目录下 uploads/。

    刻意不用 config.TEMP_DIR——setup_project 会把它指到某个任务的项目 _temp 里，
    上传发生在任务创建之前，必须落在与具体任务无关的全局临时目录。
    """
    return os.path.join(config.GLOBAL_TEMP_DIR, "uploads")


def _sanitize_filename(name: str) -> str:
    """清洗客户端文件名：防路径穿越、防非法字符、防超长。

    - 统一分隔符后只取最后一段（../../evil.mp3 -> evil.mp3），再过一遍
      os.path.basename 双保险
    - 非（字母/数字/._- 空格）字符替换为 _（中文等 Unicode 字母保留），
      首尾的 . 与空格剥掉：防 ".."、隐藏文件与空名；清洗后为空回退 "upload"
    - 超过 120 字符截断（保留扩展名），避免文件系统层报错
    """
    raw = str(name or "").replace("\\", "/")
    raw = raw.split("/")[-1]
    raw = os.path.basename(raw)
    cleaned = "".join(c if (c.isalnum() or c in "._- ") else "_" for c in raw)
    cleaned = cleaned.strip(". ").strip()
    if not cleaned:
        return "upload"
    if len(cleaned) > 120:
        stem, ext = os.path.splitext(cleaned)
        cleaned = stem[:max(1, 120 - len(ext))] + ext
    return cleaned


def _store_upload(file_storage, upload_type: str) -> tuple[str, str, int]:
    """把上传文件流式落盘到 uploads/<type>/，返回 (stored_name, 清洗后文件名, 字节数)。

    - 扩展名不在白名单立即拒绝（统一小写比较）
    - 边读边计数，累计超过 20MB 删除半成品并抛 ValueError，不把大文件写满磁盘
    - stored_name = <uuid8>_<清洗后文件名>：不含路径分隔符（防穿越）且全局唯一（防同名覆盖）
    """
    safe_name = _sanitize_filename(file_storage.filename)
    ext = os.path.splitext(safe_name)[1].lower()
    allowed = _UPLOAD_TYPES[upload_type]
    if ext not in allowed:
        raise ValueError(
            f"不支持的文件类型: {ext or '(无扩展名)'}，允许: {', '.join(sorted(allowed))}")
    target_dir = os.path.join(_upload_root(), upload_type)
    os.makedirs(target_dir, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex[:8]}_{safe_name}"
    target_path = os.path.join(target_dir, stored_name)
    size = 0
    try:
        with open(target_path, "wb") as out:
            while True:
                chunk = file_storage.stream.read(_UPLOAD_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > _MAX_UPLOAD_BYTES:
                    raise ValueError(f"文件超过 {_MAX_UPLOAD_BYTES // (1024 * 1024)}MB 上限")
                out.write(chunk)
    except Exception:
        try:
            os.remove(target_path)
        except OSError:
            pass
        raise
    if size == 0:
        try:
            os.remove(target_path)
        except OSError:
            pass
        raise ValueError("空文件")
    return stored_name, safe_name, size


def _resolve_upload(upload_id: str, upload_type: str) -> str:
    """把 /api/generate 回传的 upload_id 解析回上传目录内的真实文件路径。

    安全约束：upload_id 必须与落盘时的 stored_name 完全一致（再清洗会变样的一律
    视为伪造），realpath 后必须仍位于 uploads/<type>/ 内且真实存在；
    任何一条不满足返回 ""，由调用方决定 400。
    """
    if not upload_id or _sanitize_filename(upload_id) != upload_id:
        return ""
    base = os.path.realpath(_upload_root())
    path = os.path.realpath(os.path.join(base, upload_type, upload_id))
    if path != base and not path.startswith(base + os.sep):
        return ""
    if not os.path.isfile(path):
        return ""
    return path


@app.route("/api/upload", methods=["POST"])
def api_upload():
    """接收用户素材上传：multipart 表单字段 type=bgm|cover|script + file=<文件>。

    合法时返回 {"success", "type", "upload_id", "filename", "size"}；
    upload_id 由前端随 /api/generate 的 bgm_upload_id / cover_upload_id /
    script_upload_id 字段回传，生成任务优先使用已上传的 BGM/封面/文稿。
    """
    upload_type = (request.form.get("type") or "").strip().lower()
    if upload_type not in _UPLOAD_TYPES:
        return jsonify({"error": f"type 必须是 {' / '.join(_UPLOAD_TYPES)} 之一"}), 400
    file = request.files.get("file")
    if file is None or not file.filename:
        return jsonify({"error": "缺少上传文件（multipart 文件字段名须为 file）"}), 400
    try:
        stored_name, safe_name, size = _store_upload(file, upload_type)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({
        "success": True,
        "type": upload_type,
        "upload_id": stored_name,
        "filename": safe_name,
        "size": size,
    })


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/generate", methods=["POST"])
def api_generate():
    data = request.get_json() or {}
    topic = str(data.get("topic") or "").strip()
    script = str(data.get("script") or "").strip()

    # 上传素材联动（/api/upload 返回的 upload_id 回传到这里的 *_upload_id 字段）。
    # 校验失败一律 400 拒绝——伪造/过期的 upload_id 不能静默降级成"无素材"任务。
    user_bgm_path = user_cover_path = ""
    script_upload_id = str(data.get("script_upload_id") or "").strip()
    if script_upload_id:
        path = _resolve_upload(script_upload_id, "script")
        if not path:
            return jsonify({"error": "文稿上传不存在或已失效，请重新上传"}), 400
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            script = f.read().strip()  # 上传文稿优先于 JSON script 字段
    bgm_upload_id = str(data.get("bgm_upload_id") or "").strip()
    if bgm_upload_id:
        user_bgm_path = _resolve_upload(bgm_upload_id, "bgm")
        if not user_bgm_path:
            return jsonify({"error": "BGM 上传不存在或已失效，请重新上传"}), 400
    cover_upload_id = str(data.get("cover_upload_id") or "").strip()
    if cover_upload_id:
        user_cover_path = _resolve_upload(cover_upload_id, "cover")
        if not user_cover_path:
            return jsonify({"error": "封面底图上传不存在或已失效，请重新上传"}), 400

    # 本地素材目录：服务器本机路径（web 与服务同机部署，填路径比批量上传方便）。
    # 用户显式输入的路径不存在时 400 拒绝——静默忽略会让用户以为素材用上了
    media_dir = str(data.get("media_dir") or "").strip()
    if media_dir and not os.path.isdir(media_dir):
        return jsonify({"error": f"本地素材目录不存在: {media_dir}"}), 400

    # script 与 topic 二选一。给了 script 就是「文稿直出」：
    # 用户自带故事稿，跳过搜索/审核/LLM 写稿，直接生成视频（story 不再走确认）。
    script_mode = bool(script)
    if not topic and not script_mode:
        return jsonify({"error": "主题与文稿至少提供一个"}), 400

    if script_mode and not topic:
        from story import derive_topic_from_script
        topic = derive_topic_from_script(script)

    task_id = str(uuid.uuid4())[:8]
    # 画幅多选：aspect_ratios 数组（新）优先，兼容旧单值字段 aspect_ratio；
    # 内部归一化为去重保序的有效画幅列表（缺省/非法回退 ["16:9"]，与旧行为一致）
    aspect_ratios = _normalize_aspects(
        data.get("aspect_ratios") if data.get("aspect_ratios") is not None
        else data.get("aspect_ratio", "16:9"))
    voice = data.get("voice", "cixingnansheng")
    extra = data.get("extra", "")
    auto = data.get("auto", False)

    # 插入与清理在同一临界区内完成；_spawn_worker 紧随其后。
    # _prune_tasks 自身吞异常，保证任务创建后线程一定会被启动，
    # 不会出现「任务已建但线程未起、永久卡在 queued」的情况。
    with _tasks_lock:
        _tasks[task_id] = {
            "id": task_id,
            "status": "queued",
            "progress": 0,
            "message": "排队中...",
            "topic": topic,
            "aspect_ratio": aspect_ratios[0],  # 兼容旧前端/状态展示：主画幅
            "aspect_ratios": aspect_ratios,
            "voice": voice,
            "extra": extra,
            "auto": auto,
            "user_bgm_path": user_bgm_path,
            "user_cover_path": user_cover_path,
            "media_dir": media_dir,
        }
        if script_mode:
            _tasks[task_id]["script_mode"] = True
            _tasks[task_id]["story"] = script
            _tasks[task_id]["story_preview"] = script[:500]
        _prune_tasks()

    if script_mode:
        _spawn_worker(_generate_video_task, task_id, topic, script,
                      aspect_ratios, voice, True, user_bgm_path, user_cover_path,
                      media_dir)
    else:
        _spawn_worker(_generate_story_task, task_id, topic, aspect_ratios, voice, extra,
                      user_bgm_path, user_cover_path, media_dir)

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

    _spawn_worker(_generate_video_task, task_id, task["topic"], story_text,
                  task.get("aspect_ratios") or task["aspect_ratio"], task["voice"],
                  False, task.get("user_bgm_path", ""), task.get("user_cover_path", ""),
                  task.get("media_dir", ""))

    return jsonify({"success": True})


@app.route("/api/regenerate/<task_id>", methods=["POST"])
def api_regenerate(task_id):
    """重新生成故事稿"""
    task = _tasks.get(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404

    # 文稿直出任务的稿子是用户自带的，重新生成故事稿等于偷偷改用户的稿
    if task.get("script_mode"):
        return jsonify({"error": "文稿直出任务使用用户自带故事稿，不支持重新生成故事稿"}), 400

    # 排队中/生成中的任务不允许重开线程，否则两个线程同时写同一个 task 的状态和目录
    if task.get("status") in ("queued", "running"):
        return jsonify({"error": "任务正在处理中，暂不能重新生成"}), 400

    # 重置状态重新生成故事稿
    task["status"] = "queued"
    task["progress"] = 0
    task["message"] = "重新生成故事稿..."
    task.pop("story", None)
    task.pop("story_preview", None)
    task.pop("project_dir", None)

    _spawn_worker(_generate_story_task, task_id, task["topic"],
                  task.get("aspect_ratios") or task["aspect_ratio"],
                  task["voice"], task.get("extra", ""),
                  task.get("user_bgm_path", ""), task.get("user_cover_path", ""),
                  task.get("media_dir", ""))

    return jsonify({"success": True})


# ---------- 配图多候选（D4）：列出候选 + 换图局部重渲 ----------

def _get_selectable_task(task_id: str,
                         not_ready_msg: str = "任务尚未完成视频生成，暂无候选图"):
    """取出可局部重操作的任务：必须存在且已带着段级清单（= 视频生成已完成）。

    返回 (task, None) 或 (None, (响应, 状态码))，供 /api/candidates 与
    /api/select 共用；D6 的 /api/segments、/api/segment/edit 传自定义
    not_ready_msg（提示语从「候选图」换成「分段信息」），行为不变。
    """
    task = _tasks.get(task_id)
    if not task:
        return None, (jsonify({"error": "任务不存在"}), 404)
    if not task.get("segments"):
        return None, (jsonify({"error": not_ready_msg}), 400)
    return task, None


@app.route("/api/candidates/<task_id>/<int:seg_index>")
def api_candidates(task_id, seg_index):
    """列出某任务某段的候选配图：主图 image_XXX.jpg + 候选 image_XXX_alt_N.jpg。

    候选文件由 MiniMax n>1 生图落盘（config.MINIMAX_IMAGE_N，images.py）；
    current 为该段当前实际使用的图（= 主图位内容，换图后即选中候选）。
    """
    import images

    task, err = _get_selectable_task(task_id)
    if err:
        return err
    segments_meta = task["segments"]
    if seg_index < 0 or seg_index >= len(segments_meta):
        return jsonify({"error": f"段索引超出范围（共 {len(segments_meta)} 段）"}), 404
    project_dir = task.get("project_dir") or ""
    if not project_dir or not os.path.isdir(project_dir):
        return jsonify({"error": "项目目录不存在"}), 404

    names = images.list_candidates(project_dir, seg_index)
    current = os.path.basename(segments_meta[seg_index].get("image_path") or "") or None
    candidates = []
    for name in names:
        path = os.path.join(project_dir, name)
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        candidates.append({
            "file": name,
            "is_primary": name == f"image_{seg_index:03d}.jpg",
            "is_current": name == current,
            "size": size,
        })
    return jsonify({
        "task_id": task_id,
        "seg_index": seg_index,
        "current": current,
        "candidates": candidates,
    })


@app.route("/api/select", methods=["POST"])
def api_select():
    """选定候选图并触发局部重渲：JSON {"task_id","seg_index","file"}。

    仅接受该段名下的 image_XXX.jpg / image_XXX_alt_N.jpg（防路径穿越/跨段
    引用）；任务处于排队/运行中时拒绝（与 regenerate 同一并发闸）。换图在
    后台线程执行（_reselect_task）：候选换入主图位 → 仅重渲该段字幕帧 →
    按画幅重新合成，进度经 /api/status 查看。
    """
    import images

    data = request.get_json() or {}
    task_id = str(data.get("task_id") or "").strip()
    candidate = str(data.get("file") or "").strip()
    seg_index = data.get("seg_index")

    if not task_id:
        return jsonify({"error": "缺少 task_id"}), 400
    if not isinstance(seg_index, int) or isinstance(seg_index, bool):
        return jsonify({"error": "seg_index 必须是整数"}), 400
    if not candidate or os.path.basename(candidate) != candidate:
        return jsonify({"error": "file 必须是候选图文件名（不含路径）"}), 400

    task, err = _get_selectable_task(task_id)
    if err:
        return err
    segments_meta = task["segments"]
    if seg_index < 0 or seg_index >= len(segments_meta):
        return jsonify({"error": f"段索引超出范围（共 {len(segments_meta)} 段）"}), 404
    if task.get("status") in ("queued", "running"):
        return jsonify({"error": "任务正在处理中，暂不能换图"}), 400

    project_dir = task.get("project_dir") or ""
    if candidate not in images.list_candidates(project_dir, seg_index):
        return jsonify({"error": f"候选图不存在或不属于第 {seg_index + 1} 段: {candidate}"}), 400

    # 检查与置位在同一临界区：两个并发 select 不能同时开出重渲线程
    with _tasks_lock:
        task = _tasks.get(task_id)
        if task is None:
            return jsonify({"error": "任务不存在"}), 404
        if task.get("status") in ("queued", "running"):
            return jsonify({"error": "任务正在处理中，暂不能换图"}), 400
        task["status"] = "running"
        task["progress"] = 90
        task["message"] = f"第 {seg_index + 1} 段换用候选图 {candidate}，排队重渲..."

    _spawn_worker(_reselect_task, task_id, seg_index, candidate)
    return jsonify({"success": True, "task_id": task_id,
                    "seg_index": seg_index, "file": candidate})


# ---------- 分段编辑局部重跑（D6，R1 §五 P1-3 / §一#8） ----------
# 旧行为：改任何一段的词都要整条管线重跑（重新搜索/写稿/TTS/配图/合成）。
# 现在：项目目录 segments.json 记录各段状态，改词只重新 TTS 该段——
# 其余段的 segment_XXX.mp3 已在项目目录，generate_all_speech 的 A12 断点
# 续跑语义直接复用（不重复花 TTS 的钱）；合成走 D4 的 reuse_frames，
# 只有被改段的字幕帧重画，其余段复用缓存帧。

def _segment_edit_task(task_id: str, seg_index: int, new_text: str):
    """改词重跑后台线程：只重新 TTS 被改段，其余段复用已有音频后局部重合成。

    流程（与 _reselect_task 同一工程约定：project_dir/画幅/输出名等全部从
    任务状态显式取，不回读全局配置，多任务并发互不串目录）：
      1) 段状态优先从项目目录 segments.json 载入（落盘事实源），缺失时回退
         内存任务清单；
      2) 更新该段文本；删除旧配音 segment_XXX.mp3 并清掉旧词级时间戳——
         这是「只重跑该段」的开关：A12 断点续跑按「mp3 已存在且非空」复用，
         被删的段才会真正重新合成（TTS 未配 Key/失败的段走既有兜底）；
      3) tts.generate_all_speech 全量跑一遍：其余段全部命中复用路径；
      4) 字幕按新时长重建 → 各画幅 reuse_frames=True + 只强制重渲该段
         字幕帧重新合成 → 封面重出（失败仅告警）；
      5) segments.json 落盘 + 任务状态按 D4 同一套字段更新。

    已知取舍：若某段当初 TTS 失败（无 mp3），编辑任一段时该段也会被
    generate_all_speech 顺带重试一次——这正是 A12 断点续跑对「缺 mp3 段」
    的既定语义（缺就补），不为此绕过复用判断。
    """
    import images
    import tts
    import subtitles
    import video
    import cover

    try:
        with _tasks_lock:
            task = _tasks.get(task_id)
            if task is None:
                print(f"[Web] 分段编辑中止：任务 {task_id} 已被清理")
                return
            project_dir = task.get("project_dir") or ""
            aspects = _normalize_aspects(task.get("aspect_ratios")
                                         or task.get("aspect_ratio") or "16:9")
            multi = len(aspects) > 1
            stem = task.get("video_stem") or "video"
            topic = task.get("topic", "")
            voice = task.get("voice", "")
            bgm_path = task.get("bgm_used", "")
            cover_base = task.get("cover_base_used", "")
            stored_outputs = {o.get("aspect"): o
                              for o in (task.get("outputs") or []) if o.get("aspect")}

        if not project_dir or not os.path.isdir(project_dir):
            raise RuntimeError(f"项目目录不存在，无法局部重跑: {project_dir}")

        # 1) 段状态：优先 segments.json（服务重启/内存清理后磁盘仍在），
        #    没有（旧任务/D6 之前生成）回退内存任务清单
        segments_meta = segment_state.load_segments_state(project_dir)
        if not segments_meta:
            with _tasks_lock:
                task = _tasks.get(task_id)
                segments_meta = ([dict(s) for s in (task.get("segments") or [])]
                                 if task else [])
        if not segments_meta or seg_index >= len(segments_meta):
            raise RuntimeError("分段状态不可用（segments.json 缺失且内存无清单）")

        _tasks[task_id]["status"] = "running"
        _tasks[task_id]["progress"] = 40
        _tasks[task_id]["message"] = (
            f"第 {seg_index + 1} 段文稿已修改，正在重新配音（其余段复用已有音频）...")

        # 2) 只动被改的段：换文本、删旧配音、作废旧词级时间戳
        seg = segments_meta[seg_index]
        seg["text"] = new_text
        old_audio = seg.get("audio_path") or ""
        if old_audio and os.path.isfile(old_audio):
            try:
                os.remove(old_audio)
            except OSError as e:
                print(f"[Web] 旧配音删除失败（将原样复用旧音频）: {e}")
        seg["audio_path"] = ""
        seg["duration"] = 0.0
        seg.pop("words", None)
        seg.pop("words_source", None)
        seg.pop("audio_reused", None)

        # 3) A12 断点续跑局部重合成：其余段 mp3 已存在直接复用，只有被改段
        #    真正重新 TTS（时间戳/对齐兜底在 generate_all_speech 内部照常走）
        segments_meta = tts.generate_all_speech(segments_meta, voice=voice,
                                                project_dir=project_dir)
        total_duration = sum(float(s.get("duration") or 0.0) for s in segments_meta)

        # 4) 字幕按新时长重建（时间轴只由音频时长决定，与画幅无关，一份共用）。
        # 外挂 SRT 默认关闭（同目录 SRT 会被播放器自动加载成两层字幕，
        # 见 config.SUBTITLES_SRT）；烧录字幕不受影响，始终随新音频重渲
        if config.SUBTITLES_SRT:
            srt_path = os.path.join(project_dir, "subtitles.srt")
            subtitles.generate_srt(segments_meta, srt_path, offset=video.intro_offset(),
                                   crossfade=video.crossfade_seconds())

        _tasks[task_id]["progress"] = 85
        _tasks[task_id]["message"] = f"第 {seg_index + 1} 段配音已更新，正在重新合成..."

        outputs = []
        for i, aspect in enumerate(aspects):
            stored = stored_outputs.get(aspect) or {}
            width = int(stored.get("width") or 0)
            height = int(stored.get("height") or 0)
            if width <= 0 or height <= 0:
                width, height = config.video_size(aspect)
            tag = aspect.replace(":", "x")

            if i == 0:
                segs = [dict(s) for s in segments_meta]
            else:
                # 其余画幅：同一底图重新派生裁切（纯 PIL 离线，不调 API）
                segs = images.derive_crops_for_aspect(
                    [dict(s) for s in segments_meta], aspect, project_dir=project_dir)

            output_name = stem + (f"_{tag}" if multi else "") + ".mp4"
            output_path = os.path.join(project_dir, output_name)
            # reuse_frames + rerender_segments={seg_index}：只有被改段重画字幕帧，
            # 其余段复用上一轮缓存帧（文本未变 → 切块布局快照一致才可复用）
            video.create_video(segs, output_path, width=width, height=height,
                               project_dir=project_dir, topic=topic, bgm_path=bgm_path,
                               reuse_frames=True, rerender_segments={seg_index},
                               frame_tag=tag if multi else "")

            cover_path = os.path.join(project_dir, f"cover_{tag}.jpg" if multi
                                      else "cover.jpg")
            cover_rel = ""
            try:
                cover.generate_cover(segs, cover_path, topic=topic,
                                     total_duration=total_duration,
                                     width=width, height=height, project_dir=project_dir,
                                     base_image=cover_base)
                cover_rel = os.path.relpath(cover_path, OUTPUT_DIR)
            except Exception as e:
                print(f"[Cover] 改词重跑后封面重出失败（不影响视频）: {e}")

            outputs.append({
                "aspect": aspect,
                "file": output_name,
                "rel_path": os.path.relpath(output_path, OUTPUT_DIR),
                "cover_rel_path": cover_rel,
                "width": width,
                "height": height,
            })

        # 5) 段状态落盘 + 内存任务更新（与 D4 换图同一套字段）
        segment_state.save_segments_state(segments_meta, project_dir)
        _tasks[task_id]["segments"] = segments_meta
        _tasks[task_id]["outputs"] = outputs
        _tasks[task_id]["output_file"] = outputs[0]["file"]
        _tasks[task_id]["output_rel_path"] = outputs[0]["rel_path"]
        if outputs[0]["cover_rel_path"]:
            _tasks[task_id]["cover_rel_path"] = outputs[0]["cover_rel_path"]
        _tasks[task_id]["duration"] = total_duration
        _tasks[task_id]["status"] = "completed"
        _tasks[task_id]["progress"] = 100
        _tasks[task_id]["message"] = (
            f"第 {seg_index + 1} 段已按新文稿重新配音并重新合成完成！")

    except Exception as e:
        import traceback
        with _tasks_lock:
            task = _tasks.get(task_id)
        if task is None:
            print(f"[Web] 分段编辑失败但任务 {task_id} 已被清理，状态无处可写: {e}")
            return
        task["status"] = "failed"
        task["message"] = f"改词重跑失败: {str(e)}"
        task["error"] = str(e)
        task["traceback"] = traceback.format_exc()


@app.route("/api/segments/<task_id>")
def api_segments(task_id):
    """列出某任务的分段状态（D6）：文本/时长/配音/配图关键词，供前端
    分段列表与「改词重跑」入口展示。字段口径与 /api/status 的 segments
    一致（words 词级时间戳一并返回，前端不消费但便于调试）。"""
    task, err = _get_selectable_task(task_id, "任务尚未完成视频生成，暂无分段信息")
    if err:
        return err
    return jsonify({
        "task_id": task_id,
        "project_dir": task.get("project_dir") or "",
        "segments": task["segments"],
    })


@app.route("/api/segment/edit", methods=["POST"])
def api_segment_edit():
    """改词重跑（D6）：JSON {"task_id","index","text"} → 只重新 TTS 该段 →
    复用 A12 断点续跑完成局部重合成。

    校验与并发闸与 /api/select 同构：index 必须是整数且在段范围内、text
    非空；排队中/运行中的任务拒绝（防止两个线程同时写同一任务状态与目录）。
    实际工作在后台线程 _segment_edit_task 执行，进度经 /api/status 查看。
    """
    data = request.get_json() or {}
    task_id = str(data.get("task_id") or "").strip()
    seg_index = data.get("index")
    text = data.get("text")

    if not task_id:
        return jsonify({"error": "缺少 task_id"}), 400
    if not isinstance(seg_index, int) or isinstance(seg_index, bool):
        return jsonify({"error": "index 必须是整数"}), 400
    if not isinstance(text, str) or not text.strip():
        return jsonify({"error": "text 必须是非空字符串"}), 400
    text = text.strip()

    task, err = _get_selectable_task(task_id, "任务尚未完成视频生成，暂无分段信息")
    if err:
        return err
    segments_meta = task["segments"]
    if seg_index < 0 or seg_index >= len(segments_meta):
        return jsonify({"error": f"段索引超出范围（共 {len(segments_meta)} 段）"}), 404

    # 检查与置位在同一临界区：编辑期间不能同时再开出换图/编辑线程
    with _tasks_lock:
        task = _tasks.get(task_id)
        if task is None:
            return jsonify({"error": "任务不存在"}), 404
        if task.get("status") in ("queued", "running"):
            return jsonify({"error": "任务正在处理中，暂不能编辑分段"}), 400
        task["status"] = "running"
        task["progress"] = 40
        task["message"] = f"第 {seg_index + 1} 段文稿已修改，排队重新配音..."

    _spawn_worker(_segment_edit_task, task_id, seg_index, text)
    return jsonify({"success": True, "task_id": task_id,
                    "index": seg_index, "text": text})


@app.route("/api/status/<task_id>")
def api_status(task_id):
    task = _tasks.get(task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    # 不暴露完整 traceback 给前端
    # list() 快照：工作线程可能正在给同一 task 插入新键（story/project_dir 等），
    # 裸遍历会和写入撞 "dictionary changed size during iteration"
    resp = {k: v for k, v in list(task.items()) if k != "traceback"}
    return jsonify(resp)


@app.route("/api/download/<path:filename>")
def api_download(filename):
    """支持从项目子目录下载文件"""
    # 安全检查：防止目录遍历
    safe_path = os.path.normpath(filename)
    if safe_path.startswith("..") or os.path.isabs(safe_path):
        return jsonify({"error": "非法路径"}), 403
    return send_from_directory(OUTPUT_DIR, safe_path, as_attachment=True)


if __name__ == "__main__":
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(TEMP_DIR, exist_ok=True)
    print(f"工作流目录: {_PROJECT_ROOT}")
    print(f"输出目录: {OUTPUT_DIR}")
    print(f"启动服务: http://{config.WEB_HOST}:{config.WEB_PORT}")
    app.run(host=config.WEB_HOST, port=config.WEB_PORT, debug=False)
