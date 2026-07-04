"""Steam store appdetails lookup (public, no auth) — app type + name + the
Single-/Multi-player category signals used for MP-only detection (#366).

Used by library_sync to filter prefilled apps to actual games (type=='game')
and to get their display names, replacing the deleted worker's enumeration.
"""

from __future__ import annotations

from typing import Any, TypedDict

import httpx
import structlog

_log = structlog.get_logger(__name__)
_URL = "https://store.steampowered.com/api/appdetails"

# Steam store category ids (from the appdetails `categories` list).
_SINGLE_PLAYER_CATEGORY_ID = 2
# Gameplay multiplayer categories — a game carrying any of these AND no
# single-player category is "multiplayer-only". Non-gameplay categories (Trading
# Cards=29, Workshop=30, In-App Purchases=35, …) are deliberately NOT here.
_MULTIPLAYER_CATEGORY_IDS = frozenset(
    {
        1,  # Multi-player
        9,  # Co-op
        20,  # MMO
        24,  # Shared/Split Screen
        27,  # Cross-Platform Multiplayer
        36,  # Online PvP
        37,  # Shared/Split Screen PvP
        38,  # Online Co-op
        39,  # Shared/Split Screen Co-op
        47,  # LAN PvP
        48,  # LAN Co-op
        49,  # PvP
    }
)


class AppInfo(TypedDict):
    """Store lookup result. Category flags are 1/0 when categories were present,
    or None when they were absent/malformed (unknown — never guessed)."""

    type: str
    name: str
    has_single_player: int | None
    has_multiplayer: int | None


def _category_flags(categories: object) -> tuple[int | None, int | None]:
    """Derive (has_single_player, has_multiplayer) from an appdetails
    ``categories`` list. Returns (None, None) when the list is absent, malformed,
    or carries no recognizable category ids — the caller treats NULL as unknown."""
    if not isinstance(categories, list):
        return (None, None)
    ids: set[int] = set()
    for c in categories:
        if isinstance(c, dict):
            cid = c.get("id")
            if isinstance(cid, int):
                ids.add(cid)
    if not ids:
        return (None, None)
    sp = 1 if _SINGLE_PLAYER_CATEGORY_ID in ids else 0
    mp = 1 if ids & _MULTIPLAYER_CATEGORY_IDS else 0
    return (sp, mp)


def _build_transport() -> httpx.AsyncBaseTransport | None:
    """Seam for tests to inject an ``httpx.MockTransport``. None → real network."""
    return None


async def fetch_app_info(app_id: int) -> AppInfo | None:
    """Return {'type','name','has_single_player','has_multiplayer'} from the Steam
    store, or None on any failure. ``filters=basic,categories`` keeps the payload
    small while still returning the category list for MP-only detection (#366)."""
    transport = _build_transport()
    kwargs: dict[str, Any] = {"timeout": httpx.Timeout(15.0, connect=10.0)}
    if transport is not None:
        kwargs["transport"] = transport
    try:
        async with httpx.AsyncClient(**kwargs) as client:
            resp = await client.get(
                _URL, params={"appids": str(app_id), "filters": "basic,categories"}
            )
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        entry = resp.json().get(str(app_id), {})
    except ValueError:
        return None
    if not entry.get("success"):
        return None
    data = entry.get("data", {})
    name = data.get("name")
    app_type = data.get("type")
    if not isinstance(name, str) or not isinstance(app_type, str):
        return None
    sp, mp = _category_flags(data.get("categories"))
    return {"type": app_type, "name": name, "has_single_player": sp, "has_multiplayer": mp}
