"""Epic platform data models (F6). Pure data — no I/O."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AuthTokens:
    access_token: str
    refresh_token: str
    account_id: str
    display_name: str
    expires_at: str  # ISO8601 UTC, access-token expiry


@dataclass(frozen=True)
class EpicChunk:
    guid: tuple[int, int, int, int]
    hash: int
    sha_hash: bytes
    group_num: int
    file_size: int
    window_size: int


@dataclass
class EpicManifest:
    version: int
    chunks: list[EpicChunk] = field(default_factory=list)
    cdn_base: str = ""  # CDN base path (dir of the manifest URI), no host
    raw: bytes = b""  # the original binary manifest (for storage/diff)


@dataclass(frozen=True)
class EpicLibraryItem:
    app_name: str
    namespace: str
    catalog_item_id: str
    title: str
    build_version: str | None = None
