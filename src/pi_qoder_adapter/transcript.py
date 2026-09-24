"""Match Pi's full transcript against the part Qoder has already seen."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NormMsg:
    role: str
    content: str
    tool_calls: tuple[tuple[str, str], ...] = ()
    tool_call_id: str | None = None


@dataclass(frozen=True)
class UserDelta:
    messages: tuple[NormMsg, ...]


@dataclass(frozen=True)
class ToolDelta:
    results: tuple[NormMsg, ...]


@dataclass(frozen=True)
class EmptyDelta:
    pass


@dataclass(frozen=True)
class Mismatch:
    pass


Delta = UserDelta | ToolDelta | EmptyDelta | Mismatch


def session_key(token: str, model: str, system: str, first_user: str) -> str:
    raw = json.dumps([token, model, system, first_user], ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def tools_signature(tools: list[dict[str, Any]]) -> str:
    raw = json.dumps(tools, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def _text_from_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif item.get("type") == "image_url":
                parts.append("[image omitted]")
        return "".join(parts)
    return str(content)


def normalize_message(message: dict[str, Any]) -> NormMsg:
    role = str(message.get("role") or "")
    tool_calls: list[tuple[str, str]] = []
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        call_id = str(call.get("id") or "")
        name = str(function.get("name") or "")
        if call_id and name:
            tool_calls.append((call_id, name))
    tool_call_id = message.get("tool_call_id")
    return NormMsg(
        role=role,
        content=_text_from_content(message.get("content")).strip(),
        tool_calls=tuple(tool_calls),
        tool_call_id=str(tool_call_id) if tool_call_id else None,
    )


def split_messages(messages: list[Any]) -> tuple[str, list[NormMsg]]:
    system_parts: list[str] = []
    convo: list[NormMsg] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role in {"system", "developer"}:
            text = _text_from_content(message.get("content")).strip()
            if text:
                system_parts.append(text)
            continue
        convo.append(normalize_message(message))
    return "\n\n".join(system_parts), convo


def _same(left: NormMsg, right: NormMsg) -> bool:
    if left.role != right.role or left.tool_call_id != right.tool_call_id:
        return False
    if left.tool_calls != right.tool_calls:
        return False
    if left.role == "assistant" and (left.tool_calls or right.tool_calls):
        return True
    return left.content == right.content


def match_delta(
    acknowledged: list[NormMsg],
    incoming: list[NormMsg],
    pending_tool_ids: set[str],
) -> Delta:
    if len(incoming) < len(acknowledged):
        return Mismatch()
    for left, right in zip(acknowledged, incoming, strict=False):
        if not _same(left, right):
            return Mismatch()
    rest = incoming[len(acknowledged) :]
    if not rest:
        return EmptyDelta()
    if all(item.role == "tool" for item in rest):
        ids = {item.tool_call_id for item in rest}
        if pending_tool_ids and ids == pending_tool_ids and None not in ids:
            return ToolDelta(tuple(rest))
        return Mismatch()
    if all(item.role == "user" for item in rest):
        return UserDelta(tuple(rest))
    return Mismatch()


def render_reseed(messages: list[NormMsg]) -> str:
    lines = [
        "Continue this conversation from the transcript below.",
        "Use the provided tools when you need to act.",
        "Do not claim a tool ran unless its result is in the transcript.",
        "",
    ]
    for message in messages:
        if message.role == "user":
            lines.append(f"User: {message.content}")
        elif message.role == "assistant":
            if message.content:
                lines.append(f"Assistant: {message.content}")
            for call_id, name in message.tool_calls:
                lines.append(f"Assistant called {name} id={call_id}")
        elif message.role == "tool":
            lines.append(f"Tool result id={message.tool_call_id}: {message.content}")
    return "\n".join(lines).strip()


def first_user_text(messages: list[NormMsg]) -> str:
    for message in messages:
        if message.role == "user":
            return message.content
    return ""
