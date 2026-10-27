"""主题搜索 + 内容审核"""
from ddgs import DDGS

import llm


def search_topic(topic: str, max_results: int = 8) -> list[dict]:
    """用 DuckDuckGo 搜索主题相关内容；失败时返回空列表，流程照常走 LLM 常识生成"""
    try:
        results = DDGS().text(topic, max_results=max_results, region="cn-zh")
        return list(results)
    except Exception as e:
        print(f"[Research] 搜索失败（含 cn-zh 区域）: {e}，改用默认区域重试...")
    try:
        results = DDGS().text(topic, max_results=max_results)
        return list(results)
    except Exception as e:
        print(f"[Research] 搜索失败: {e}")
        return []


def analyze_content(topic: str, search_results: list[dict]) -> dict:
    """分析内容是否热门、是否有违禁风险"""
    if not search_results:
        return {
            "is_hot": False,
            "is_safe": True,
            "risk_level": "low",
            "reason": "无搜索结果，基于LLM常识判断",
            "suggestions": "",
        }

    context = "\n".join(
        f"标题: {r.get('title', '')}\n摘要: {r.get('body', '')[:200]}"
        for r in search_results[:5]
    )

    prompt = f"""你是一个内容审核专家。请根据以下搜索结果，分析主题「{topic}」的内容风险。

搜索结果：
{context}

请严格用JSON格式返回（不要markdown代码块，不要其他文字）：
{{
  "is_hot": boolean,
  "is_safe": boolean,
  "risk_level": "low" | "medium" | "high",
  "reason": "string",
  "suggestions": "string"
}}
"""

    try:
        content = llm.chat(prompt, temperature=0.3, max_tokens=600)
        return llm.parse_json_loose(content)
    except Exception as e:
        print(f"[Research] 内容审核调用失败，默认放行: {e}")
        return {
            "is_hot": True,
            "is_safe": True,
            "risk_level": "low",
            "reason": f"审核服务不可用，默认通过（{e}）",
            "suggestions": "",
        }


def generate_story_with_research(topic: str, search_results: list[dict], extra_requirements: str = "") -> str:
    """基于搜索结果生成故事文案"""
    from story import SYSTEM_PROMPT

    context = "\n\n".join(
        f"[来源{i+1}] {r.get('title', '')}\n{r.get('body', '')[:300]}"
        for i, r in enumerate(search_results[:5])
    )

    user_prompt = f"主题：{topic}"
    if extra_requirements:
        user_prompt += f"\n额外要求：{extra_requirements}"

    user_prompt += (
        f"\n\n以下是该主题的参考信息，请基于这些信息撰写故事文案"
        f"（不要编造数据，优先使用搜索到的信息）：\n\n{context}"
    )

    return llm.chat(
        user_prompt,
        system=SYSTEM_PROMPT,
        temperature=0.8,
        max_tokens=3000,
    )
