"""Classify a Steam app as a prefill-exclusion CANDIDATE (#229).

The operator curates ``selectedAppsToPrefill.json`` by hand; the scheduled
prefill pulls every app in it to the LAN cache. Soundtracks, dedicated servers,
SDKs, editors/tools, demos, and trailers waste WAN pulls and cache space and
never need to be on the cache. This module flags such apps as *candidates* for
the operator to review — it NEVER edits the selection itself.

Pure + stdlib only (mirrors manifest_fetcher's isolation): the classification is
a function of the Steam store ``type`` + ``name`` already cached in
``steam_app_info`` (populated by library_sync). It cannot catch a genuine
utility that Steam types as ``game`` (e.g. Lossless Scaling); those stay an
operator judgement call.
"""

from __future__ import annotations

import re

# Steam store `type` values that are never worth prefilling. `game`, `dlc`, and
# `mod` are real downloadable content and are kept.
_NON_GAME_TYPES = frozenset(
    {
        "music",
        "application",
        "tool",
        "demo",
        "video",
        "movie",
        "media",
        "series",
        "episode",
        # NOTE: `advertising` intentionally EXCLUDED from this set (#229 follow-up).
        # Steam types some real games' app_ids as `advertising` (seen live:
        # Darksiders II 50650, Eufloria 41210), so flagging it produced false
        # positives. A genuine MP-only/promo entry typed `advertising` (e.g. COD
        # BO2 Zombies) is now an operator judgement call, like game-typed tools.
        "hardware",
        "config",
        "comic",
        "beta",
    }
)

# Name phrases that flag an app typed `game` on the store but which is really a
# server/tool/soundtrack. Kept as full phrases (NOT a bare "server", which would
# over-match "Observer") to avoid excluding a real game.
_NAME_FLAG_RE = re.compile(
    r"dedicated server|\bsdk\b|soundtrack|\bost\b|benchmark|authoring tool",
    re.IGNORECASE,
)


def classify(
    app_type: str | None,
    name: str | None,
    *,
    has_single_player: int | None = None,
    has_multiplayer: int | None = None,
) -> str | None:
    """Return an exclusion REASON when this app should not be prefilled to the
    LAN cache, else None. The reason is a short tag for the operator's review
    (``type=music``, ``name~'dedicated server'``, or ``multiplayer-only``). A
    candidate is never removed from the selection automatically — the operator
    decides.

    ``has_single_player`` / ``has_multiplayer`` are the Steam store category
    signals (1/0, or None when categories haven't been fetched). A game with a
    multiplayer category and NO single-player category is flagged ``multiplayer-only``
    (#366) — but only when BOTH flags are known, so an un-fetched app is never
    guessed as MP-only."""
    t = (app_type or "").strip().lower()
    if t in _NON_GAME_TYPES:
        return f"type={t}"
    m = _NAME_FLAG_RE.search(name or "")
    if m:
        return f"name~{m.group(0).lower()!r}"
    if (
        has_single_player is not None
        and has_multiplayer is not None
        and has_multiplayer
        and not has_single_player
    ):
        return "multiplayer-only"
    return None
