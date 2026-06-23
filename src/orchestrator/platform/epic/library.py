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

# COR-4: bound pagination so a misbehaving/hostile API can't loop forever.
# A real Epic library is a handful of pages; this is a generous backstop.
_MAX_PAGES = 10_000


class EpicLibraryError(Exception):
    """Epic library enumeration failed.

    ``status_code`` carries the upstream HTTP status when the failure came from a
    response (so EpicClient can force a token refresh + retry on 401)."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


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
    build_version = rec.get("buildVersion")
    return EpicLibraryItem(
        app_name=str(app_name),
        namespace=str(namespace),
        catalog_item_id=str(catalog),
        title=str(title),
        build_version=(str(build_version) if build_version else None),
    )


async def enumerate_library(access_token: str, settings: Settings) -> list[EpicLibraryItem]:
    """Enumerate owned library items, following the cursor to the last page."""
    headers = {"Authorization": f"bearer {access_token}"}
    items: list[EpicLibraryItem] = []
    params: dict[str, Any] = {"includeMetadata": "true"}
    seen_cursors: set[str] = set()
    async with _client(settings) as client:
        for _page in range(_MAX_PAGES):
            resp = await client.get(settings.epic_library_url, headers=headers, params=params)
            if resp.status_code != 200:
                raise EpicLibraryError(
                    f"epic library fetch failed: HTTP {resp.status_code}",
                    status_code=resp.status_code,
                )
            data = resp.json()
            for rec in data.get("records", []):
                item = _to_item(rec)
                if item is not None:
                    items.append(item)
            cursor = (data.get("responseMetadata") or {}).get("nextCursor")
            if not cursor:
                return items
            # COR-4: a repeated cursor means the API is looping us — fail loudly
            # rather than paginate forever.
            if cursor in seen_cursors:
                raise EpicLibraryError(f"epic library pagination repeated cursor: {cursor!r}")
            seen_cursors.add(cursor)
            params["cursor"] = cursor
    # COR-4: exhausted the page cap without a terminal (empty-cursor) page.
    raise EpicLibraryError(f"epic library pagination exceeded {_MAX_PAGES} pages")
