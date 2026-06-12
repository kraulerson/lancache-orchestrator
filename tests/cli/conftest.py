"""Shared CLI test harness: invoke a command with a mock HTTP transport."""

from __future__ import annotations

import httpx
import pytest
from click.testing import CliRunner


@pytest.fixture
def cli_invoke():
    """Invoke the root cli with a mock transport injected into any OrchClient.

    `make_client` looks up `orchestrator.cli.client.OrchClient` at call time, so
    patching that attribute with a transport-bearing subclass routes every
    command's HTTP through the supplied MockTransport (no live network).
    """

    def _invoke(args, *, transport=None, input=None, env=None):
        import orchestrator.cli.client as client_mod
        from orchestrator.cli.main import cli

        orig = client_mod.OrchClient
        if transport is not None:

            class _Patched(orig):
                def __init__(self, *a, **k):
                    super().__init__(*a, **k)
                    self._transport = transport

            client_mod.OrchClient = _Patched
        try:
            return CliRunner().invoke(
                cli, args, input=input, env=env if env is not None else {"ORCH_TOKEN": "t"}
            )
        finally:
            client_mod.OrchClient = orig

    return _invoke


@pytest.fixture
def mock(cli_invoke):
    """Convenience: build a MockTransport from a handler and invoke."""

    def _mock(args, handler, *, input=None, env=None):
        return cli_invoke(args, transport=httpx.MockTransport(handler), input=input, env=env)

    return _mock
