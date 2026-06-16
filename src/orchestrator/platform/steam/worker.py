"""Steam worker subprocess — gevent-patched, runs steam-next.

This module is launched as a subprocess by SteamWorkerClient. Its first
line MUST be the gevent monkey-patch — steam-next requires this BEFORE
any other imports.

The orchestrator process NEVER imports this module. Only the subprocess
runs it (`python -u -m orchestrator.platform.steam.worker`).
"""

# gevent monkey-patch — keep this as the FIRST line of executable code.
# Importing this module from the orchestrator's asyncio process would
# corrupt the asyncio loop. SteamWorkerClient.start() invokes us via
# subprocess.exec; nothing else should import us.
from steam import monkey  # type: ignore[import-not-found]

monkey.patch_minimal()

import contextlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
import uuid  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, TypedDict  # noqa: E402

# F-UAT6-2: worker reads the credential dir from env (set by orchestrator
# client.start()). Falls back to the BL10 default. The orchestrator's
# Settings.steam_session_dir authoritatively configures this; this
# constant exists only as the safety net if the env var isn't forwarded.
_DEFAULT_SESSION_DIR = "/var/lib/orchestrator/steam_session"

# Steam-next imports (post-monkey-patch).
# gevent.Timeout subclasses BaseException (by design, so a stack-unwinding
# timeout can't be swallowed by a bare `except Exception`). steam-next's CDN/CM
# calls raise it when a depot is slow; left uncaught it escapes every handler and
# kills the worker process (UAT-11: a 15s CDN timeout → steam_worker.died
# reason=stdout_closed). We catch it explicitly. Available post-monkey-patch.
from gevent import Timeout as GeventTimeout  # noqa: E402
from steam.client import SteamClient  # type: ignore[import-not-found]  # noqa: E402
from steam.enums import EResult  # type: ignore[import-not-found]  # noqa: E402
from steam.exceptions import SteamError  # type: ignore[import-not-found]  # noqa: E402

# Pure-Python enumeration helpers (testable without gevent monkey-patch).
from orchestrator.platform.steam import enumerate as enumerate_module  # noqa: E402

# #122: genuine auth-loss EResults. steam-next's CDN calls raise SteamError with
# an `.eresult`; mapping these to kind=NotAuthenticated lets the orchestrator flip
# platforms.auth_status='expired'. Transient results (notably EResult.Timeout from
# a slow/dropped CM, and Fail) are deliberately EXCLUDED so a network blip stays a
# retryable SteamAPIError and never forces a needless 2FA re-auth (the design fix
# for the pulled connected/logged_on socket-proxy — adversarial-review SEV-2).
#
# `_SESSION_LOST_ERESULTS` are UNAMBIGUOUS session-wide losses: if one surfaces on
# a per-depot call it means the whole session died, so we fail the fetch (vs. a
# false-partial success). `AccessDenied` is ambiguous on a per-depot call (the
# common "depot not owned" case is indistinguishable), so it only counts as auth
# loss on the app-wide enumeration path.
_SESSION_LOST_ERESULTS = frozenset(
    {EResult.NotLoggedOn, EResult.LoggedInElsewhere, EResult.Expired, EResult.Revoked}
)
_AUTH_LOSS_ERESULTS = _SESSION_LOST_ERESULTS | frozenset({EResult.AccessDenied})

# In-memory state for the worker's lifetime.
_client: SteamClient | None = None
# BL12: CDNClient instance, constructed lazily on first manifest.fetch.
# Holds a reference to _client + a content-server list, a thread pool,
# and a requests session — non-trivial init, so we don't create it
# eagerly. None until first use; reused across subsequent calls.
_cdn_client: Any = None


class _Challenge(TypedDict):
    username: str
    password: str
    expires_at: float


_challenges: dict[str, _Challenge] = {}  # challenge_id -> {username, password, expires_at}


def _send(payload: dict[str, Any]) -> None:
    """Write a single JSON response line to stdout."""
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _ok(msg_id: str, result: dict[str, Any]) -> None:
    _send({"msg_id": msg_id, "ok": True, "result": result})


def _err(msg_id: str, kind: str, message: str = "") -> None:
    _send({"msg_id": msg_id, "ok": False, "error": {"kind": kind, "message": message}})


