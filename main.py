#!/usr/bin/env python3
"""
自动生成视频工作流
用法: python main.py "视频主题" [--extra "额外要求"] [--output 输出文件名]
文稿直出: python main.py [主题] --script-file 故事稿.txt [--output 输出文件名]
  （跳过搜索/审核/LLM 写稿，直接读 txt 作为故事稿走 分段→TTS→配图→字幕→合成；
    主题可省略，省略时用文稿第一个非空行；该模式不需要 LLM_API_KEY）
演示模式: python main.py --demo [--output 输出文件名]
  （零 Key 全离线出片：内置示例文稿，跳过搜索/审核/LLM，TTS 用每段 3 秒静音段，
    配图用 PIL 渐变大字占位图——没配 Key 的新用户半分钟看到成品效果；
    环境变量 DEMO=1 同效，可搭配 --script-file 换成自己的文稿）
批量画幅: 任意模式加 --batch-aspect "16:9,9:16"
  （同一故事稿一次跑出横竖两版：TTS 只生成一次（音频与画幅无关），
    配图只在首个画幅真实获取、其余画幅用同一底图智能裁切，输出名带 _16x9/_9x16 后缀）

环境变量（或 .env 文件）:
  LLM_API_KEY=你的LLM API Key（DeepSeek/Kimi/OpenAI 等任意兼容 API）
  STEPFUN_API_KEY=你的阶跃星辰API Key（TTS_PROVIDER=stepfun 时必填）
  MINIMAX_API_KEY=你的MiniMax API Key（TTS_PROVIDER=minimax 或 AI 配图时用）
  VOLC_TTS_APPID/VOLC_TTS_TOKEN=火山引擎（豆包）TTS 凭证（TTS_PROVIDER=volc 时必填）
  UNSPLASH_ACCESS_KEY=你的Unsplash Access Key (可选，没有会用备用图)

详细配置见 .env.example
"""
import argparse
import os
import sys

# 确保当前目录在路径中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from research import search_topic, analyze_content, generate_story_with_research
from story import (
    generate_story_with_retry,
    self_review_story,
    split_story_into_segments,
    generate_image_keywords,
    fill_fallback_keywords,
    derive_topic_from_script,
)
from tts import generate_all_speech
from images import fetch_all_images, derive_crops_for_aspect, assign_local_media
from subtitles import generate_srt
from video import (
    cards_duration,
    create_video,
    crossfade_overlap_total,
    crossfade_seconds,
    intro_offset,
)
from cover import generate_cover
from demo import (
    DEMO_STORY,
    DEMO_TOPIC,
    fill_demo_keywords,
    fetch_demo_images,
    fill_demo_speech,
)
import config
from config import setup_project, validate_config
from segment_state import save_segments_state


def parse_batch_aspects(raw: str) -> list[str]:
    """解析 --batch-aspect "16:9,9:16" → 去重保序的有效画幅列表。

    只接受 16:9 / 9:16（config.video_size 只区分这两档）；非法项打印警告并跳过；
    全部非法或为空返回 []（调用方回退单画幅默认行为）。
    """
    if not raw or not raw.strip():
        return []
    seen: set[str] = set()
    aspects: list[str] = []
    for item in raw.split(","):
        ratio = item.strip()
        if not ratio:
            continue
        if ratio not in ("16:9", "9:16"):
            print(f"[Batch] 警告: 忽略不支持的画幅 {ratio!r}（只支持 16:9 / 9:16）")
            continue
        if ratio not in seen:
            seen.add(ratio)
            aspects.append(ratio)
    return aspects


