"""Steam library enumeration helpers (post-UAT-6).

Pure functions extracted from worker.py so they can be unit-tested
without the gevent monkey-patch that worker.py applies at module load.
The worker subprocess imports + delegates to these functions; the
orchestrator process never executes this path (and never imports steam-
next), but the module itself is plain Python so the test suite can
exercise it freely.

Two SEV-2 bugs from UAT-6 (issues #107 and #109) are addressed here:

- **#107** — `client.licenses` is `dict[int -> License]` populated
  asynchronously by `EMsg.ClientLicenseList`. The previous code
  iterated the dict directly (yielding keys, not entries) and didn't
  wait for the message — always producing zero apps. `wait_for_licenses`
  + `enumerate_apps` together fix both halves.

- **#109** — `client.get_product_info(packages=N)` exceeds Steam's
  CM job timeout (15 s default) for real-size libraries. `enumerate_apps`
  chunks both the package and app calls into batches of 50, AND passes
  package access tokens explicitly (skipping the
  `auto_access_tokens=True` round trip that would re-fetch tokens we
  already have on `licenses[pid].access_token`).
"""

from __future__ import annotations

import hashlib
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

# Per-call chunk size for get_product_info(packages=) and (apps=). Empirically,
# Steam's CM handles ~50 ids per call comfortably within its 15 s job timeout.
# Smaller chunks add round-trip latency; bigger ones risk per-call timeout.
DEFAULT_BATCH_SIZE = 50


def _chunks(seq: Sequence[Any], size: int) -> list[list[Any]]:
    """Slice ``seq`` into successive lists of at most ``size`` items."""
    if size <= 0:
        raise ValueError("size must be positive")
    return [list(seq[i : i + size]) for i in range(0, len(seq), size)]


