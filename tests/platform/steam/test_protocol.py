"""Tests for the Steam worker IPC protocol (BL10 / F1)."""

from __future__ import annotations

import json

import pytest


class TestRequestEnvelope:
    def test_encode_request(self):
        from orchestrator.platform.steam.protocol import RequestEnvelope

        msg_id = "550e8400-e29b-41d4-a716-446655440000"
        req = RequestEnvelope(
            msg_id=msg_id, op="auth.begin", params={"username": "u", "password": "p"}
        )
        line = req.to_line()
        # Single line ending in \n
        assert line.endswith("\n")
        assert "\n" not in line[:-1]
        # Round-trip
        parsed = json.loads(line)
        assert parsed["msg_id"] == msg_id
        assert parsed["op"] == "auth.begin"
        assert parsed["params"] == {"username": "u", "password": "p"}

    def test_decode_request(self):
        from orchestrator.platform.steam.protocol import RequestEnvelope

        line = '{"msg_id": "abc", "op": "auth.status", "params": {}}\n'
        req = RequestEnvelope.from_line(line)
        assert req.msg_id == "abc"
        assert req.op == "auth.status"
        assert req.params == {}

    def test_decode_rejects_missing_msg_id(self):
        from orchestrator.platform.steam.protocol import ProtocolError, RequestEnvelope

        with pytest.raises(ProtocolError, match=r"msg_id"):
            RequestEnvelope.from_line('{"op": "auth.status", "params": {}}\n')

    def test_decode_rejects_unknown_op(self):
        from orchestrator.platform.steam.protocol import ProtocolError, RequestEnvelope

        with pytest.raises(ProtocolError, match=r"unknown op"):
            RequestEnvelope.from_line('{"msg_id": "x", "op": "evil.do_a_thing", "params": {}}\n')


class TestResponseEnvelope:
    def test_encode_success(self):
        from orchestrator.platform.steam.protocol import ResponseEnvelope

        resp = ResponseEnvelope.ok_(
            msg_id="x",
            result={"authenticated": True, "steam_id": 76561198000000000},
        )
        line = resp.to_line()
        parsed = json.loads(line)
        assert parsed["ok"] is True
        assert parsed["result"]["steam_id"] == 76561198000000000
        assert "error" not in parsed

    def test_encode_failure(self):
        from orchestrator.platform.steam.protocol import ResponseEnvelope

        resp = ResponseEnvelope.err(
            msg_id="x", kind="TwoFactorCodeMismatch", message="code did not match"
        )
        parsed = json.loads(resp.to_line())
        assert parsed["ok"] is False
        assert parsed["error"]["kind"] == "TwoFactorCodeMismatch"

    def test_decode_success(self):
        from orchestrator.platform.steam.protocol import ResponseEnvelope

        resp = ResponseEnvelope.from_line('{"msg_id": "x", "ok": true, "result": {"a": 1}}\n')
        assert resp.ok is True
        assert resp.result == {"a": 1}
        assert resp.error is None


class TestMaxLineSize:
    def test_oversized_line_rejected(self):
        from orchestrator.platform.steam.protocol import (
            MAX_IPC_LINE_BYTES,
            ProtocolError,
            ResponseEnvelope,
        )

        big = "x" * (MAX_IPC_LINE_BYTES + 1)
        with pytest.raises(ProtocolError, match=r"line exceeds.*bytes"):
            ResponseEnvelope.from_line(
                '{"msg_id": "x", "ok": true, "result": {"data": "' + big + '"}}\n'
            )