def run_workflow(topic: str, extra: str = "", output_name: str = "", auto: bool = False,
                 batch_aspects: list[str] | None = None, media_dir: str = "") -> str:
    """运行完整工作流（含搜索、审核、自我校验），返回最终视频路径"""

    print(f"\n{'='*60}")
    print(f"主题: {topic}")
    if extra:
        print(f"额外要求: {extra}")
    print(f"{'='*60}\n")

    # Step 0: 搜索 + 内容审核
    print("[0/7] 搜索主题相关信息...")
    search_results = search_topic(topic)
    print(f"找到 {len(search_results)} 条搜索结果")

    print("[0/7] 内容安全审核...")
    analysis = analyze_content(topic, search_results)
    print(
        f"审核结果: 热门={analysis.get('is_hot')}, "
        f"安全={analysis.get('is_safe')}, 风险={analysis.get('risk_level')}"
    )

    if not analysis.get("is_safe", True):
        print(f"\n错误: 内容审核未通过 - {analysis.get('reason')}")
        print(f"建议: {analysis.get('suggestions')}")
        return ""

    # Step 1: 生成故事稿（基于搜索结果 + 自我校验）
    print("\n[1/7] 生成故事文字稿...")
    if search_results:
        story = generate_story_with_research(topic, search_results, extra)
        # 即使基于搜索结果生成，也做一次自我校验
        review = self_review_story(story, topic)
        if not review.get("passed"):
            print(f"[Story] 自我校验发现问题: {review.get('issues', [])}")
            print("[Story] 基于搜索结果重新生成...")
            story = generate_story_with_research(topic, search_results, extra)
    else:
        story = generate_story_with_retry(topic, extra)

    print(f"故事长度: {len(story)} 字\n")
    print("=" * 60)
    print("故事文稿（请审核确认）")
    print("=" * 60)
    print(story)
    print("=" * 60)
    print()

    if not auto:
        confirm = input("确认使用此文稿生成视频？(y/n): ").strip().lower()
        if confirm not in ("y", "yes", "是", "确认"):
            print("已取消生成。")
            return ""
        # 本地素材优先：制作前主动问一句「有没有自己的素材」（照片/视频目录），
        # 有就优先用，没有回车继续走网上配图
        if not media_dir:
            answer = input("本地素材目录（自己的照片/视频，回车跳过用网上配图）: ").strip()
            if answer:
                media_dir = answer
    else:
        print("（全自动模式，跳过文稿确认）")

    # Step 2 起的管线：分段→TTS→配图→字幕→合成→封面
    return _produce_video(story, topic, output_name, batch_aspects=batch_aspects,
                          media_dir=media_dir)


def run_workflow_from_script(script_file: str, topic: str = "", output_name: str = "",
                             batch_aspects: list[str] | None = None,
                             demo: bool = False, media_dir: str = "") -> str:
    """文稿直出模式：读取 txt 文件作为故事稿，跳过搜索/审核/LLM 写稿，
    直接走 分段→TTS→配图→字幕→合成（用户自己写稿也能出片）。

    - 全程不调用 llm.chat：配图关键词用主题兜底（fill_fallback_keywords），
      因此也不需要 LLM_API_KEY
    - 文稿就是用户自己写的，不再弹文稿确认（等价于 --auto）
    - 主题缺省时用文稿第一个非空行（derive_topic_from_script）
    - demo=True（--demo 搭配 --script-file）时连 TTS/配图 API 也跳过：
      每段 3 秒静音配音 + PIL 渐变大字占位图（见 demo.py）

    返回最终视频路径；文件不存在或为空时返回空字符串。
    """
    if not os.path.isfile(script_file):
        print(f"错误: 文稿文件不存在: {script_file}")
        return ""
    with open(script_file, "r", encoding="utf-8") as f:
        story = f.read().strip()
    if not story:
        print(f"错误: 文稿文件为空: {script_file}")
        return ""

    if not topic:
        topic = derive_topic_from_script(story)

    print(f"\n{'='*60}")
    print(f"文稿直出模式: {script_file}")
    print(f"主题: {topic}")
    print("（跳过搜索/审核/LLM 写稿，直接使用用户提供的故事稿）"
          + ("（演示模式：TTS/配图走离线占位）" if demo else ""))
    print(f"{'='*60}\n")

    return _produce_video(story, topic, output_name, use_llm_keywords=False,
                          batch_aspects=batch_aspects, demo=demo, media_dir=media_dir)


def run_demo_workflow(topic: str = "", output_name: str = "",
                      batch_aspects: list[str] | None = None) -> str:
    """演示模式：内置示例文稿，零 Key 全离线出片（--demo / DEMO=1）。

    跳过搜索/审核/LLM（连确认也不弹），TTS 换成每段固定 3 秒静音段，
    配图换成 PIL 渐变大字占位图——完整走 分段→配音→配图→字幕→合成→封面，
    让没配 Key 的新用户半分钟看到成品效果。主题可自定义（影响片头卡/封面/
    项目目录名），缺省用内置 DEMO_TOPIC。
    """
    print(f"\n{'='*60}")
    print(f"演示模式: 内置示例文稿，零 Key 全离线出片")
    print(f"主题: {topic or DEMO_TOPIC}")
    print("（跳过搜索/审核/LLM；TTS=每段 3 秒静音段；配图=PIL 渐变占位图）")
    print(f"{'='*60}\n")
    return _produce_video(DEMO_STORY, topic or DEMO_TOPIC, output_name,
                          use_llm_keywords=False, demo=True,
                          batch_aspects=batch_aspects)


