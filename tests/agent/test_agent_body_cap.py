"""The agent refuses an absurdly large request body.

UAT-14 #298. The API has capped bodies at 32 KiB since it was written;
``create_agent_app`` never got the middleware, and no agent request model bounds its
collections. The exploratory arm sent a **238 MB** POST to ``/v1/stat``: it returned
200 after **88 seconds**, and the agent's RSS went from 63 MB to 698 MB and stayed
there. One request, against the process sitting on the CPU-constrained NAS beside
lancache.

**The agent's cap cannot be the API's.** The agent legitimately receives very large
bodies: ``/v1/epic/validate`` carries the game's entire manifest, base64-encoded. In
this library the largest is game 15035 at 66 MB raw — about 88 MB on the wire — with
two more at 40 MB and 21 MB against a 1.1 MB average across 922 manifests. Applying
32 KiB here would reject every Epic validation, starting with the three biggest
games. That is a real risk this test exists to lock out: the cap must sit above the
legitimate maximum and below the abuse case.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from orchestrator.agent.app import create_agent_app
from orchestrator.agent.constants import AGENT_BODY_SIZE_CAP_BYTES
from orchestrator.api._constants import BODY_SIZE_CAP_BYTES

# The real numbers from this library, so the sizing is anchored to data rather than
# taste. Raw manifest bytes grow by ~4/3 under base64.
LARGEST_MANIFEST_RAW_BYTES = 66_056_479
LARGEST_MANIFEST_ON_WIRE = int(LARGEST_MANIFEST_RAW_BYTES * 4 / 3)
OBSERVED_ABUSE_BYTES = 238 * 1024 * 1024


class TestTheCapIsSizedForRealTraffic:
    def test_it_admits_the_largest_real_manifest(self) -> None:
        assert AGENT_BODY_SIZE_CAP_BYTES > LARGEST_MANIFEST_ON_WIRE, (
            "an epic validate carries the whole manifest; a cap below the largest real "
            "one silently breaks validation for the biggest games in the library"
        )

    def test_it_leaves_room_for_growth(self) -> None:
        assert AGENT_BODY_SIZE_CAP_BYTES >= LARGEST_MANIFEST_ON_WIRE * 1.4, (
            "manifests grow with game size; a cap that only just fits today's largest "
            "becomes an outage the next time a big game updates"
        )

    def test_it_rejects_the_request_that_caused_this(self) -> None:
        assert AGENT_BODY_SIZE_CAP_BYTES < OBSERVED_ABUSE_BYTES, (
            "the 238 MB POST that held the agent for 88s and permanently grew its RSS "
            "must not be admitted"
        )

    def test_it_is_deliberately_looser_than_the_api_cap(self) -> None:
        """Not a mistake — a service that must accept 88 MB cannot have a tight cap."""
        assert AGENT_BODY_SIZE_CAP_BYTES > BODY_SIZE_CAP_BYTES


class TestTheAgentEnforcesIt:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch) -> TestClient:
        monkeypatch.setenv("ORCH_TOKEN", "t" * 32)
        monkeypatch.setenv("ORCH_AGENT_CACHE_ROOT", str(tmp_path))
        from orchestrator.core.settings import get_settings

        get_settings.cache_clear()
        return TestClient(create_agent_app())

    def test_an_oversized_body_is_refused_with_413(self, client: TestClient) -> None:
        resp = client.post(
            "/v1/stat",
            content=b"x" * (AGENT_BODY_SIZE_CAP_BYTES + 1),
            headers={
                "Authorization": "Bearer " + "t" * 32,
                "Content-Type": "application/json",
            },
        )

        assert resp.status_code == 413, (
            f"expected 413 for a body over the cap, got {resp.status_code}. Without a "
            "cap the agent buffers the whole thing and its memory never comes back."
        )

    def test_an_ordinary_request_is_unaffected(self, client: TestClient) -> None:
        """The cap must not become the reason normal work fails."""
        resp = client.post(
            "/v1/stat",
            json={"paths": ["a/b/c"]},
            headers={"Authorization": "Bearer " + "t" * 32},
        )

        assert resp.status_code != 413, "a small, well-formed request must never be capped"