def _blob_temp_path(prefix: str) -> Path:
    """A unique temp-file path on the shared container FS for a manifest BLOB.

    The worker and orchestrator run in the same container, so a path under
    the system temp dir is readable by both. Used to hand off manifest BLOBs
    out-of-band (S2-1) instead of stuffing them into the IPC line.
    """
    tmp_dir = Path(tempfile.gettempdir())
    return tmp_dir / f"orch-{prefix}-{uuid.uuid4().hex}.zst"


def _ensure_client(credential_dir: str | None = None) -> SteamClient:
    global _client
    if _client is None:
        # F-UAT6-2: pull from env (set by orchestrator client.start()) so
        # operator-customized ORCH_STEAM_SESSION_DIR actually wires through.
        # Argument override is for tests that want to inject a tmp_path.
        path = credential_dir or os.environ.get("ORCH_STEAM_SESSION_DIR", _DEFAULT_SESSION_DIR)
        _client = SteamClient()
        # The credential dir holds the long-lived Steam refresh token (account
        # access without 2FA). Create it 0700 — mkdir's mode is masked by umask,
        # so chmod explicitly to guarantee it is not world-traversable
        # (audit 2026-06-09).
        Path(path).mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(OSError):
            os.chmod(path, 0o700)
        _client.set_credential_location(path)
    return _client


def _sweep_expired_challenges() -> None:
    """Evict expired 2FA challenges so an abandoned flow's cleartext password
    does not linger in worker memory past its TTL (audit 2026-06-09). Mirrors the
    orchestrator-side sweep; without it an orphaned challenge_id is never
    revisited and its password lives for the whole process lifetime."""
    now = time.time()
    for cid in [c for c, ch in _challenges.items() if now > ch["expires_at"]]:
        _challenges.pop(cid, None)


def _handle_auth_begin(msg_id: str, params: dict[str, str]) -> None:
    _sweep_expired_challenges()  # don't let abandoned-flow passwords linger
    username = params.get("username")
    password = params.get("password")
    if not username or not password:
        _err(msg_id, "InvalidCredentials", "missing username or password")
        return

    client = _ensure_client()
    try:
        result = client.login(username, password)
    except Exception as e:  # surface any steam-next exception as IPC error
        _err(msg_id, "SteamAPIError", str(e)[:200])
        return

    if result == EResult.OK:
        _ok(
            msg_id,
            {
                "authenticated": True,
                "steam_id": int(client.steam_id) if client.steam_id else 0,
                "licenses_count": len(client.licenses) if hasattr(client, "licenses") else 0,
            },
        )
        return

    if result in (EResult.AccountLoginDeniedNeedTwoFactor, EResult.AccountLogonDenied):
        challenge_type = (
            "mobile_authenticator"
            if result == EResult.AccountLoginDeniedNeedTwoFactor
            else "email_code"
        )
        challenge_id = str(uuid.uuid4())
        _challenges[challenge_id] = {
            "username": username,
            "password": password,
            "expires_at": time.time() + 300,  # 5 min
        }
        _ok(
            msg_id,
            {
                "authenticated": False,
                "challenge_id": challenge_id,
                "challenge_type": challenge_type,
            },
        )
        return

    _err(msg_id, "InvalidCredentials", f"steam returned {result!r}")


def _handle_auth_complete(msg_id: str, params: dict[str, str]) -> None:
    challenge_id = params.get("challenge_id", "")
    code = params.get("code", "")
    challenge = _challenges.get(challenge_id)
    if challenge is None:
        _err(msg_id, "ChallengeExpired", "no such challenge_id")
        return
    if time.time() > challenge["expires_at"]:
        _challenges.pop(challenge_id, None)
        _err(msg_id, "ChallengeExpired", "challenge expired")
        return

    client = _ensure_client()
    # steam-next picks two_factor_code vs auth_code based on the challenge type;
    # we pass both and let steam-next ignore the irrelevant one (matches Spike A).
    try:
        result = client.login(
            challenge["username"],
            challenge["password"],
            two_factor_code=code,
            auth_code=code,
        )
    except Exception as e:
        _challenges.pop(challenge_id, None)
        _err(msg_id, "SteamAPIError", str(e)[:200])
        return

    _challenges.pop(challenge_id, None)
    if result == EResult.OK:
        _ok(
            msg_id,
            {
                "authenticated": True,
                "steam_id": int(client.steam_id) if client.steam_id else 0,
                "licenses_count": len(client.licenses) if hasattr(client, "licenses") else 0,
            },
        )
        return
    _err(msg_id, "TwoFactorCodeMismatch", f"steam returned {result!r}")