def _produce_video(story: str, topic: str, output_name: str = "",
                   use_llm_keywords: bool = True,
                   batch_aspects: list[str] | None = None,
                   demo: bool = False, media_dir: str = "") -> "str | list[str]":
    """从已确定的故事稿走到成片：分段→配图关键词→TTS→配图→字幕→合成→封面。

    use_llm_keywords=False 时（文稿直出模式）不调用 llm.chat 生成配图关键词，
    直接用主题兜底（fill_fallback_keywords）。

    media_dir 非空（CLI --media-dir / 交互输入 / LOCAL_MEDIA_DIR）时本地素材
    优先：语音生成后把素材目录里的图片/视频分配给各段（images.assign_local_media），
    图片拷入项目目录主图位，未覆盖的段再走云端配图降级链；demo 模式保持
    全离线占位图语义，不消费本地素材。

    demo=True（--demo / DEMO=1）时整条管线零 Key 全离线：
    - 关键词用每段段首文案（demo.fill_demo_keywords，不调 LLM）；
    - TTS 换成每段固定 3 秒静音段（demo.fill_demo_speech，ffmpeg 离线合成）；
    - 配图换成 PIL 渐变大字占位图（demo.fetch_demo_images，不出网）；
    其余步骤（分段/字幕/合成/封面/批量画幅/断点续跑）与正式管线完全一致。

    batch_aspects 给定且多于一个画幅时走批量：同一故事稿一次跑出多画幅版本
    （如 --batch-aspect "16:9,9:16" 同时出横竖两版）。设计取舍：
    - TTS 只生成一次：配音时长/内容与画幅无关，各画幅直接复用同批 mp3，
      不重做（也省钱）；
    - 配图只在第一个画幅真实获取（API 侧按该画幅给原生构图），其余画幅用
      同一底图智能裁切派生（images.derive_crops_for_aspect），不再调配图 API
      ——省 N-1 倍配图花费（MiniMax 生图按张计费，与断点续跑的省钱哲学一致），
      且横竖版本画面内容一致；代价是跨画幅裁切会丢画面边缘（16:9 底图裁
      9:16 只保留约 31.6% 宽度，偏离中心的主体可能被裁掉）；
    - 字幕时间轴只由音频时长决定，与画幅无关——一份 subtitles.srt 全画幅共用；
    - 每个画幅各自合成 mp4 + 封面，输出名追加 _16x9 / _9x16 后缀。

    返回：单画幅时成片路径字符串（旧行为不变）；多画幅批量时返回路径列表。
    """
    # 创建项目目录
    project_dir = setup_project(topic)
    print(f"\n项目目录: {project_dir}")

    # 保存故事文稿
    story_path = os.path.join(project_dir, "story.txt")
    with open(story_path, "w", encoding="utf-8") as f:
        f.write(story)
    print(f"故事文稿已保存: {story_path}")

    # Step 2: 分段 + 生成配图关键词
    print("\n[2/7] 分段并生成配图关键词...")
    segments = split_story_into_segments(story)
    print(f"共 {len(segments)} 段\n")
    if demo:
        # 演示模式：不调 LLM，大字用每段段首文案（比统一主题更有分镜感）
        segments = fill_demo_keywords(segments, topic)
    elif use_llm_keywords:
        segments = generate_image_keywords(segments, topic)
    else:
        # 文稿直出：不调 LLM，配图关键词退化为主题（与 LLM 关键词不足时的兜底一致）
        segments = fill_fallback_keywords(segments, topic)

    # Step 3: 生成语音
    print("[3/7] 生成语音...")
    if demo:
        # 演示模式：跳过 TTS API，每段固定 3 秒静音段（离线 ffmpeg 合成）
        segments = fill_demo_speech(segments, project_dir=project_dir)
    else:
        segments = generate_all_speech(segments, project_dir=project_dir)
    # 断点续跑：已存在且非空的 segment_XXX.mp3 被 generate_all_speech 直接复用
    reused = sum(1 for s in segments if s.get("audio_reused"))
    if reused:
        print(f"检测到 {reused} 个已完成段，已复用")
    total_duration = sum(s.get("duration", 0) for s in segments)
    print(f"语音总时长: {total_duration:.1f} 秒\n")

    # 本地素材优先：图片拷入项目目录主图位（fetch_all_images 按断点续跑语义
    # 直接复用、不再调云端），视频记 video_path 由合成层截取；未覆盖的段
    # 照旧走云端配图降级链。CLI --media-dir / 交互输入优先，回退 LOCAL_MEDIA_DIR
    media_dir = (media_dir or config.LOCAL_MEDIA_DIR).strip()
    if media_dir and not demo:
        assigned = assign_local_media(segments, media_dir, project_dir=project_dir)
        if assigned:
            print(f"已使用本地素材覆盖 {assigned}/{len(segments)} 段\n")

    aspects = batch_aspects if batch_aspects else [config.ASPECT_RATIO]

    # ---------- 单画幅：保持旧行为（输出名不加画幅后缀） ----------
    if len(aspects) <= 1:
        return _finish_single_aspect(segments, topic, project_dir, output_name,
                                     total_duration, demo=demo)

    # ---------- 批量画幅：同一故事稿一次跑出多画幅版本 ----------
    print(f"\n[Batch] 批量画幅模式: {' + '.join(aspects)}"
          f"（TTS 只生成一次，配图只获取一次，其余画幅智能裁切复用同一底图）")

    # Step 4: 配图只在首个画幅真实获取（API 侧按该画幅返回原生构图）
    print("[4/7] 获取配图（首个画幅原生获取）...")
    if demo:
        # 演示模式：PIL 渐变大字占位图，不调任何配图 API
        segments = fetch_demo_images(segments, aspect_ratio=aspects[0],
                                     project_dir=project_dir)
    else:
        segments = fetch_all_images(segments, aspect_ratio=aspects[0], project_dir=project_dir)
    reused_images = sum(1 for s in segments if s.get("image_reused"))
    if reused_images:
        print(f"检测到 {reused_images} 张已完成配图，已复用")
    # 底图快照：其余画幅从这份底图智能裁切，不重复调用配图 API
    base_segments = [dict(s) for s in segments]

    # Step 5: 字幕（时间轴只由音频时长决定，与画幅无关——一份 SRT 全画幅共用）
    print("\n[5/7] 生成字幕...")
    srt_path = os.path.join(project_dir, "subtitles.srt")
    # 片头卡会把正片整体后移，段间交叉淡化会让后一段提前开始——
    # 字幕时间轴同步偏移 + 段间重叠才对得上
    generate_srt(segments, srt_path, offset=intro_offset(), crossfade=crossfade_seconds())
    print(f"字幕已保存: {srt_path}")

    # 输出名：多画幅时每个画幅追加 _16x9 / _9x16 后缀
    if not output_name:
        safe_name = "".join(c if c.isalnum() or c in "_- " else "_" for c in topic)
        output_name = safe_name[:30].strip() + ".mp4"
    if not output_name.endswith(".mp4"):
        output_name += ".mp4"
    stem = output_name[:-len(".mp4")]

    final_paths: list[str] = []
    for i, aspect in enumerate(aspects):
        width, height = config.video_size(aspect)  # 显式宽高：批量画幅不能跟随全局 ASPECT_RATIO
        tag = aspect.replace(":", "x")
        print(f"\n[6/7] 合成 {aspect}（{width}×{height}）..."
              + ("" if i == 0 else "（配图由首个画幅底图智能裁切）"))
        # 各画幅用独立段落副本，避免互相覆盖 image_path
        segs = ([dict(s) for s in segments] if i == 0
                else derive_crops_for_aspect([dict(s) for s in base_segments],
                                             aspect, project_dir=project_dir))
        out_path = os.path.join(project_dir, f"{stem}_{tag}.mp4")
        final_paths.append(create_video(segs, out_path, width=width, height=height,
                                        project_dir=project_dir, topic=topic))

        # 每个画幅各自出封面（失败不影响已合成的视频）
        print(f"[7/7] 生成 {aspect} 封面...")
        try:
            generate_cover(segs, os.path.join(project_dir, f"cover_{tag}.jpg"),
                           topic=topic, total_duration=total_duration,
                           width=width, height=height, project_dir=project_dir)
        except Exception as e:
            print(f"警告: {aspect} 封面生成失败（不影响视频）: {e}")

    print(f"\n{'='*60}")
    print("批量渲染完成！")
    print(f"项目路径: {project_dir}")
    for p in final_paths:
        print(f"视频路径: {p}")
    print(f"正片时长: {total_duration:.1f} 秒"
          f"（各画幅成片约 {total_duration + cards_duration() - crossfade_overlap_total([float(s.get('duration') or 0.0) for s in segments]):.1f} 秒，含片头片尾卡）")
    print(f"{'='*60}\n")

    # 段级状态落盘（D6）：segments.json 记录各段文本/配音/配图/时长，
    # 供 Web /api/segment/edit 等局部重合成能力以此为段状态事实源
    save_segments_state(segments, project_dir)

    return final_paths


