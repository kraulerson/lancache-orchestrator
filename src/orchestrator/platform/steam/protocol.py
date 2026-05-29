"""Newline-delimited JSON IPC protocol between orchestrator and steam worker.

Locked at the protocol level so all 3 BLs (BL10, BL11, BL12) use the same
surface. See F1 design spec §3.2-3.3.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# Cap any single IPC line at 10 MiB (D20 of F1 spec). Worker is killed
# + restarted if it exceeds; protects orchestrator from malformed JSON
# storms or runaway BLOBs.
MAX_IPC_LINE_BYTES = 10 * 1024 * 1024

KNOWN_OPS: frozenset[str] = frozenset(
    {
        "auth.begin",
        "auth.complete",
        "auth.status",
        "library.enumerate",
        "manifest.fetch",
        "manifest.expand",
        "shutdown",
    }
)


class ProtocolError(Exception):
    """Raised on any IPC protocol violation: malformed JSON, missing
    required field, unknown op, oversized line, etc."""


@dataclass(frozen=True)
class RequestEnvelope:
    msg_id: str
    op: str
    params: dict[str, Any] = field(default_factory=dict)

    def to_line(self) -> str:
        return (
            json.dumps(
                {
                    "msg_id": self.msg_id,
                    "op": self.op,
                    "params": self.params,
                },
                separators=(",", ":"),
            )
            + "\n"
        )

    @classmethod
    def from_line(cls, line: str) -> RequestEnvelope:
        if len(line.encode("utf-8")) > MAX_IPC_LINE_BYTES:
            raise ProtocolError(f"line exceeds {MAX_IPC_LINE_BYTES} bytes")
        try:
            data = json.loads(line)
        except json.JSONDecodeError as e:
            raise ProtocolError(f"malformed JSON: {e}") from e
        if not isinstance(data, dict):
            raise ProtocolError("envelope must be a JSON object")
        if "msg_id" not in data:
            raise ProtocolError("missing msg_id")
        if "op" not in data:
            raise ProtocolError("missing op")
        if data["op"] not in KNOWN_OPS:
            raise ProtocolError(f"unknown op: {data['op']!r}")
        return cls(
            msg_id=str(data["msg_id"]),
            op=str(data["op"]),
            params=dict(data.get("params") or {}),
        )


@dataclass(frozen=True)
class ResponseEnvelope:
    msg_id: str
    ok: bool
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    @classmethod
    def ok_(cls, msg_id: str, result: dict[str, Any]) -> ResponseEnvelope:
        return cls(msg_id=msg_id, ok=True, result=result, error=None)

    @classmethod
    def err(cls, msg_id: str, kind: str, message: str = "") -> ResponseEnvelope:
        return cls(
            msg_id=msg_id,
            ok=False,
            result=None,
            error={"kind": kind, "message": message},
        )

    def to_line(self) -> str:
        payload: dict[str, Any] = {"msg_id": self.msg_id, "ok": self.ok}
        if self.ok and self.result is not None:
            payload["result"] = self.result
        elif not self.ok and self.error is not None:
            payload["error"] = self.error
        return json.dumps(payload, separators=(",", ":")) + "\n"

    @classmethod
    def from_line(cls, line: str) -> ResponseEnvelope:
        if len(line.encode("utf-8")) > MAX_IPC_LINE_BYTES:
            raise ProtocolError(f"line exceeds {MAX_IPC_LINE_BYTES} bytes")
        try:
            data = json.loads(line)
        except json.JSONDecodeError as e:
            raise ProtocolError(f"malformed JSON: {e}") from e
        if not isinstance(data, dict):
            raise ProtocolError("envelope must be a JSON object")
        if "msg_id" not in data or "ok" not in data:
            raise ProtocolError("missing required field")
        return cls(
            msg_id=str(data["msg_id"]),
            ok=bool(data["ok"]),
            result=data.get("result"),
            error=data.get("error"),
        )
