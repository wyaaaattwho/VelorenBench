"""SDK-independent values at the existing common player-tool seam."""
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class Invocation:
    session_id: str
    tool_call_id: str
    tool_name: str
    arguments: Any


@dataclass(frozen=True)
class Image:
    data: bytes
    mime_type: str
    sha256: str
    width: int
    height: int


@dataclass(frozen=True)
class Result:
    text_result_for_llm: str
    result_type: str = "success"
    error: str | None = None
    images: tuple[Image, ...] = ()


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[Invocation], Awaitable[Result]]
