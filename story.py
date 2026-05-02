"""生成故事文字稿"""
from openai import OpenAI
from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)


SYSTEM_PROMPT = """你是一位资深的故事解说文案撰写专家。
请根据用户提供的主题，撰写一段适合视频解说的故事文案。

【风格要求——每次生成必须采用不同的叙事风格，不要固定套路】
请根据主题自动选择最合适的一种或组合多种风格，可选方向包括：
- 悬疑揭秘型：设置悬念钩子，层层剥茧推进，结尾有反转或恍然大悟
- 温情治愈型：聚焦人物命运与情感细节，语言细腻，收尾温暖有力
- 硬核科普型：数据驱动，逻辑推演严密，结论掷地有声
- 历史穿越型：古今对照，时空交错，以史为鉴照见当下
- 对话访谈型：模拟第一人称对话或现场采访，口语化强，代入感深
- 吐槽锐评型：观点鲜明，语言带锋芒，金句频出，节奏快
- 沉浸游记型：像带着观众实地探访，画面随脚步推进，感官细节丰富
- 反常识颠覆型：先抛出反直觉结论，再用证据一步步推翻常识认知

【格式要求】
1. 用中文撰写，语言生动、有画面感
2. 每段 2-4 句话，适合配一张图
3. 总字数控制在 800-1500 字之间，适合 3-5 分钟的视频
4. 每段开头用 [段落] 标记，方便后续处理
5. 不要在文案中使用括号添加指令或表情符号
6. 返回格式：只返回故事正文，不要有任何前言或总结"""


def generate_story(topic: str, extra_requirements: str = "") -> str:
    """根据主题生成故事文字稿"""
    user_prompt = f"主题：{topic}"
    if extra_requirements:
        user_prompt += f"\n额外要求：{extra_requirements}"

    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.8,
        max_tokens=3000,
    )

    story = response.choices[0].message.content.strip()
    return story


def split_story_into_segments(story: str) -> list[dict]:
    """将故事分割成段落，每段包含文字和配图关键词"""
    import re

    # 按 [段落] 或空行分割
    raw_segments = re.split(r'\[段落\]|\n\s*\n', story)
    segments = []

    for seg in raw_segments:
        seg = seg.strip()
        if not seg or len(seg) < 10:
            continue
        segments.append({
            "text": seg,
            "image_keyword": "",
        })

    # 如果没有 [段落] 标记，就按句子数简单分段
    if len(segments) <= 1:
        sentences = re.split(r'(?<=[。！？\.\!\?])\s+', story)
        segments = []
        current = ""
        for s in sentences:
            s = s.strip()
            if not s:
                continue
            current += s
            if len(current) >= 80 or s.endswith(("！", "？")):
                segments.append({"text": current, "image_keyword": ""})
                current = ""
        if current:
            segments.append({"text": current, "image_keyword": ""})

    return segments


def generate_image_keywords(segments: list[dict], topic: str) -> list[dict]:
    """为每段生成配图搜索关键词（英文，方便搜图）"""
    prompt = f"""为主题为「{topic}」的故事生成配图搜索关键词。
故事分为以下 {len(segments)} 段，请为每段生成一个适合在图片库搜索的英文关键词或短语（2-4 个英文单词），要求画面感强、具象化。

格式要求：只返回每段的关键词，每行一个，不要编号，不要解释。

故事段落：
"""
    for i, seg in enumerate(segments):
        prompt += f"\n段落{i+1}：{seg['text'][:100]}..."

    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": "你是一个图片搜索关键词生成专家。只输出英文关键词，每行一个。"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.7,
        max_tokens=500,
    )

    keywords = response.choices[0].message.content.strip().split('\n')
    keywords = [k.strip().strip('"').strip("'").strip('- ') for k in keywords if k.strip()]

    for i, seg in enumerate(segments):
        if i < len(keywords):
            seg["image_keyword"] = keywords[i]
        else:
            seg["image_keyword"] = topic

    return segments


def self_review_story(story: str, topic: str) -> dict:
    """自我校验故事文案的数据准确性和逻辑合理性"""
    import json

    prompt = f"""你是一个严格的内容审核员。请仔细审核以下关于「{topic}」的视频解说文案，检查以下问题：

1. 数据准确性：文中提到的数字、日期、比例是否合理？是否有明显编造的痕迹？
2. 逻辑一致性：推理链条是否通顺？因果关系的表述是否站得住脚？
3. 事实错误：是否有与常识严重相悖的说法？
4. 违禁内容：是否包含政治敏感、色情、暴力、仇恨言论？
5. 标题党/夸大：是否有过度夸大、误导读者的表述？

文案内容：
{story[:2000]}

请严格用JSON格式返回（不要markdown代码块，不要其他文字）：
{{
  "passed": boolean,
  "issues": ["string"],
  "severity": "low" | "medium" | "high",
  "improved_version": "string（如果有问题给出修改建议，通过则空字符串）"
}}
"""

    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=1500,
    )

    content = response.choices[0].message.content.strip()
    try:
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()
        return json.loads(content)
    except Exception as e:
        return {"passed": True, "issues": [f"解析审核结果失败: {e}"], "severity": "low", "improved_version": ""}


def generate_story_with_retry(topic: str, extra_requirements: str = "", max_retries: int = 2) -> str:
    """带自我校验和重试的故事生成"""
    for attempt in range(max_retries + 1):
        story = generate_story(topic, extra_requirements)
        review = self_review_story(story, topic)

        if review.get("passed"):
            print(f"[Story] 自我校验通过（尝试 {attempt + 1}/{max_retries + 1}）")
            return story

        print(f"[Story] 自我校验未通过（尝试 {attempt + 1}/{max_retries + 1}）：{review.get('issues', [])}")

        if attempt < max_retries and review.get("improved_version"):
            extra_requirements = (extra_requirements or "") + f"\n请基于以下改进意见修改：{review.get('improved_version', '')[:500]}"

    print(f"[Story] 已达到最大重试次数，返回最后一次生成的版本")
    return story
