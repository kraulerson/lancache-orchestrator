"""Tests for orchestrator.platform.steam.enumerate (#107 + #109 fixes)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from orchestrator.platform.steam.enumerate import (
    DEFAULT_BATCH_SIZE,
    _build_package_request,
    _chunks,
    _extract_app_ids_from_package_info,
    _extract_app_metadata,
    enumerate_apps,
    wait_for_licenses,
)


class _License:
    """Stand-in for `steam.protobuf.CMsgClientLicenseList.License`. Only
    `package_id` and `access_token` are read by the enumeration code."""

    def __init__(self, package_id: int, access_token: int = 0) -> None:
        self.package_id = package_id
        self.access_token = access_token


class _Client:
    """Minimal stand-in for the steam-next SteamClient — `licenses` dict
    and `get_product_info` callable."""

    def __init__(
        self,
        licenses: dict[int, _License] | None = None,
        packages_responses: list[dict[str, Any]] | None = None,
        apps_responses: list[dict[str, Any]] | None = None,
    ) -> None:
        self.licenses: dict[int, _License] = dict(licenses or {})
        self._packages_responses = list(packages_responses or [])
        self._apps_responses = list(apps_responses or [])
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_product_info(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_product_info", kwargs))
        if kwargs.get("packages"):
            if self._packages_responses:
                return self._packages_responses.pop(0)
            return {"packages": {}}
        if kwargs.get("apps"):
            if self._apps_responses:
                return self._apps_responses.pop(0)
            return {"apps": {}}
        return {}


class TestChunks:
    def test_exact_multiple(self):
        assert _chunks([1, 2, 3, 4, 5, 6], 2) == [[1, 2], [3, 4], [5, 6]]

    def test_uneven(self):
        assert _chunks([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]

    def test_smaller_than_chunk(self):
        assert _chunks([1, 2], 5) == [[1, 2]]

    def test_empty(self):
        assert _chunks([], 10) == []

    def test_zero_size_raises(self):
        with pytest.raises(ValueError, match="must be positive"):
            _chunks([1], 0)


class TestWaitForLicenses:
    def test_returns_immediately_when_populated(self):
        c = _Client(licenses={730: _License(730)})
        sleep = MagicMock()
        n = wait_for_licenses(c, timeout=1.0, sleep_fn=sleep)
        assert n == 1
        assert sleep.call_count == 0

    def test_polls_until_populated(self):
        c = _Client(licenses={})
        sleep_calls: list[float] = []

        def fake_sleep(dt: float) -> None:
            sleep_calls.append(dt)
            if len(sleep_calls) == 3:
                c.licenses = {730: _License(730), 440: _License(440)}

        now = [0.0]

        def fake_mono() -> float:
            now[0] += 0.1
            return now[0]

        n = wait_for_licenses(c, timeout=5.0, sleep_fn=fake_sleep, monotonic_fn=fake_mono)
        assert n == 2
        assert sleep_calls == [0.1, 0.1, 0.1]

    def test_timeout_returns_zero(self):
        c = _Client(licenses={})
        now = [0.0]

        def fake_mono() -> float:
            now[0] += 1.0
            return now[0]

        n = wait_for_licenses(c, timeout=2.0, sleep_fn=lambda _dt: None, monotonic_fn=fake_mono)
        assert n == 0

    def test_handles_none_licenses(self):
        c = _Client()
        c.licenses = None  # type: ignore[assignment]
        now = [0.0]

        def fake_mono() -> float:
            now[0] += 10.0
            return now[0]

        n = wait_for_licenses(c, timeout=1.0, sleep_fn=lambda _dt: None, monotonic_fn=fake_mono)
        assert n == 0


class TestBuildPackageRequest:
    def test_includes_access_token_when_nonzero(self):
        licenses = {730: _License(730, access_token=42)}
        out = _build_package_request(licenses)
        assert out == [{"packageid": 730, "access_token": 42}]

    def test_omits_access_token_when_zero(self):
        licenses = {730: _License(730, access_token=0)}
        out = _build_package_request(licenses)
        assert out == [{"packageid": 730}]

    def test_omits_access_token_when_attr_missing(self):
        class _NoToken:
            package_id = 730

        out = _build_package_request({730: _NoToken()})
        assert out == [{"packageid": 730}]

    def test_handles_empty(self):
        assert _build_package_request({}) == []

    def test_multiple_packages(self):
        licenses = {
            730: _License(730, access_token=1),
            440: _License(440, access_token=0),
            570: _License(570, access_token=99),
        }
        out = _build_package_request(licenses)
        # order matches dict iteration; check by package_id
        as_dict = {e["packageid"]: e for e in out}
        assert as_dict[730] == {"packageid": 730, "access_token": 1}
        assert as_dict[440] == {"packageid": 440}
        assert as_dict[570] == {"packageid": 570, "access_token": 99}


class TestExtractAppIdsFromPackageInfo:
    def test_walks_appids_dict(self):
        resp = {
            "packages": {
                "730": {"appids": {"0": 730, "1": 731}},
                "440": {"appids": {"0": 440}},
            }
        }
        out = _extract_app_ids_from_package_info(resp)
        assert sorted(out) == [440, 730, 731]

    def test_dedups(self):
        resp = {
            "packages": {
                "p1": {"appids": {"0": 730}},
                "p2": {"appids": {"0": 730, "1": 731}},
            }
        }
        out = _extract_app_ids_from_package_info(resp)
        assert sorted(out) == [730, 731]

    def test_skips_non_int_appids(self):
        resp = {
            "packages": {
                "730": {"appids": {"0": "not-an-int", "1": 731}},
            }
        }
        out = _extract_app_ids_from_package_info(resp)
        assert out == [731]

    def test_empty_or_none(self):
        assert _extract_app_ids_from_package_info(None) == []
        assert _extract_app_ids_from_package_info({}) == []
        assert _extract_app_ids_from_package_info({"packages": {}}) == []


class TestExtractAppMetadata:
    def test_happy_path(self):
        resp = {
            "apps": {
                730: {
                    "common": {"name": "Counter-Strike 2"},
                    "depots": {"731": {}, "734": {}, "branches": {}},
                },
            }
        }
        out = _extract_app_metadata(resp, [730])
        assert out == [
            {"app_id": 730, "name": "Counter-Strike 2", "depots": [731, 734], "version": None}
        ]

    def test_falls_back_to_string_key(self):
        resp = {"apps": {"730": {"common": {"name": "CS2"}, "depots": {}}}}
        out = _extract_app_metadata(resp, [730])
        assert out == [{"app_id": 730, "name": "CS2", "depots": [], "version": None}]

    def test_skips_missing_common_name(self):
        """Better than synthesizing placeholder names that pollute games table."""
        resp = {"apps": {730: {"common": {}, "depots": {}}}}
        out = _extract_app_metadata(resp, [730])
        assert out == []

    def test_skips_apps_not_in_response(self):
        resp = {"apps": {730: {"common": {"name": "CS2"}, "depots": {}}}}
        out = _extract_app_metadata(resp, [730, 440])
        assert [r["app_id"] for r in out] == [730]

    def test_empty_response(self):
        assert _extract_app_metadata(None, [730]) == []
        assert _extract_app_metadata({}, [730]) == []
        assert _extract_app_metadata({"apps": {}}, [730]) == []


class TestEnumerateApps:
    def test_empty_licenses_returns_empty(self):
        c = _Client(licenses={})
        assert enumerate_apps(c) == []
        assert c.calls == []  # no IPC calls

    def test_happy_path_single_package_single_app(self):
        c = _Client(
            licenses={730: _License(730, access_token=42)},
            packages_responses=[{"packages": {"730": {"appids": {"0": 730}}}}],
            apps_responses=[
                {
                    "apps": {
                        730: {
                            "common": {"name": "Counter-Strike 2"},
                            "depots": {"731": {}, "734": {}},
                        }
                    }
                }
            ],
        )
        out = enumerate_apps(c)
        assert out == [
            {"app_id": 730, "name": "Counter-Strike 2", "depots": [731, 734], "version": None}
        ]
        # Verify the package call passes access token and auto_access_tokens=False
        pkg_call = next(kwargs for op, kwargs in c.calls if "packages" in kwargs)
        assert pkg_call["packages"] == [{"packageid": 730, "access_token": 42}]
        assert pkg_call["auto_access_tokens"] is False

    def test_batches_packages_into_chunks(self):
        # 125 packages with batch_size=50 → 3 calls (50 + 50 + 25)
        licenses = {pid: _License(pid, access_token=pid) for pid in range(1, 126)}
        # Each batch response yields one app per package
        packages_responses = []
        for batch_start in (1, 51, 101):
            batch_end = min(batch_start + 50, 126)
            pkgs = {
                str(pid): {"appids": {"0": pid + 10000}} for pid in range(batch_start, batch_end)
            }
            packages_responses.append({"packages": pkgs})

        # Apps response: cover all 125 app_ids; single batch since they're all distinct
        all_app_ids = list(range(10001, 10126))
        # 125 apps with batch_size=50 → also 3 calls
        apps_responses = []
        for batch_start in (10001, 10051, 10101):
            batch_end = min(batch_start + 50, 10126)
            apps = {
                aid: {"common": {"name": f"App {aid}"}, "depots": {}}
                for aid in range(batch_start, batch_end)
            }
            apps_responses.append({"apps": apps})

        c = _Client(
            licenses=licenses,
            packages_responses=packages_responses,
            apps_responses=apps_responses,
        )
        out = enumerate_apps(c, batch_size=50)
        assert len(out) == 125
        assert {r["app_id"] for r in out} == set(all_app_ids)

        # 3 package calls + 3 app calls = 6 IPC round trips
        assert len(c.calls) == 6

    def test_dedups_app_ids_across_packages(self):
        c = _Client(
            licenses={
                100: _License(100, access_token=1),
                200: _License(200, access_token=2),
            },
            packages_responses=[
                {
                    "packages": {
                        "100": {"appids": {"0": 730}},
                        "200": {"appids": {"0": 730}},  # same app via different package
                    }
                }
            ],
            apps_responses=[{"apps": {730: {"common": {"name": "CS2"}, "depots": {}}}}],
        )
        out = enumerate_apps(c)
        assert len(out) == 1
        assert out[0]["app_id"] == 730
        # Single app call requested only the deduped id
        app_call = next(kwargs for op, kwargs in c.calls if "apps" in kwargs)
        assert app_call["apps"] == [730]

    def test_no_apps_in_packages_returns_empty(self):
        c = _Client(
            licenses={730: _License(730)},
            packages_responses=[{"packages": {}}],
        )
        out = enumerate_apps(c)
        assert out == []

    def test_apps_response_missing_some_doesnt_break(self):
        # Request 3 app_ids; response has only 2
        c = _Client(
            licenses={
                100: _License(100),
                200: _License(200),
                300: _License(300),
            },
            packages_responses=[
                {
                    "packages": {
                        "100": {"appids": {"0": 1}},
                        "200": {"appids": {"0": 2}},
                        "300": {"appids": {"0": 3}},
                    }
                }
            ],
            apps_responses=[
                {
                    "apps": {
                        1: {"common": {"name": "App One"}, "depots": {}},
                        # 2 missing — perhaps access denied
                        3: {"common": {"name": "App Three"}, "depots": {}},
                    }
                }
            ],
        )
        out = enumerate_apps(c)
        ids = {r["app_id"] for r in out}
        assert ids == {1, 3}

    def test_default_batch_size_constant(self):
        # Sanity: 50 packages per call balances Steam's 15s job timeout vs round-trip count
        assert DEFAULT_BATCH_SIZE == 50


class TestManifestGidExtraction:
    """UAT-9: steam-next 1.4.4 chokes on the dict-form manifests[branch]
    entry. Our dict-aware extractor must handle both forms."""

    def test_legacy_string_gid(self):
        from orchestrator.platform.steam.enumerate import extract_manifest_gid

        assert extract_manifest_gid("7611933945298954112") == 7611933945298954112

    def test_dict_form_gid(self):
        from orchestrator.platform.steam.enumerate import extract_manifest_gid

        entry = {"gid": "7611933945298954112", "size": "123", "download": "45"}
        assert extract_manifest_gid(entry) == 7611933945298954112

    def test_none_and_unparseable(self):
        from orchestrator.platform.steam.enumerate import extract_manifest_gid

        assert extract_manifest_gid(None) is None
        assert extract_manifest_gid({}) is None
        assert extract_manifest_gid({"gid": None}) is None
        assert extract_manifest_gid("not-a-number") is None

    def test_manifest_gids_for_app_filters_and_extracts(self):
        from orchestrator.platform.steam.enumerate import manifest_gids_for_app

        depots = {
            "branches": {"public": {"buildid": "1"}},  # non-depot key, skipped
            "baselanguages": "english",  # non-depot, skipped
            "529341": {"manifests": {"public": {"gid": "100", "size": "1"}}},
            "529345": {"manifests": {"public": "200"}},  # legacy string form
            "529346": {"manifests": {"beta": {"gid": "300"}}},  # no public, skipped
            "529347": {"depotfromapp": "228980"},  # shared depot, no manifests
        }
        result = manifest_gids_for_app(depots, "public")
        assert sorted(result) == [(529341, 100), (529345, 200)]

    def test_manifest_gids_for_app_non_dict(self):
        from orchestrator.platform.steam.enumerate import manifest_gids_for_app

        assert manifest_gids_for_app(None) == []
        assert manifest_gids_for_app([]) == []


class TestAppVersionToken:
    def test_prefers_public_branch_buildid(self):
        from orchestrator.platform.steam.enumerate import _app_version_token

        depots = {"branches": {"public": {"buildid": "1788499"}}, "1234": {"manifests": {}}}
        assert _app_version_token(depots) == "1788499"

    def test_composite_when_no_buildid(self):
        from orchestrator.platform.steam.enumerate import _app_version_token

        depots = {
            "1234": {"manifests": {"public": {"gid": "555"}}},
            "1200": {"manifests": {"public": {"gid": "777"}}},
        }
        tok = _app_version_token(depots)
        # order-independent: same pairs -> same token
        assert tok == _app_version_token(
            {
                "1200": {"manifests": {"public": {"gid": "777"}}},
                "1234": {"manifests": {"public": {"gid": "555"}}},
            }
        )
        assert tok is not None and tok != "1788499"

    def test_none_when_no_version_info(self):
        from orchestrator.platform.steam.enumerate import _app_version_token

        assert _app_version_token({"branches": {"public": {}}}) is None
        assert _app_version_token(None) is None

    def test_extract_app_metadata_includes_version(self):
        apps_response = {
            "apps": {
                "10": {
                    "common": {"name": "Game"},
                    "depots": {"branches": {"public": {"buildid": "42"}}, "11": {"manifests": {}}},
                }
            }
        }
        out = _extract_app_metadata(apps_response, [10])
        assert out == [{"app_id": 10, "name": "Game", "depots": [11], "version": "42"}]
