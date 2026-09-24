"""Encode adapter events as OpenAI Chat Completions responses."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from pi_qoder_adapter.events import Semantic


def sse(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


def sse_done() -> bytes:
    return b"data: [DONE]\n\n"


@dataclass
class OpenAIEncoder:
    completion_id: str
    model: str
    created: int = field(default_factory=lambda: int(time.time()))
    sent_role: bool = False
    tool_indexes: dict[str, int] = field(default_factory=dict)
    next_tool_index: int = 0
    content: str = ""
    reasoning: str = ""
    tool_calls: dict[str, dict[str, str]] = field(default_factory=dict)
    usage: dict[str, Any] | None = None
    finish_reason: str = "stop"

    def _chunk(self, delta: dict[str, Any], finish_reason: str | None = None, usage: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.completion_id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": finish_reason,
                }
            ],
        }
        if usage is not None:
            payload["usage"] = usage
        return payload

    def _with_role(self, delta: dict[str, Any]) -> dict[str, Any]:
        if not self.sent_role:
            delta = {"role": "assistant", **delta}
            self.sent_role = True
        return delta

    def chunks(self, event: Semantic) -> list[dict[str, Any]]:
        if event.kind in {"text", "error"} and event.text:
            self.content += event.text
            return [self._chunk(self._with_role({"content": event.text}))]
        if event.kind == "thinking" and event.text:
            self.reasoning += event.text
            return [self._chunk(self._with_role({"reasoning_content": event.text}))]
        if event.kind == "tool_start":
            index = self.tool_indexes.setdefault(event.tool_id, self.next_tool_index)
            if index == self.next_tool_index:
                self.next_tool_index += 1
            arguments = event.text or ""
            self.tool_calls[event.tool_id] = {"name": event.tool_name, "arguments": arguments}
            return [
                self._chunk(
                    self._with_role(
                        {
                            "tool_calls": [
                                {
                                    "index": index,
                                    "id": event.tool_id,
                                    "type": "function",
                                    "function": {"name": event.tool_name, "arguments": arguments},
                                }
                            ]
                        }
                    )
                )
            ]
        if event.kind == "tool_args" and event.text:
            index = self.tool_indexes.get(event.tool_id)
            if index is None:
                return []
            bucket = self.tool_calls.setdefault(event.tool_id, {"name": event.tool_name, "arguments": ""})
            bucket["arguments"] += event.text
            return [
                self._chunk(
                    {
                        "tool_calls": [
                            {
                                "index": index,
                                "function": {"arguments": event.text},
                            }
                        ]
                    }
                )
            ]
        if event.kind == "usage" and event.usage:
            self.usage = event.usage
            return []
        if event.kind == "finish":
            self.finish_reason = event.finish_reason or "stop"
            return [self._chunk({}, finish_reason=self.finish_reason, usage=self.usage)]
        return []

    def completion(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.content or None}
        if self.reasoning:
            message["reasoning_content"] = self.reasoning
        if self.tool_calls:
            ordered = sorted(self.tool_indexes.items(), key=lambda item: item[1])
            message["tool_calls"] = [
                {
                    "id": tool_id,
                    "type": "function",
                    "function": {
                        "name": self.tool_calls[tool_id]["name"],
                        "arguments": self.tool_calls[tool_id]["arguments"],
                    },
                }
                for tool_id, _index in ordered
                if tool_id in self.tool_calls
            ]
            if not self.content:
                message["content"] = None
        payload: dict[str, Any] = {
            "id": self.completion_id,
            "object": "chat.completion",
            "created": self.created,
            "model": self.model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": self.finish_reason,
                }
            ],
        }
        if self.usage is not None:
            payload["usage"] = self.usage
        return payload
