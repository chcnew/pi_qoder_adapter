"""Qoder SDK session that exposes Pi tools and returns model decisions."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qoder_agent_sdk import (
    AssistantMessage,
    QoderAgentOptions,
    QoderSDKClient,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    access_token,
    create_sdk_mcp_server,
    tool,
)

from pi_qoder_adapter.events import Semantic

logger = logging.getLogger(__name__)

MCP_SERVER = "pi"
MCP_PREFIX = f"mcp__{MCP_SERVER}__"
DEFAULT_SYSTEM = (
    "You are a coding assistant responding through Pi. "
    "Pi executes every tool. When you need files or commands, call the provided tools. "
    "Do not claim a tool ran unless its result is in the conversation."
)
TOOL_WAIT_SECONDS = 30 * 60
CLAIM_WAIT_SECONDS = 30

BUILTIN_TOOLS: tuple[str, ...] = (
    "Bash", "Read", "Edit", "Write", "Glob", "Grep", "WebFetch", "WebSearch",
    "Agent", "AskUserQuestion", "NotebookEdit", "TaskOutput", "TaskStop",
    "ExitPlanMode", "EnterWorktree", "ExitWorktree", "Config", "TodoWrite",
    "ListMcpResources", "ReadMcpResource", "Mcp", "Skill",
)


@dataclass
class PiTool:
    name: str
    description: str
    parameters: dict[str, Any]
    mcp_name: str


@dataclass
class _Slot:
    tool_id: str
    name: str
    tool_input: dict[str, Any]
    future: asyncio.Future[tuple[str, bool]]
    claimed: bool = False


def public_error(exc: BaseException, token: str = "") -> str:
    text = str(exc) or exc.__class__.__name__
    if token and token in text:
        text = text.replace(token, "[redacted]")
    return text


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_schema(parameters: Any) -> dict[str, Any]:
    if not isinstance(parameters, dict) or not parameters:
        return {"type": "object", "properties": {}}
    if parameters.get("type") == "object" or "properties" in parameters:
        schema = dict(parameters)
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        return schema
    return {"type": "object", "properties": {}}


def parse_tools(raw_tools: Any) -> list[PiTool]:
    if not isinstance(raw_tools, list):
        return []
    tools: list[PiTool] = []
    used: set[str] = set()
    for item in raw_tools:
        if not isinstance(item, dict):
            continue
        function = item.get("function") if item.get("type") == "function" else item
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        description = function.get("description")
        mcp_name = _unique_mcp_name(name, used)
        tools.append(
            PiTool(
                name=name,
                description=description if isinstance(description, str) and description else name,
                parameters=normalize_schema(function.get("parameters")),
                mcp_name=mcp_name,
            )
        )
    return tools


def _unique_mcp_name(name: str, used: set[str]) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in name)[:64]
    if not cleaned:
        cleaned = "tool"
    candidate = cleaned
    suffix = 2
    while candidate in used:
        candidate = f"{cleaned[:60]}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def to_pi_name(raw_name: str, name_map: dict[str, str]) -> str:
    short = raw_name[len(MCP_PREFIX) :] if raw_name.startswith(MCP_PREFIX) else raw_name
    return name_map.get(short, short)


def usage_from(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    input_tokens = int(raw.get("input_tokens") or 0)
    output_tokens = int(raw.get("output_tokens") or 0)
    cache_read = int(raw.get("cache_read_input_tokens") or 0)
    cache_create = int(raw.get("cache_creation_input_tokens") or 0)
    prompt = input_tokens + cache_read + cache_create
    usage: dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": output_tokens,
        "total_tokens": prompt + output_tokens,
    }
    if raw.get("credits") is not None:
        usage["credits"] = raw["credits"]
    return usage


def build_options(
    *,
    token: str,
    model: str,
    system: str,
    allowed_tools: list[str],
    mcp_server: Any,
    config_dir: str,
    cwd: str,
    strategy: str = "disallow_builtins",
) -> QoderAgentOptions:
    disallowed = list(BUILTIN_TOOLS) if strategy == "disallow_builtins" else []
    tools: list[str] | None = [] if strategy == "empty" else None
    return QoderAgentOptions(
        auth=access_token(token),
        model=model,
        system_prompt=system or DEFAULT_SYSTEM,
        tools=tools,
        allowed_tools=list(allowed_tools),
        disallowed_tools=disallowed,
        permission_mode="dontAsk",
        include_partial_messages=True,
        mcp_servers={MCP_SERVER: mcp_server} if mcp_server is not None else {},
        allowed_mcp_server_names=[MCP_SERVER] if mcp_server is not None else [],
        skills=[],
        setting_sources=[],
        plugins=[],
        cwd=cwd,
        env={"QODER_CONFIG_DIR": config_dir},
        stderr=lambda line: logger.debug("qodercli: %s", line.rstrip()),
    )


class Translator:
    """Turn SDK messages into one Pi-visible model turn."""

    def __init__(self, name_map: dict[str, str]) -> None:
        self._name_map = name_map
        self.saw_text = False
        self.started_tools: set[str] = set()
        self.streamed_args: set[str] = set()
        self.saw_tool = False
        self.usage: dict[str, Any] | None = None
        self._index_to_id: dict[int, str] = {}

    def feed(self, message: Any) -> list[Semantic]:
        parent = getattr(message, "parent_tool_use_id", None)
        if parent:
            return []
        if isinstance(message, StreamEvent):
            return self._feed_stream(message.event)
        if isinstance(message, AssistantMessage):
            return self._feed_assistant(message)
        if isinstance(message, ResultMessage):
            return self._feed_result(message)
        return []

    def _feed_stream(self, event: dict[str, Any]) -> list[Semantic]:
        event_type = event.get("type")
        if event_type == "content_block_start":
            block = event.get("content_block")
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                return []
            tool_id = str(block.get("id") or "")
            if not tool_id:
                return []
            index = event.get("index")
            if isinstance(index, int):
                self._index_to_id[index] = tool_id
            return [self._start_tool(tool_id, str(block.get("name") or ""), None)]
        if event_type != "content_block_delta":
            return []
        delta = event.get("delta")
        if not isinstance(delta, dict):
            return []
        delta_type = delta.get("type")
        if delta_type == "text_delta":
            text = str(delta.get("text") or "")
            if not text:
                return []
            self.saw_text = True
            return [Semantic(kind="text", text=text)]
        if delta_type == "thinking_delta":
            text = str(delta.get("thinking") or "")
            return [Semantic(kind="thinking", text=text)] if text else []
        if delta_type == "input_json_delta":
            tool_id = self._index_to_id.get(event.get("index"))
            fragment = str(delta.get("partial_json") or "")
            if not tool_id or not fragment:
                return []
            self.streamed_args.add(tool_id)
            return [Semantic(kind="tool_args", tool_id=tool_id, text=fragment)]
        return []

    def _start_tool(self, tool_id: str, raw_name: str, tool_input: dict[str, Any] | None) -> Semantic:
        self.started_tools.add(tool_id)
        self.saw_tool = True
        return Semantic(
            kind="tool_start",
            tool_id=tool_id,
            tool_name=to_pi_name(raw_name, self._name_map),
            tool_input=tool_input,
            text="" if tool_input is None else json.dumps(tool_input, ensure_ascii=False),
        )

    def _feed_assistant(self, message: AssistantMessage) -> list[Semantic]:
        events: list[Semantic] = []
        if message.error and not self.saw_text:
            events.append(Semantic(kind="error", text=str(message.error)))
            self.saw_text = True
        for block in message.content:
            if isinstance(block, TextBlock) and block.text and not self.saw_text:
                events.append(Semantic(kind="text", text=block.text))
                self.saw_text = True
            elif isinstance(block, ThinkingBlock) and block.thinking:
                events.append(Semantic(kind="thinking", text=block.thinking))
            elif isinstance(block, ToolUseBlock):
                tool_input = block.input if isinstance(block.input, dict) else {}
                if block.id not in self.started_tools:
                    events.append(self._start_tool(block.id, block.name, tool_input))
                else:
                    events.append(
                        Semantic(
                            kind="tool_input",
                            tool_id=block.id,
                            tool_name=to_pi_name(block.name, self._name_map),
                            tool_input=tool_input,
                        )
                    )
                    if block.id not in self.streamed_args:
                        events.append(
                            Semantic(
                                kind="tool_args",
                                tool_id=block.id,
                                tool_name=to_pi_name(block.name, self._name_map),
                                text=json.dumps(tool_input, ensure_ascii=False),
                            )
                        )
                self.saw_tool = True
        parsed = usage_from(message.usage)
        if parsed:
            self.usage = parsed
            events.append(Semantic(kind="usage", usage=parsed))
        if self.saw_tool:
            events.append(Semantic(kind="finish", finish_reason="tool_calls", usage=self.usage))
        return events

    def _feed_result(self, message: ResultMessage) -> list[Semantic]:
        events: list[Semantic] = []
        if message.is_error and not self.saw_text:
            detail = "; ".join(message.errors or []) or message.result or message.subtype
            events.append(Semantic(kind="error", text=detail or "Qoder request failed"))
        if self.usage is None:
            parsed = usage_from(message.usage)
            if parsed:
                self.usage = parsed
                events.append(Semantic(kind="usage", usage=parsed))
        events.append(Semantic(kind="finish", finish_reason="stop", usage=self.usage))
        return events


class QoderRuntime:
    """One long-lived qodercli process for a single Pi conversation."""

    def __init__(self, *, token: str, model: str, system: str, tools: list[PiTool]) -> None:
        self.token = token
        self.model = model
        self.system = system
        self.tools = tools
        self._name_map = {tool.mcp_name: tool.name for tool in tools}
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._client: QoderSDKClient | None = None
        self._reader: asyncio.Task[None] | None = None
        self._workdir: Path | None = None
        self._slots: dict[str, _Slot] = {}
        self._unclaimed: list[_Slot] = []
        self._cond = asyncio.Condition()
        self._closed = False

    async def start(self) -> None:
        self._workdir = Path(tempfile.mkdtemp(prefix="pi-qoder-"))
        config_dir = self._workdir / "config"
        config_dir.mkdir()
        mcp_server = self._build_mcp_server()
        allowed = [f"{MCP_PREFIX}{tool.mcp_name}" for tool in self.tools]
        options = build_options(
            token=self.token,
            model=self.model,
            system=self.system,
            allowed_tools=allowed,
            mcp_server=mcp_server,
            config_dir=str(config_dir),
            cwd=str(self._workdir),
        )
        self._client = QoderSDKClient(options)
        try:
            await self._client.connect()
        except Exception:
            await self.aclose()
            raise
        self._reader = asyncio.create_task(self._read_loop())

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        async with self._cond:
            for slot in self._slots.values():
                if not slot.future.done():
                    slot.future.set_result(("session closed", True))
            self._cond.notify_all()
        await self._queue.put(ConnectionError("session closed"))
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                logger.debug("qodercli disconnect failed", exc_info=True)
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except asyncio.CancelledError:
                pass
            self._reader = None
        if self._workdir is not None:
            shutil.rmtree(self._workdir, ignore_errors=True)
            self._workdir = None

    async def query(self, prompt: str) -> None:
        if self._client is None:
            raise ConnectionError("Qoder session is not connected")
        logger.info("qoder query chars=%s model=%s", len(prompt), self.model)
        await self._client.query(prompt)

    async def resolve_tools(self, results: dict[str, tuple[str, bool]]) -> None:
        async with self._cond:
            missing = [tool_id for tool_id in results if tool_id not in self._slots]
            if missing:
                raise KeyError("unknown tool call")
            for tool_id, result in results.items():
                slot = self._slots[tool_id]
                if not slot.future.done():
                    slot.future.set_result(result)
            self._cond.notify_all()

    async def next_turn(self) -> AsyncIterator[Semantic]:
        translator = Translator(self._name_map)
        while True:
            message = await self._queue.get()
            if isinstance(message, BaseException):
                yield Semantic(kind="error", text=public_error(message, self.token))
                yield Semantic(kind="finish", finish_reason="stop")
                return
            for event in translator.feed(message):
                if event.kind in {"tool_start", "tool_input"} and event.tool_input is not None:
                    await self._register_slot(event.tool_id, event.tool_name, event.tool_input)
                if event.kind == "tool_input":
                    continue
                yield event
                if event.kind == "finish":
                    return

    def _build_mcp_server(self) -> Any:
        sdk_tools = []
        for spec in self.tools:
            sdk_tools.append(
                tool(spec.mcp_name, spec.description, spec.parameters)(self._handler(spec.name))
            )
        return create_sdk_mcp_server(name=MCP_SERVER, tools=sdk_tools)

    def _handler(self, pi_name: str) -> Any:
        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            try:
                slot = await asyncio.wait_for(
                    self._claim(pi_name, args if isinstance(args, dict) else {}),
                    timeout=CLAIM_WAIT_SECONDS,
                )
            except TimeoutError:
                logger.warning("tool handler did not match a pending call name=%s", pi_name)
                return {
                    "is_error": True,
                    "content": [{"type": "text", "text": "Pi did not accept this tool call."}],
                }
            try:
                text, is_error = await asyncio.wait_for(slot.future, timeout=TOOL_WAIT_SECONDS)
            except TimeoutError:
                return {
                    "is_error": True,
                    "content": [
                        {
                            "type": "text",
                            "text": "Timed out waiting for Pi to return the tool result.",
                        }
                    ],
                }
            payload: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
            if is_error:
                payload["is_error"] = True
            return payload

        return handler

    async def _claim(self, pi_name: str, args: dict[str, Any]) -> _Slot:
        wanted = canonical_json(args)
        async with self._cond:
            while True:
                exact = [
                    slot
                    for slot in self._unclaimed
                    if slot.name == pi_name and canonical_json(slot.tool_input) == wanted
                ]
                same_name = [slot for slot in self._unclaimed if slot.name == pi_name]
                chosen = exact[0] if exact else same_name[0] if len(same_name) == 1 else None
                if chosen is not None:
                    chosen.claimed = True
                    self._unclaimed.remove(chosen)
                    return chosen
                await self._cond.wait()

    async def _register_slot(self, tool_id: str, pi_name: str, tool_input: dict[str, Any]) -> None:
        async with self._cond:
            existing = self._slots.get(tool_id)
            if existing is None:
                slot = _Slot(
                    tool_id=tool_id,
                    name=pi_name,
                    tool_input=tool_input,
                    future=asyncio.get_running_loop().create_future(),
                )
                self._slots[tool_id] = slot
                self._unclaimed.append(slot)
            else:
                existing.tool_input = tool_input
            self._cond.notify_all()

    async def _read_loop(self) -> None:
        assert self._client is not None
        try:
            async for message in self._client.receive_messages():
                await self._queue.put(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._queue.put(exc)
        else:
            await self._queue.put(ConnectionError("qodercli closed the session"))