def _handle_auth_status(msg_id: str, _params: dict[str, str]) -> None:
    global _client
    authenticated = _client is not None and _client.connected and _client.logged_on
    payload: dict[str, str | bool | int] = {
        "authenticated": authenticated,
        "last_check_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if authenticated and _client is not None and _client.steam_id:
        payload["steam_id"] = int(_client.steam_id)
    _ok(msg_id, payload)


def _handle_library_enumerate(msg_id: str, _params: dict[str, str]) -> None:
    """Enumerate the operator's owned Steam apps (BL11, post-spike-a2).

    Delegates to `enumerate.enumerate_apps` (pure, unit-testable). Waits
    up to 10s for `client.licenses` to populate after login — Steam
    sends `EMsg.ClientLicenseList` asynchronously and the dict is empty
    in the moments after `login()` returns. See spike_a2_steam_modern.md
    for the API mapping that UAT-6 surfaced.
    """
    global _client
    if _client is None or not _client.connected or not _client.logged_on:
        _err(msg_id, "NotAuthenticated", "no logged-in steam session")
        return

    try:
        # Wait for the asynchronous license list to arrive before enumerating.
        # The wait is configurable and defaults well above the old hardcoded 10s
        # (audit 2026-06-09): a slow CM connection could lag past 10s, leaving
        # client.licenses empty and yielding a false "empty library" success.
        wait_sec = float(os.environ.get("ORCH_STEAM_LICENSE_WAIT_SEC", "60"))
        count = enumerate_module.wait_for_licenses(_client, timeout=wait_sec)
        if count == 0:
            # Distinguish "licenses never arrived" from a real enumeration:
            # signal a retryable timeout rather than recording a green zero-game
            # sync that silently drops the operator's whole library.
            _err(
                msg_id,
                "LicenseListTimeout",
                f"no licenses populated within {wait_sec}s — retry",
            )
            return
        apps = enumerate_module.enumerate_apps(_client)
        _ok(msg_id, {"apps": apps})
    except Exception as e:
        _err(msg_id, "SteamAPIError", str(e)[:200])


def _handle_manifest_fetch(msg_id: str, params: dict[str, str]) -> None:
    """Fetch ALL depot manifests for a Steam app (BL12, post-spike-a3).

    Constructs a CDNClient lazily, calls `cdn.get_manifests(app_id)`,
    extracts {depot_id, manifest_gid, total_bytes, chunk_count, raw_path}
    per manifest, returns the list.

    Per spike-a3:
    - `CDNDepotManifest.cdn_client` back-reference prevents pickle —
      use `.serialize()` to get raw protobuf bytes instead.
    - zstd-level-3 compresses the protobuf; base64 over JSON IPC.

    Live steam-next interaction is validated during UAT-9; unit tests
    exercise the orchestrator-side handler via a stub.
    """
    global _client, _cdn_client
    if _client is None or not _client.connected or not _client.logged_on:
        _err(msg_id, "NotAuthenticated", "no logged-in steam session")
        return

    app_id_raw = params.get("app_id")
    if app_id_raw is None:
        _err(msg_id, "InvalidArgument", "manifest.fetch requires app_id")
        return
    try:
        app_id = int(app_id_raw)
    except (TypeError, ValueError):
        _err(msg_id, "InvalidArgument", f"app_id {app_id_raw!r} is not an integer")
        return

    try:
        # Steam-next imports inside the handler so module-import time
        # doesn't trigger any CDN-related network calls or thread-pool
        # allocation — they happen only when manifest.fetch is invoked.
        import zstandard as _zstd  # type: ignore[import-not-found]
        from steam.client.cdn import CDNClient  # type: ignore[import-not-found]

        if _cdn_client is None:
            _cdn_client = CDNClient(_client)

        # NOTE (UAT-9): we deliberately do NOT call CDNClient.get_manifests().
        # steam-next 1.4.4 assumes depot_info["manifests"][branch] is a bare
        # gid string and does int() on it; current Steam returns a dict
        # ({"gid","size","download"}), so the library raises
        # "int() argument ... not 'dict'". We replicate the enumeration with
        # a dict-aware gid extractor (enumerate.manifest_gids_for_app) and
        # fetch each depot manifest ourselves, skipping depots we can't
        # access (unowned/shared) rather than failing the whole job.
        depots = _cdn_client.get_app_depot_info(app_id)
        depot_gids = enumerate_module.manifest_gids_for_app(depots, "public")
        compressor = _zstd.ZstdCompressor(level=3)

        payload: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        # Track every BLOB temp file written so a mid-loop failure can clean them
        # up (audit 2026-06-09): on the outer-except path the orchestrator never
        # receives the raw_path values, so the worker (producer) must delete them
        # or they accumulate on the container FS across every failed fetch.
        written_blobs: list[Path] = []
        try:
            for depot_id, gid in depot_gids:
                try:
                    code = _cdn_client.get_manifest_request_code(app_id, depot_id, gid)
                    mfst = _cdn_client.get_manifest(
                        app_id, depot_id, gid, decrypt=True, manifest_request_code=code
                    )
                except Exception as e:
                    # #122: an UNAMBIGUOUS session-wide auth loss (e.g. NotLoggedOn)
                    # on a per-depot call means the whole session died — re-raise so
                    # the fetch fails as NotAuthenticated rather than swallowing it
                    # into `skipped` as a false-partial success (the #109 lesson).
                    # AccessDenied is left as a skip: the common "depot not owned"
                    # case is indistinguishable from auth loss here.
                    if isinstance(e, SteamError) and getattr(e, "eresult", None) in (
                        _SESSION_LOST_ERESULTS
                    ):
                        raise
                    skipped.append(
                        {"depot_id": depot_id, "reason": f"{type(e).__name__}: {e}"[:120]}
                    )
                    continue

                # #123.1: serialize(compress=False) — serialize() defaults to
                # ZIP-compressing, so storing zstd(serialize()) was zstd(ZIP(pb)),
                # double compression. We store zstd(pb); DepotManifest.deserialize
                # auto-detects (tries ZipFile, falls back to raw), so the F7 expand
                # round-trip and already-stored zstd(ZIP(pb)) blobs both still parse.
                data = mfst.serialize(compress=False)  # raw protobuf bytes
                compressed = compressor.compress(data)

                # S2-1: write the BLOB to a temp file on the shared container FS
                # and send its PATH, not the bytes. A 50+ depot game's combined
                # base64 response would otherwise exceed the 10 MiB IPC line cap
                # and kill the worker. The orchestrator reads + deletes the file.
                blob_path = _blob_temp_path("manifest")
                blob_path.write_bytes(compressed)
                written_blobs.append(blob_path)

                # #121: report the manifest's UNIQUE chunk count (protobuf
                # ContentManifestMetadata.unique_chunks) — what F7's SHA-deduped
                # validate counts — not the sum of per-file mapping refs, which
                # double-counts content-deduped chunks (operator saw "1820" here
                # vs the validator's "1100"). `unique_chunks` legitimately being 0
                # (an empty depot) is distinct from the field being ABSENT (a
                # steam-next rename / older protobuf): only the latter falls back
                # to the summed refs, and it warns to stderr so a silent revert to
                # the double-counting bug is visible rather than masked.
                raw_unique = getattr(mfst.metadata, "unique_chunks", None)
                if raw_unique is None:
                    unique_chunks = sum(len(mapping.chunks) for mapping in mfst.payload.mappings)
                    print(  # noqa: T201 — surfaced via the orchestrator's stderr drain
                        f"manifest_fetch: metadata.unique_chunks missing for depot "
                        f"{mfst.depot_id}; chunk_count fell back to summed mapping refs",
                        file=sys.stderr,
                    )
                else:
                    unique_chunks = int(raw_unique or 0)

                # #123.2: `name` was sent over IPC but the orchestrator handler
                # never consumes it — drop the dead field.
                payload.append(
                    {
                        "depot_id": int(mfst.depot_id),
                        "manifest_gid": int(mfst.gid),
                        "total_bytes": int(mfst.metadata.cb_disk_original or 0),
                        "chunk_count": unique_chunks,
                        "raw_path": str(blob_path),
                    }
                )
        except (Exception, GeventTimeout):
            # Failure mid-fetch (including a gevent.Timeout from a slow CDN
            # depot): the orchestrator gets _err (no manifests list), so it can
            # never clean these up. Delete them here before re-raising.
            for bp in written_blobs:
                with contextlib.suppress(OSError):
                    bp.unlink()
            raise

        _ok(msg_id, {"manifests": payload, "skipped": skipped})
    except GeventTimeout as e:
        # A CDN/CM operation exceeded steam-next's internal timeout. Report a
        # retryable error rather than silently recording a partial manifest set
        # as success (the false-empty-library lesson, #109) — and crucially,
        # never let the BaseException escape and crash the worker.
        _err(msg_id, "SteamCDNTimeout", f"manifest fetch timed out (app {app_id}): {e}"[:200])
    except SteamError as e:
        # #122: map a genuine auth-loss EResult to NotAuthenticated so the
        # orchestrator's auth-flip fires. Everything else — notably EResult.Timeout
        # from a transient CM drop — stays a retryable SteamAPIError, so a network
        # blip never forces a needless 2FA re-auth.
        if getattr(e, "eresult", None) in _AUTH_LOSS_ERESULTS:
            _err(
                msg_id, "NotAuthenticated", f"steam auth lost (eresult={e.eresult}): {str(e)[:140]}"
            )
        else:
            _err(msg_id, "SteamAPIError", str(e)[:200])
    except Exception as e:
        _err(msg_id, "SteamAPIError", str(e)[:200])


def _handle_manifest_expand(msg_id: str, params: dict[str, str]) -> None:
    """Deserialize a stored manifest BLOB → {depot_id, chunk_shas} (F7).

    Offline: zstd-decompress the stored bytes then reconstruct via
    `DepotManifest(data)` and iterate `payload.mappings[*].chunks[*].sha`.
    No CDNClient, no `_client`, no Steam session — pure protobuf parse, so
    validation works even when the operator's auth has expired.

    Chunk SHAs are deduped: the same chunk can appear in multiple file
    mappings (Steam content dedup), but in the cache it is one file.

    S2-2: the orchestrator writes the stored BLOB to a temp file and sends
    its PATH (not ~170 MB of base64 over stdin). We read, then delete it.
    """
    raw_path = params.get("raw_path")
    if not raw_path:
        _err(msg_id, "InvalidArgument", "manifest.expand requires raw_path")
        return
    try:
        import zstandard as _zstd
        from steam.core.manifest import DepotManifest  # type: ignore[import-not-found]

        compressed = Path(raw_path).read_bytes()
        data = _zstd.ZstdDecompressor().decompress(compressed)
        mfst = DepotManifest(data)

        seen: dict[str, None] = {}
        for mapping in mfst.payload.mappings:
            for chunk in mapping.chunks:
                seen.setdefault(chunk.sha.hex(), None)

        _ok(msg_id, {"depot_id": int(mfst.depot_id), "chunk_shas": list(seen)})
    except Exception as e:
        _err(msg_id, "ManifestParseError", f"{type(e).__name__}: {e}"[:200])
    finally:
        # The orchestrator produced this temp file; the worker (consumer)
        # deletes it after reading.
        with contextlib.suppress(OSError):
            Path(raw_path).unlink()


_HANDLERS = {
    "auth.begin": _handle_auth_begin,
    "auth.complete": _handle_auth_complete,
    "auth.status": _handle_auth_status,
    "library.enumerate": _handle_library_enumerate,
    "manifest.fetch": _handle_manifest_fetch,
    "manifest.expand": _handle_manifest_expand,
}


def main() -> int:
    # Issue #95 item 7: stdin (worker reading from orchestrator) is NOT
    # length-capped here. The 10 MiB `MAX_IPC_LINE_BYTES` cap in
    # protocol.py is enforced on the response direction only — what the
    # client reads from the worker's stdout. The asymmetry is
    # intentional: stdin is a trusted channel from the orchestrator
    # process we ourselves spawned (see ADR-0013). Adding a cap here
    # would have to balance "longer requests may be legitimate"
    # (e.g., a manifest BLOB round-trip in BL12) against "no realistic
    # caller sends 100 MiB". Deferred until a concrete need exists.
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg_id = req.get("msg_id", "")
        op = req.get("op", "")
        if op == "shutdown":
            _ok(msg_id, {"ok": True})
            return 0
        handler = _HANDLERS.get(op)
        if handler is None:
            _err(msg_id, "UnknownOp", op)
            continue
        try:
            handler(msg_id, req.get("params") or {})
        except GeventTimeout as e:
            # Defense in depth: a gevent.Timeout (BaseException) from any handler
            # would otherwise unwind past this loop and kill the worker — failing
            # every subsequent job until a restart. Convert to a retryable error
            # and keep serving (UAT-11: a 15s CDN timeout crashed the worker).
            _err(msg_id, "SteamCDNTimeout", f"steam operation timed out: {e}"[:200])
        except Exception as e:
            # Last-resort guard: no handler bug may take the worker down.
            _err(msg_id, "SteamAPIError", f"{type(e).__name__}: {e}"[:200])
    return 0


if __name__ == "__main__":
    sys.exit(main())
