"""主题搜索 + 内容审核"""
import json
from ddgs import DDGS
from openai import OpenAI
from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)


def search_topic(topic: str, max_results: int = 8) -> list[dict]:
    """用 DuckDuckGo 搜索主题相关内容"""
    try:
        with DDGS() as ddgs:
            results = ddgs.text(topic, max_results=max_results, region="cn-zh")
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
        [f"标题: {r['title']}\n摘要: {r['body'][:200]}" for r in search_results[:5]]
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

    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=600,
    )

    content = response.choices[0].message.content.strip()
    try:
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()
        return json.loads(content)
    except Exception:
        return {
            "is_hot": True,
            "is_safe": True,
            "risk_level": "low",
            "reason": "解析失败，默认通过",
            "suggestions": "",
        }


def generate_story_with_research(topic: str, search_results: list[dict], extra_requirements: str = "") -> str:
    """基于搜索结果生成故事文案"""
    from story import SYSTEM_PROMPT

    context = "\n\n".join(
        [f"[来源{i+1}] {r['title']}\n{r['body'][:300]}" for i, r in enumerate(search_results[:5])]
    )

    user_prompt = f"主题：{topic}"
    if extra_requirements:
        user_prompt += f"\n额外要求：{extra_requirements}"

    user_prompt += (
        f"\n\n以下是该主题的参考信息，请基于这些信息撰写故事文案"
        f"（不要编造数据，优先使用搜索到的信息）：\n\n{context}"
    )

    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.8,
        max_tokens=3000,
    )

    return response.choices[0].message.content.strip()
