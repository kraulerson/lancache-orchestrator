"""uvicorn entrypoint for the data-plane agent: `python -m orchestrator.agent`."""

from __future__ import annotations

import uvicorn

from orchestrator.agent.app import _enforce_agent_lan_bind_policy, create_agent_app
from orchestrator.core.settings import get_settings


def main() -> None:
    settings = get_settings()
    _enforce_agent_lan_bind_policy(settings)
    app = create_agent_app(settings=settings)
    uvicorn.run(app, host=settings.agent_bind_host, port=settings.agent_bind_port)


if __name__ == "__main__":
    main()
