#!/usr/bin/env python3
"""
自动生成视频工作流
用法: python main.py "视频主题" [--extra "额外要求"] [--output 输出文件名]

环境变量（或 .env 文件）:
  STEPFUN_API_KEY=你的阶跃星辰API Key
  UNSPLASH_ACCESS_KEY=你的Unsplash Access Key (可选，没有会用备用图)
"""
import argparse
import os
import sys

# 确保当前目录在路径中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from research import search_topic, analyze_content, generate_story_with_research
from story import generate_story_with_retry, split_story_into_segments, generate_image_keywords
from tts import generate_all_speech
from images import fetch_all_images
from subtitles import generate_srt
from video import create_video
from config import setup_project, get_project_dir


def run_workflow(topic: str, extra: str = "", output_name: str = "", auto: bool = False) -> str:
    """运行完整工作流（含搜索、审核、自我校验），返回最终视频路径"""

    print(f"\n{'='*60}")
    print(f"主题: {topic}")
    if extra:
        print(f"额外要求: {extra}")
    print(f"{'='*60}\n")

    # Step 0: 搜索 + 内容审核
    print("[0/6] 搜索主题相关信息...")
    search_results = search_topic(topic)
    print(f"找到 {len(search_results)} 条搜索结果")

    print("[0/6] 内容安全审核...")
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
    print("\n[1/6] 生成故事文字稿...")
    if search_results:
        story = generate_story_with_research(topic, search_results, extra)
        # 即使基于搜索结果生成，也做一次自我校验
        from story import self_review_story
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
    else:
        print("（全自动模式，跳过文稿确认）")

    # 创建项目目录
    project_dir = setup_project(topic)
    print(f"\n项目目录: {project_dir}")

    # 保存故事文稿
    story_path = os.path.join(project_dir, "story.txt")
    with open(story_path, "w", encoding="utf-8") as f:
        f.write(story)
    print(f"故事文稿已保存: {story_path}")

    # Step 2: 分段 + 生成配图关键词
    print("\n[2/6] 分段并生成配图关键词...")
    segments = split_story_into_segments(story)
    print(f"共 {len(segments)} 段\n")
    segments = generate_image_keywords(segments, topic)

    # Step 3: 生成语音
    print("[3/6] 生成语音...")
    segments = generate_all_speech(segments)
    total_duration = sum(s.get("duration", 0) for s in segments)
    print(f"语音总时长: {total_duration:.1f} 秒\n")

    # Step 4: 获取配图
    print("[4/6] 获取配图...")
    segments = fetch_all_images(segments)

    # Step 5: 生成字幕
    print("\n[5/6] 生成字幕...")
    srt_path = os.path.join(get_project_dir(), "subtitles.srt")
    generate_srt(segments, srt_path)
    print(f"字幕已保存: {srt_path}")

    # Step 6: 合成视频
    print("\n[6/6] 合成视频...")
    if not output_name:
        safe_name = "".join(c if c.isalnum() or c in "_- " else "_" for c in topic)
        output_name = safe_name[:30].strip() + ".mp4"
    if not output_name.endswith(".mp4"):
        output_name += ".mp4"

    output_path = os.path.join(get_project_dir(), output_name)
    final_path = create_video(segments, output_path)

    print(f"\n{'='*60}")
    print(f"视频生成完成！")
    print(f"项目路径: {project_dir}")
    print(f"视频路径: {final_path}")
    print(f"时长: {total_duration:.1f} 秒")
    print(f"{'='*60}\n")

    return final_path


def main():
    parser = argparse.ArgumentParser(description="自动生成视频工作流")
    parser.add_argument("topic", help="视频主题")
    parser.add_argument("--extra", "-e", default="", help="额外要求（如风格、长度等）")
    parser.add_argument("--output", "-o", default="", help="输出文件名")
    parser.add_argument("--auto", "-a", action="store_true", help="全自动模式，跳过文稿确认")
    args = parser.parse_args()

    # 检查必要配置
    from config import STEPFUN_API_KEY
    if not STEPFUN_API_KEY:
        print("错误: 缺少 STEPFUN_API_KEY 环境变量")
        print("请在 .env 文件中设置: STEPFUN_API_KEY=your_key_here")
        sys.exit(1)

    run_workflow(args.topic, args.extra, args.output, args.auto)


if __name__ == "__main__":
    main()
