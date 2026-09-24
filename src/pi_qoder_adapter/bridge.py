"""Keep one Qoder session aligned with one Pi conversation."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

from pi_qoder_adapter.events import Semantic
from pi_qoder_adapter.qoder_runtime import PiTool, QoderRuntime, parse_tools, public_error
from pi_qoder_adapter.transcript import (
    EmptyDelta,
    Mismatch,
    NormMsg,
    ToolDelta,
    UserDelta,
    first_user_text,
    match_delta,
    render_reseed,
    session_key,
    split_messages,
    tools_signature,
)

logger = logging.getLogger(__name__)

RuntimeFactory = Callable[..., QoderRuntime]


class BridgeError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class _Session:
    def __init__(self) -> None:
        self.runtime: QoderRuntime | None = None
        self.acknowledged: list[NormMsg] = []
        self.pending: set[str] = set()
        self.tools_sig = ""
        self.force_reseed = False
        self.touched = time.monotonic()
        self.lock = asyncio.Lock()

    async def aclose(self) -> None:
        runtime = self.runtime
        self.runtime = None
        self.acknowledged = []
        self.pending = set()
        if runtime is not None:
            await runtime.aclose()


class SessionManager:
    def __init__(self, factory: RuntimeFactory | None = None) -> None:
        self._factory = factory or QoderRuntime
        self._sessions: dict[str, _Session] = {}
        self._lock = asyncio.Lock()

    async def iter_semantics(self, token: str, body: dict[str, Any]) -> AsyncIterator[Semantic]:
        model = body.get("model")
        messages = body.get("messages")
        if not isinstance(model, str) or not model:
            raise BridgeError("model is required")
        if not isinstance(messages, list) or not messages:
            raise BridgeError("messages is required")
        system, convo = split_messages(messages)
        if not any(item.role == "user" for item in convo):
            raise BridgeError("messages must include a user message")
        tools = parse_tools(body.get("tools"))
        signature = tools_signature(
            [
                {"name": tool.name, "description": tool.description, "parameters": tool.parameters}
                for tool in tools
            ]
        )
        key = session_key(token, model, system, first_user_text(convo))
        session = await self._get(key)
        async with session.lock:
            session.touched = time.monotonic()
            delta = match_delta(session.acknowledged, convo, session.pending)
            history_matches = (
                session.runtime is not None
                and not session.force_reseed
                and session.tools_sig == signature
                and not isinstance(delta, Mismatch)
            )
            try:
                if not history_matches:
                    first_turn = (
                        session.runtime is None
                        and not session.force_reseed
                        and isinstance(delta, UserDelta)
                    )
                    await self._open(session, token, model, system, tools, signature)
                    if first_turn and isinstance(delta, UserDelta):
                        prompt = "\n\n".join(item.content for item in delta.messages)
                        base = list(delta.messages)
                    else:
                        prompt = render_reseed(convo)
                        base = list(convo)
                    logger.info("qoder reseed model=%s chars=%s", model, len(prompt))
                    await session.runtime.query(prompt)  # type: ignore[union-attr]
                elif isinstance(delta, EmptyDelta):
                    raise BridgeError("no new message")
                elif isinstance(delta, UserDelta):
                    prompt = "\n\n".join(item.content for item in delta.messages)
                    base = [*session.acknowledged, *delta.messages]
                    logger.info("qoder user turn model=%s chars=%s", model, len(prompt))
                    await session.runtime.query(prompt)  # type: ignore[union-attr]
                elif isinstance(delta, ToolDelta):
                    base = [*session.acknowledged, *delta.results]
                    results = {
                        item.tool_call_id or "": (item.content, False) for item in delta.results
                    }
                    logger.info("qoder tool results model=%s count=%s", model, len(results))
                    try:
                        await session.runtime.resolve_tools(results)  # type: ignore[union-attr]
                    except KeyError as exc:
                        await self._open(session, token, model, system, tools, signature)
                        prompt = render_reseed(convo)
                        base = list(convo)
                        await session.runtime.query(prompt)  # type: ignore[union-attr]
                        logger.info("qoder tool ids reset the session: %s", exc)
                else:
                    raise BridgeError("unsupported message delta")
                assert session.runtime is not None
                assistant_text: list[str] = []
                tool_calls: list[tuple[str, str]] = []
                finish_reason = "stop"
                finished = False
                async for event in session.runtime.next_turn():
                    if event.kind == "text":
                        assistant_text.append(event.text)
                    elif event.kind == "error" and event.text:
                        session.force_reseed = True
                        raise BridgeError(event.text, status=502)
                    elif event.kind == "tool_start":
                        tool_calls.append((event.tool_id, event.tool_name))
                    yield event
                    if event.kind == "finish":
                        finish_reason = event.finish_reason or "stop"
                        finished = True
                        break
                if not finished:
                    session.force_reseed = True
                    raise BridgeError("model stream ended early", status=502)
            except asyncio.CancelledError:
                session.force_reseed = True
                raise
            except BridgeError:
                raise
            except Exception as exc:
                session.force_reseed = True
                raise BridgeError(public_error(exc, token), status=502) from exc
            session.acknowledged = [
                *base,
                NormMsg(
                    role="assistant",
                    content="".join(assistant_text).strip(),
                    tool_calls=tuple(tool_calls),
                ),
            ]
            session.pending = {tool_id for tool_id, _name in tool_calls} if finish_reason == "tool_calls" else set()
            session.force_reseed = False
            logger.info("qoder turn finished model=%s reason=%s", model, finish_reason)

    async def close_idle(self, max_idle_seconds: float = 1800) -> None:
        now = time.monotonic()
        async with self._lock:
            stale = [
                self._sessions.pop(key)
                for key, session in list(self._sessions.items())
                if now - session.touched > max_idle_seconds
            ]
        for session in stale:
            await session.aclose()

    async def aclose_all(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            await session.aclose()

    async def _get(self, key: str) -> _Session:
        async with self._lock:
            session = self._sessions.get(key)
            if session is None:
                session = _Session()
                self._sessions[key] = session
            return session

    async def _open(
        self,
        session: _Session,
        token: str,
        model: str,
        system: str,
        tools: list[PiTool],
        signature: str,
    ) -> None:
        await session.aclose()
        runtime = self._factory(token=token, model=model, system=system, tools=tools)
        try:
            await runtime.start()
        except Exception as exc:
            await runtime.aclose()
            raise BridgeError(public_error(exc, token), status=502) from exc
        session.runtime = runtime
        session.tools_sig = signature
        session.acknowledged = []
        session.pending = set()
        session.force_reseed = False