def _finish_single_aspect(segments: list[dict], topic: str, project_dir: str,
                          output_name: str, total_duration: float,
                          demo: bool = False) -> str:
    """单画幅收尾：配图→字幕→合成→封面（原 _produce_video 的 Step 4-7，行为不变）"""
    # Step 4: 获取配图
    print("[4/7] 获取配图...")
    if demo:
        # 演示模式：PIL 渐变大字占位图，不调任何配图 API
        segments = fetch_demo_images(segments, project_dir=project_dir)
    else:
        segments = fetch_all_images(segments, project_dir=project_dir)
    reused_images = sum(1 for s in segments if s.get("image_reused"))
    if reused_images:
        print(f"检测到 {reused_images} 张已完成配图，已复用")

    # Step 5: 生成字幕
    print("\n[5/7] 生成字幕...")
    srt_path = os.path.join(project_dir, "subtitles.srt")
    # 片头卡会把正片整体后移，段间交叉淡化会让后一段提前开始——
    # 字幕时间轴同步偏移 + 段间重叠才对得上
    generate_srt(segments, srt_path, offset=intro_offset(), crossfade=crossfade_seconds())
    print(f"字幕已保存: {srt_path}")

    # Step 6: 合成视频
    print("\n[6/7] 合成视频...")
    if not output_name:
        safe_name = "".join(c if c.isalnum() or c in "_- " else "_" for c in topic)
        output_name = safe_name[:30].strip() + ".mp4"
    if not output_name.endswith(".mp4"):
        output_name += ".mp4"

    output_path = os.path.join(project_dir, output_name)
    final_path = create_video(segments, output_path, project_dir=project_dir, topic=topic)

    # Step 7: 生成封面（失败不影响已合成的视频）
    print("\n[7/7] 生成封面...")
    cover_path = ""
    try:
        cover_path = generate_cover(segments, os.path.join(project_dir, "cover.jpg"),
                                    topic=topic, total_duration=total_duration,
                                    project_dir=project_dir)
    except Exception as e:
        print(f"警告: 封面生成失败（不影响视频）: {e}")

    print(f"\n{'='*60}")
    print(f"视频生成完成！")
    print(f"项目路径: {project_dir}")
    print(f"视频路径: {final_path}")
    if cover_path:
        print(f"封面路径: {cover_path}")
    print(f"正片时长: {total_duration:.1f} 秒"
          f"（成片约 {total_duration + cards_duration() - crossfade_overlap_total([float(s.get('duration') or 0.0) for s in segments]):.1f} 秒，含片头片尾卡）")
    print(f"{'='*60}\n")

    # 段级状态落盘（D6）：segments.json 记录各段文本/配音/配图/时长，
    # 供 Web /api/segment/edit 等局部重合成能力以此为段状态事实源
    save_segments_state(segments, project_dir)

    return final_path


