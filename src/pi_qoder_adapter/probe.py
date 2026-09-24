"""Check that a Qoder MCP tool can wait and hand control back to the caller."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from qoder_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
    tool,
)

from pi_qoder_adapter.qoder_runtime import BUILTIN_TOOLS, MCP_PREFIX, build_options


async def _run_once(
    *,
    token: str,
    model: str,
    strategy: str,
    wait_seconds: float,
) -> dict[str, Any]:
    called = asyncio.Event()
    release = asyncio.Event()
    started = 0.0
    elapsed = 0.0

    @tool(
        "ping",
        "Wait until the host releases this call, then return the elapsed time.",
        {"note": str},
    )
    async def ping(_args: dict[str, Any]) -> dict[str, Any]:
        nonlocal started, elapsed
        started = time.monotonic()
        called.set()
        if wait_seconds > 0:
            try:
                await asyncio.wait_for(release.wait(), timeout=wait_seconds + 30)
            except TimeoutError:
                return {
                    "is_error": True,
                    "content": [{"type": "text", "text": "probe release timed out"}],
                }
        elapsed = time.monotonic() - started
        return {"content": [{"type": "text", "text": f"released after {elapsed:.1f}s"}]}

    server = create_sdk_mcp_server(name="pi", tools=[ping])
    workdir = Path(tempfile.mkdtemp(prefix="pi-qoder-probe-"))
    config_dir = workdir / "config"
    config_dir.mkdir()
    options = build_options(
        token=token,
        model=model,
        system="Call the ping tool exactly once with note set to hi, then summarize the tool result.",
        allowed_tools=[f"{MCP_PREFIX}ping"],
        mcp_server=server,
        config_dir=str(config_dir),
        cwd=str(workdir),
        strategy=strategy,
    )
    messages: list[Any] = []

    async def consume() -> None:
        async for message in query(
            prompt="Call the ping tool now with note hi. Do not answer before the tool returns.",
            options=options,
        ):
            messages.append(message)

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(called.wait(), timeout=120)
        if wait_seconds > 0:
            await asyncio.sleep(wait_seconds)
            release.set()
        await asyncio.wait_for(consumer, timeout=180)
    except TimeoutError:
        release.set()
    finally:
        release.set()
        if not consumer.done():
            consumer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await consumer
        shutil.rmtree(workdir, ignore_errors=True)
    tool_called = called.is_set()
    continued = _model_continued(messages)
    builtins_seen = _builtin_tools_seen(messages)
    return {
        "strategy": strategy,
        "tool_called": tool_called,
        "elapsed": elapsed,
        "model_continued": continued,
        "builtin_tool_called": builtins_seen,
        "message_count": len(messages),
    }


def _model_continued(messages: list[Any]) -> bool:
    saw_tool = False
    for message in messages:
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    saw_tool = True
                elif isinstance(block, TextBlock) and block.text and saw_tool:
                    return True
        if isinstance(message, ResultMessage) and saw_tool and not message.is_error:
            return True
    return False


def _builtin_tools_seen(messages: list[Any]) -> list[str]:
    found: list[str] = []
    for message in messages:
        if not isinstance(message, AssistantMessage):
            continue
        for block in message.content:
            if isinstance(block, ToolUseBlock) and block.name in BUILTIN_TOOLS:
                found.append(block.name)
    return found


async def run_probe(*, model: str = "efficient", wait_seconds: float = 60) -> int:
    token = os.environ.get("QODER_PERSONAL_ACCESS_TOKEN", "").strip()
    if not token:
        print("未设置 QODER_PERSONAL_ACCESS_TOKEN，探针无法连接 Qoder。")
        return 2

    print("探针 1/3：tools=[] 时模型能否看见 MCP 工具")
    empty = await _run_once(token=token, model=model, strategy="empty", wait_seconds=0)
    print(
        f"tools=[] tool_called={empty['tool_called']} "
        f"builtin={empty['builtin_tool_called'] or 'none'}"
    )

    print("探针 2/3：disallow_builtins 时模型能否看见 MCP 工具")
    blocked = await _run_once(token=token, model=model, strategy="disallow_builtins", wait_seconds=0)
    print(
        f"disallow_builtins tool_called={blocked['tool_called']} "
        f"builtin={blocked['builtin_tool_called'] or 'none'}"
    )

    if empty["tool_called"] and not empty["builtin_tool_called"]:
        strategy = "empty"
    elif blocked["tool_called"] and not blocked["builtin_tool_called"]:
        strategy = "disallow_builtins"
    else:
        print("模型没有只调用探针工具。服务仍默认使用 disallow_builtins。")
        return 1

    print(f"探针 3/3：{strategy} 下挂起 {wait_seconds:.0f} 秒后交还结果")
    held = await _run_once(
        token=token,
        model=model,
        strategy=strategy,
        wait_seconds=wait_seconds,
    )
    print(
        f"elapsed={held['elapsed']:.1f}s tool_called={held['tool_called']} "
        f"model_continued={held['model_continued']} builtin={held['builtin_tool_called'] or 'none'}"
    )
    if (
        held["tool_called"]
        and held["model_continued"]
        and not held["builtin_tool_called"]
        and held["elapsed"] + 0.5 >= wait_seconds
    ):
        print(f"探针通过。服务默认策略是 disallow_builtins，本次可用策略是 {strategy}。")
        return 0
    print("探针未通过：工具没有按预期挂起，或模型没有在结果返回后继续。")
    return 1
