"""Remove client identity declarations from system/developer text only."""

import os
import re


_DECLARATION = re.compile(
    r"^(?:you\s+are\b|your\s+(?:designated\s+)?identity\b|"
    r"you\s+have\s+been\s+invoked\b|you\s+are\s+powered\s+by\b|"
    r"你(?:是|的身份是|的角色是|由)|您(?:是|的身份是|的角色是|由))",
    re.IGNORECASE,
)
_IDENTITY = re.compile(
    r"\b(?:zcode|codex|claude(?:\s+code)?|codebuddy|workbuddy|"
    r"opencode|sisyphus|anthropic|openai|chatgpt|gemini|"
    r"deepseek|qwen|kimi|glm|gpt|AI|LLM|assistant|agent|model)\b|"
    r"人工智能|智能助手|编程助手|编码助手|语言模型|智能体",
    re.IGNORECASE,
)
_OBLIGATION = re.compile(
    r"^you\s+are\s+(?:required|expected|allowed|not allowed|forbidden|responsible|"
    r"operating|working|running|using|to)\b", re.IGNORECASE
)
_HEADINGS = re.compile(
    r"^#{1,6}\s+(?:ZCode|Codex|Claude Code|CodeBuddy|WorkBuddy|OpenCode)\s+",
    re.IGNORECASE,
)
_BRANCH_TEMPLATE = re.compile(
    r"Main branch \(you will usually use this for PRs\):", re.IGNORECASE
)


def filter_system_text(text: str) -> str:
    lines = []
    for line in text.splitlines(keepends=True):
        candidate = re.sub(r"^\s*(?:[-*]\s+|#{1,6}\s+)?", "", line)
        # Limit removal to declarative sentences. Keep subsequent instructions.
        pieces = re.split(r"(?<=[.!?。！？])(?=\s|$)", candidate)
        kept = []
        removed = False
        for piece in pieces:
            clean = piece.strip()
            if _DECLARATION.search(clean) and _IDENTITY.search(clean) and not _OBLIGATION.search(clean):
                removed = True
            else:
                kept.append(piece)
        if removed:
            line = "".join(kept).lstrip()
            if not line.strip():
                continue
        # Preserve branch value and section meaning, dropping fixed client wording.
        line = _BRANCH_TEMPLATE.sub("Main branch:", line)
        line = _HEADINGS.sub("# ", line)
        lines.append(line)
    return "".join(lines)


def filter_system_identity(body: dict) -> dict:
    """Return a new body; never modify user/assistant/tool messages or tool schemas."""
    messages = []
    for message in body.get("messages") or []:
        if not isinstance(message, dict) or message.get("role") not in ("system", "developer"):
            messages.append(message)
            continue
        content = message.get("content")
        if isinstance(content, str):
            filtered = filter_system_text(content)
            if filtered.strip():
                messages.append(dict(message, content=filtered))
        elif isinstance(content, list):
            blocks = []
            for block in content:
                if isinstance(block, dict) and block.get("type") in ("text", "input_text", "output_text") and isinstance(block.get("text"), str):
                    filtered = filter_system_text(block["text"])
                    if filtered.strip():
                        blocks.append(dict(block, text=filtered))
                else:
                    blocks.append(block)
            if blocks:
                messages.append(dict(message, content=blocks))
        else:
            messages.append(message)
    return dict(body, messages=messages)


# 上游硬性要求 messages 首条为 system prompt，否则返回：
#   11128 "first message is not system prompt"
# 客户端未提供 system，或 filter_system_identity 把 system 内容清空并整条删除后，
# 首条会变成 user，触发拦截。这里补一条最小 system 兜底。
DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."


def ensure_leading_system(body: dict, default: str | None = None) -> dict:
    """确保 messages 以一条非空 system 消息开头。

    必须在 filter_system_identity / 脱敏之后调用，避免兜底内容被再次清空。

    Args:
        body: 已构造好的 Chat Completions 请求体。
        default: 兜底 system 内容；默认读环境变量 CODEBUDDY_DEFAULT_SYSTEM。

    Returns:
        新请求体；无需改动时原样返回。
    """
    messages = body.get("messages")
    if not isinstance(messages, list):
        return body

    placeholder = (
        default
        if default is not None
        else os.environ.get("CODEBUDDY_DEFAULT_SYSTEM", DEFAULT_SYSTEM_PROMPT)
    )

    first = messages[0] if messages else None
    if isinstance(first, dict) and first.get("role") in ("system", "developer"):
        content = first.get("content")
        has_text = (isinstance(content, str) and content.strip()) or (
            isinstance(content, list) and len(content) > 0
        )
        if has_text:
            return body
        # 首条 system 存在但内容为空 -> 原地替换为兜底内容
        return dict(body, messages=[dict(first, content=placeholder), *messages[1:]])

    # 首条不是 system（缺失或被删除）-> 前置一条兜底 system
    return dict(body, messages=[{"role": "system", "content": placeholder}, *messages])