def main():
    parser = argparse.ArgumentParser(description="自动生成视频工作流")
    parser.add_argument("topic", nargs="?", default="",
                        help="视频主题（--script-file / --demo 模式下可省略）")
    parser.add_argument("--extra", "-e", default="", help="额外要求（如风格、长度等）")
    parser.add_argument("--output", "-o", default="", help="输出文件名")
    parser.add_argument("--auto", "-a", action="store_true", help="全自动模式，跳过文稿确认")
    parser.add_argument("--script-file", "-s", default="",
                        help="故事稿 txt 文件路径（文稿直出：跳过搜索/审核/LLM 写稿，"
                             "直接读该文件作为故事稿生成视频）")
    parser.add_argument("--demo", action="store_true",
                        help="演示模式：零 Key 全离线出片（环境变量 DEMO=1 同效）——"
                             "内置示例文稿（可搭配 --script-file 换成自己的），跳过"
                             "搜索/审核/LLM，TTS 用每段 3 秒静音段，配图用 PIL 渐变"
                             "大字占位图；默认按预览分辨率快速渲染（设 PREVIEW=0 用"
                             "全分辨率）")
    parser.add_argument("--batch-aspect", default="",
                        help='批量画幅：一次跑出多个画幅版本，逗号分隔，如 "16:9,9:16"。'
                             "同一故事稿 TTS 只生成一次（音频与画幅无关），配图只在首个"
                             "画幅真实获取、其余画幅用同一底图智能裁切（省配图 API 花费）")
    parser.add_argument("--media-dir", default="",
                        help="本地素材目录（用户自备照片/视频）：递归扫描其中的"
                             "图片与视频优先用作各段画面（视频自动截取到配音时长并静音），"
                             "未覆盖到的段再走网上配图。回车交互模式也会主动询问；"
                             "环境变量 LOCAL_MEDIA_DIR 可设全局默认")
    parser.add_argument("--preview", action="store_true",
                        help="预览模式：按 960×540（横）/ 540×960（竖）快速渲染，"
                             "不跑全分辨率（等价于环境变量 PREVIEW=1）")
    args = parser.parse_args()

    batch_aspects = parse_batch_aspects(args.batch_aspect)
    if args.batch_aspect and not batch_aspects:
        parser.error("--batch-aspect 未解析出有效画幅（只支持 16:9 / 9:16，逗号分隔）")

    # --demo 透传到全局演示开关：CLI 参数与环境变量 DEMO=1 同效（见 config.DEMO）。
    # 演示目标是「没配 Key 也半分钟看到成品」，故默认跟随 --preview 打开预览分辨率；
    # 显式设置了 PREVIEW 环境变量（如 PREVIEW=0）时尊重用户选择不再覆盖
    demo = bool(args.demo) or config.DEMO
    if demo:
        config.DEMO = True
        if "PREVIEW" not in os.environ:
            config.PREVIEW = True
            preview_w, preview_h = config.video_size(aspect_ratio=os.getenv("ASPECT_RATIO", "16:9"))
            print(f"演示模式: 以 {preview_w}×{preview_h} 快速渲染"
                  f"（设 PREVIEW=0 可用全分辨率出片）")

    # --preview 透传到全局预览开关：create_video/封面/兜底图都跟随 config.video_size()
    # 取预览分辨率；环境变量 PREVIEW=1 也能打开（见 config.PREVIEW）
    if args.preview:
        config.PREVIEW = True
        preview_w, preview_h = config.video_size(aspect_ratio=os.getenv("ASPECT_RATIO", "16:9"))
        print(f"预览模式: 以 {preview_w}×{preview_h} 快速渲染"
              f"（正式出片请去掉 --preview / 关闭 PREVIEW）")

    script_mode = bool(args.script_file)
    if not args.topic and not script_mode and not demo:
        parser.error("需要提供视频主题，或用 --script-file 指定故事稿文件")

    # 检查配置（按所选 TTS/配图提供商校验，而不是一刀切要求 StepFun）。
    # 文稿直出模式全程不调用 LLM，LLM_API_KEY 不再是必需项；
    # 演示模式全程离线不调任何 API，所有 Key 都不需要，跳过校验
    if demo:
        print("（演示模式跳过 API Key 校验：全程离线，不需要任何 Key）")
    else:
        ok, problems, warnings = validate_config(require_llm=not script_mode)
        for w in warnings:
            print(f"警告: {w}")
        if not ok:
            print("错误: 配置不完整，无法启动：")
            for p in problems:
                print(f"  - {p}")
            print("\n请在 .env 文件中补全配置（参考 .env.example），"
                  "或先跑 python main.py --demo 零 Key 体验成品效果")
            sys.exit(1)

    if script_mode:
        run_workflow_from_script(args.script_file, topic=args.topic, output_name=args.output,
                                 batch_aspects=batch_aspects, demo=demo,
                                 media_dir=args.media_dir)
    elif demo:
        run_demo_workflow(topic=args.topic, output_name=args.output,
                          batch_aspects=batch_aspects)
    else:
        run_workflow(args.topic, args.extra, args.output, args.auto,
                     batch_aspects=batch_aspects, media_dir=args.media_dir)


if __name__ == "__main__":
    main()
