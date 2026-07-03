"""Epic library enumeration (F6).

Paginated GET of the operator's owned items. Pure async httpx; the caller
(EpicClient / library_sync handler) maps EpicLibraryItem rows into the games table.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
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

# Bound concurrent catalog title lookups (#140): each item usually has its own
# namespace, so a full library is hundreds of calls — cap the fan-out so we don't
# open hundreds of sockets or hammer Epic's catalog API at once.
_TITLE_CONCURRENCY = 10


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


async def _resolve_titles(
    client: httpx.AsyncClient,
    access_token: str,
    items: list[EpicLibraryItem],
    settings: Settings,
) -> dict[str, str]:
    """Resolve real display titles for items left on the appName codename (#140).

    An item whose ``title == app_name`` had no ``metadata.title`` in the library
    response, so it carries the codename (e.g. 'Fangtooth'). Look up the real
    title from Epic's catalog bulk-items API, grouped by namespace (the endpoint
    is namespace-scoped and accepts multiple ids). Returns ``{catalog_item_id:
    title}`` for every item resolved. Best-effort: any lookup that errors or omits
    a title is simply absent from the map (the caller keeps the codename), so a
    title backfill never fails the library sync.
    """
    by_ns: dict[str, list[EpicLibraryItem]] = {}
    for item in items:
        if item.title == item.app_name:  # codename fallback -> needs a real title
            by_ns.setdefault(item.namespace, []).append(item)
    resolved: dict[str, str] = {}
    if not by_ns:
        return resolved
    headers = {"Authorization": f"bearer {access_token}"}
    sem = asyncio.Semaphore(_TITLE_CONCURRENCY)

    async def _resolve_ns(namespace: str, ns_items: list[EpicLibraryItem]) -> None:
        url = settings.epic_catalog_url_template.format(namespace=namespace)
        # Repeated `id` keys (one per catalog item) need a tuple list, not a dict.
        qp: list[tuple[str, str | int | float | bool | None]] = [
            ("id", it.catalog_item_id) for it in ns_items
        ]
        qp += [("country", "US"), ("locale", "en-US")]
        params = httpx.QueryParams(qp)
        async with sem:
            try:
                resp = await client.get(url, headers=headers, params=params)
                if resp.status_code != 200:
                    return
                data = resp.json()
            except (httpx.HTTPError, ValueError) as e:
                _log.warning("epic.title_resolve_failed", namespace=namespace, reason=str(e))
                return
        if not isinstance(data, dict):
            return
        for it in ns_items:
            entry = data.get(it.catalog_item_id)
            title = entry.get("title") if isinstance(entry, dict) else None
            if title:
                # No await between here and the dict write, so the single-threaded
                # event loop makes these concurrent tasks' writes race-free.
                resolved[it.catalog_item_id] = str(title)

    await asyncio.gather(*(_resolve_ns(ns, its) for ns, its in by_ns.items()))
    return resolved


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
                resolved = await _resolve_titles(client, access_token, items, settings)
                if resolved:
                    items = [
                        replace(it, title=resolved[it.catalog_item_id])
                        if it.catalog_item_id in resolved
                        else it
                        for it in items
                    ]
                return items
            # COR-4: a repeated cursor means the API is looping us — fail loudly
            # rather than paginate forever.
            if cursor in seen_cursors:
                raise EpicLibraryError(f"epic library pagination repeated cursor: {cursor!r}")
            seen_cursors.add(cursor)
            params["cursor"] = cursor
    # COR-4: exhausted the page cap without a terminal (empty-cursor) page.
    raise EpicLibraryError(f"epic library pagination exceeded {_MAX_PAGES} pages")
