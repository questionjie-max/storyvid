"""LLM 客户端（懒加载）+ 通用调用与 JSON 解析工具

客户端在首次调用时才创建，避免「import 就崩」：
未配置 key 时 `import story` / `import research` 不再报错，方便测试和 --help。
"""
import json
import uuid

_client = None


def _default_headers(base_url: str) -> dict:
    """OpenCode（opencode.ai）路由要求每个会话带 x-opencode-session 头，
    否则 400 MissingSessionID；其他 OpenAI 兼容服务忽略未知头，无副作用。
    每个进程生成一次随机会话 ID 即可。"""
    if "opencode.ai" in (base_url or ""):
        return {"x-opencode-session": str(uuid.uuid4())}
    return {}


def get_client():
    """返回 OpenAI 兼容客户端；未配置 key 时抛出带指引的异常

    每次调用时读取 config（而不是 import 时绑定），
    与 chat() 里延迟导入 LLM_MODEL 的做法保持一致，测试改配置也能生效。
    """
    global _client
    if _client is None:
        import config

        if not config.LLM_API_KEY:
            raise RuntimeError(
                "LLM_API_KEY 未配置。请在 .env 中设置（支持 DeepSeek / Kimi / OpenAI 等"
                "任意 OpenAI 兼容 API），或直接配置 STEPFUN_API_KEY 作为回退。"
            )
        from openai import OpenAI

        _client = OpenAI(
            api_key=config.LLM_API_KEY,
            base_url=config.LLM_BASE_URL,
            default_headers=_default_headers(config.LLM_BASE_URL),
        )
    return _client


def chat(
    user: str,
    system: str = "",
    temperature: float = 0.7,
    max_tokens: int = 2000,
    model: str | None = None,
) -> str:
    """一次简单的对话调用，返回回复文本"""
    from config import LLM_MODEL

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})

    response = get_client().chat.completions.create(
        model=model or LLM_MODEL,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    # 部分 OpenAI 兼容服务在触发了内容过滤等场景会返回 content=None
    return (response.choices[0].message.content or "").strip()


def parse_json_loose(content: str) -> dict:
    """解析 LLM 返回的 JSON，容忍 ```json 围栏和前后杂文字"""
    content = content.strip()
    if "```json" in content:
        content = content.split("```json")[1].split("```")[0]
    elif "```" in content:
        content = content.split("```")[1].split("```")[0]
    # 截取第一个 { 到最后一个 } 之间的内容，容忍围栏外的说明文字
    start, end = content.find("{"), content.rfind("}")
    if start != -1 and end > start:
        content = content[start : end + 1]
    return json.loads(content)
