"""ARCH-4 / re-arch ④: the data-plane agent process must not transitively import
the control-plane API entrypoint or the DB pool."""

from __future__ import annotations

import subprocess
import sys


def test_agent_app_does_not_import_api_main_or_pool():
    code = (
        "import orchestrator.agent.app as a\n"
        "_ = a.create_agent_app\n"
        "import sys\n"
        "bad = [m for m in ('orchestrator.api.main', 'orchestrator.db.pool') "
        "if m in sys.modules]\n"
        "print('BAD=' + ','.join(bad))\n"
    )
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout
    assert out.strip() == "BAD=", f"agent pulled in control-plane/DB modules: {out.strip()}"
