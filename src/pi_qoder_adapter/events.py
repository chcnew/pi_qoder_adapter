"""Events produced while a Qoder turn is translated for Pi."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Semantic:
    kind: str
    text: str = ""
    tool_id: str = ""
    tool_name: str = ""
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    tool_input: dict[str, Any] | None = field(default=None)