def wait_for_licenses(
    client: Any,
    *,
    timeout: float = 10.0,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> int:
    """Block (gevent-friendly in production) until ``client.licenses`` has at
    least one entry, or until ``timeout`` seconds have elapsed. Returns the
    observed count (which may still be zero on timeout).

    The polling approach is preferred over ``client.wait_event(EMsg.ClientLicenseList)``
    because the license message may have ALREADY arrived between login
    and our call — ``wait_event`` would then block waiting for the NEXT
    one, hanging indefinitely.

    ``sleep_fn`` and ``monotonic_fn`` are injected for deterministic tests.
    """
    deadline = monotonic_fn() + timeout
    while monotonic_fn() < deadline:
        if client.licenses:
            return len(client.licenses)
        sleep_fn(0.1)
    return len(client.licenses or {})


def _build_package_request(
    licenses: dict[int, Any],
) -> list[dict[str, Any]]:
    """Convert ``client.licenses`` into the ``packages=`` list shape that
    ``get_product_info`` accepts, including access tokens so the
    ``auto_access_tokens`` round-trip can be skipped for packages.

    Token of zero (or missing) is omitted; Steam treats that as "no token
    required."
    """
    request: list[dict[str, Any]] = []
    for pkg_id, lic in licenses.items():
        entry: dict[str, Any] = {"packageid": int(pkg_id)}
        access_token = getattr(lic, "access_token", 0) or 0
        if access_token:
            entry["access_token"] = int(access_token)
        request.append(entry)
    return request


def _extract_app_ids_from_package_info(
    packages_response: dict[str, Any] | None,
) -> list[int]:
    """Walk a ``get_product_info`` ``packages={...}`` response → flat list
    of distinct app_ids. Order-preserving via ``dict.fromkeys``."""
    if not packages_response:
        return []
    packages = packages_response.get("packages", {}) or {}
    collected: list[int] = []
    for pkg_info in packages.values():
        appid_map = (pkg_info or {}).get("appids", {}) or {}
        for app_id_val in appid_map.values():
            try:
                collected.append(int(app_id_val))
            except (TypeError, ValueError):
                continue
    # de-dup while preserving order
    return list(dict.fromkeys(collected))


def _extract_app_metadata(
    apps_response: dict[str, Any] | None,
    requested_app_ids: list[int],
) -> list[dict[str, Any]]:
    """Walk a ``get_product_info`` ``apps={...}`` response → list of
    ``{app_id, name, depots}`` dicts. Apps missing from the response
    (e.g., access denied) are skipped silently rather than emitting
    placeholders — UAT-6 found that the previous code synthesized
    ``f"app_{app_id}"`` names which polluted the games table."""
    if not apps_response:
        return []
    apps_info = apps_response.get("apps", {}) or {}
    out: list[dict[str, Any]] = []
    for app_id in requested_app_ids:
        # steam-next sometimes keys by int, sometimes by str — check both.
        info = apps_info.get(app_id) or apps_info.get(str(app_id))
        if not info:
            continue
        common = info.get("common", {}) or {}
        name = common.get("name")
        if not isinstance(name, str) or not name:
            # Skip apps without a real common.name — better than polluting
            # the games table with synthetic placeholders.
            continue
        depots_dict = info.get("depots", {}) or {}
        depot_ids: list[int] = []
        for depot_key in depots_dict:
            key_str = str(depot_key)
            if key_str.isdigit():
                depot_ids.append(int(key_str))
        out.append(
            {
                "app_id": int(app_id),
                "name": name,
                "depots": depot_ids,
                "version": _app_version_token(depots_dict),
            }
        )
    return out


def enumerate_apps(
    client: Any,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> list[dict[str, Any]]:
    """Enumerate owned apps from a steam-next-like client.

    Reads ``client.licenses`` (already populated — caller should
    ``wait_for_licenses`` first), batches the ``get_product_info``
    calls per #109's batching requirement, and assembles the
    ``{app_id, name, depots}`` records BL11 ships into the games table.

    Returns an empty list rather than raising on empty licenses — the
    handler treats "no apps" as a successful enumeration with zero
    upserts.
    """
    licenses = client.licenses or {}
    if not licenses:
        return []

    package_request = _build_package_request(licenses)
    candidate_app_ids: list[int] = []
    for batch in _chunks(package_request, batch_size):
        resp = client.get_product_info(packages=batch, auto_access_tokens=False)
        candidate_app_ids.extend(_extract_app_ids_from_package_info(resp))

    # de-dup again across batches
    unique_app_ids = list(dict.fromkeys(candidate_app_ids))
    if not unique_app_ids:
        return []

    out: list[dict[str, Any]] = []
    for batch in _chunks(unique_app_ids, batch_size):
        resp = client.get_product_info(apps=batch)
        out.extend(_extract_app_metadata(resp, batch))
    return out


def extract_manifest_gid(entry: Any) -> int | None:
    """Extract a manifest gid from a depot's ``manifests[branch]`` entry.

    Steam-next 1.4.4's ``CDNClient.get_manifests`` assumes this entry is a
    bare gid string and does ``int(entry)``. Current Steam returns it as a
    dict — ``{"gid": "...", "size": "...", "download": "..."}`` — so the
    library raises ``int() argument ... not 'dict'`` (surfaced live in
    UAT-9). Handle both the legacy string form and the dict form. Returns
    the gid as int, or ``None`` if absent/unparseable.
    """
    if isinstance(entry, dict):
        entry = entry.get("gid")
    if entry is None:
        return None
    try:
        return int(entry)
    except (TypeError, ValueError):
        return None


def manifest_gids_for_app(depots: Any, branch: str = "public") -> list[tuple[int, int]]:
    """From ``CDNClient.get_app_depot_info()`` output, return
    ``[(depot_id, manifest_gid)]`` for depots that publish a manifest on
    ``branch``.

    Skips non-depot keys (``branches``, ``baselanguages``, etc.), depots
    with no ``manifests`` mapping (e.g. ``depotfromapp`` shared depots), and
    entries with no/unparseable gid for the branch. Pure — no network — so
    it is unit-testable without a Steam session.
    """
    out: list[tuple[int, int]] = []
    if not isinstance(depots, dict):
        return out
    for key, info in depots.items():
        if not str(key).isdigit() or not isinstance(info, dict):
            continue
        manifests = info.get("manifests")
        if not isinstance(manifests, dict):
            continue
        gid = extract_manifest_gid(manifests.get(branch))
        if gid is None:
            continue
        out.append((int(key), gid))
    return out


def _app_version_token(depots: Any) -> str | None:
    """A stable per-app version string for change detection (F8).

    Prefers the public-branch ``buildid`` (changes on every app update); falls
    back to a SHA-256 of the sorted ``(depot_id, manifest_gid)`` pairs so any
    depot manifest change shifts the token. Returns ``None`` when neither is
    available — the game is then treated as needing a prefill.
    """
    if isinstance(depots, dict):
        buildid = ((depots.get("branches") or {}).get("public") or {}).get("buildid")
        if buildid is not None and str(buildid):
            return str(buildid)
    pairs = manifest_gids_for_app(depots, "public")
    if not pairs:
        return None
    joined = ",".join(f"{d}:{g}" for d, g in sorted(pairs))
    return hashlib.sha256(joined.encode()).hexdigest()
