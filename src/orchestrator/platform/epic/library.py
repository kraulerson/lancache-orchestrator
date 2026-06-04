"""Epic library enumeration (F6).

Paginated GET of the operator's owned items. Pure async httpx; the caller
(EpicClient / library_sync handler) maps EpicLibraryItem rows into the games table.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import structlog

from orchestrator.platform.epic.models import EpicLibraryItem

if TYPE_CHECKING:
    from orchestrator.core.settings import Settings

_log = structlog.get_logger(__name__)


class EpicLibraryError(Exception):
    """Epic library enumeration failed."""


def _build_transport() -> httpx.AsyncBaseTransport | None:
    """Seam for tests to inject httpx.MockTransport. None -> real network."""
    return None


def _client(settings: Settings) -> httpx.AsyncClient:
    transport = _build_transport()
    kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(30.0, connect=10.0),
        "headers": {"User-Agent": settings.epic_user_agent},
    }
    if transport is not None:
        kwargs["transport"] = transport
    return httpx.AsyncClient(**kwargs)


def _to_item(rec: dict[str, Any]) -> EpicLibraryItem | None:
    app_name = rec.get("appName")
    namespace = rec.get("namespace")
    catalog = rec.get("catalogItemId")
    if not app_name or not namespace or not catalog:
        return None
    title = (rec.get("metadata") or {}).get("title") or app_name
    return EpicLibraryItem(
        app_name=str(app_name),
        namespace=str(namespace),
        catalog_item_id=str(catalog),
        title=str(title),
    )


async def enumerate_library(access_token: str, settings: Settings) -> list[EpicLibraryItem]:
    """Enumerate owned library items, following the cursor to the last page."""
    headers = {"Authorization": f"bearer {access_token}"}
    items: list[EpicLibraryItem] = []
    params: dict[str, Any] = {"includeMetadata": "true"}
    async with _client(settings) as client:
        while True:
            resp = await client.get(settings.epic_library_url, headers=headers, params=params)
            if resp.status_code != 200:
                raise EpicLibraryError(f"epic library fetch failed: HTTP {resp.status_code}")
            data = resp.json()
            for rec in data.get("records", []):
                item = _to_item(rec)
                if item is not None:
                    items.append(item)
            cursor = (data.get("responseMetadata") or {}).get("nextCursor")
            if not cursor:
                break
            params["cursor"] = cursor
    return items
