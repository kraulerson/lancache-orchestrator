"""Is the lancache cache index running out of room, and how fast?

nginx OSS publishes no gauge for keys_zone occupancy: there is no
http_api_module, stub_status omits cache zones, and the error log stays silent
through the normal evict-to-fit path -- it logged nothing at all through the
2026-07-31 mass deletion. So the metric is derived: count cache objects by
sampling leaf directories, and compare against the nearest real ceiling.

Pure and stdlib-only on purpose. key_budget_probe.py does the I/O; everything
that can be arithmetically wrong lives here, where CI can reach it.
"""

from __future__ import annotations

import math
from typing import NamedTuple

# nginx documents ~8000 keys per megabyte of zone for open-source builds
# (commercial subscriptions carry extended cache info and fit ~4000). This
# deployment is nginx/1.24.0 OSS.
KEYS_PER_MB = 8000

# proxy_cache_path ... levels=2:2 -> 256 * 256 leaf directories.
TOTAL_LEAVES = 65536


class Sample(NamedTuple):
    objects: float | None
    stderr: float | None
    leaves_read: int
    read_failures: int


class Ceiling(NamedTuple):
    keys: int | None
    name: str


class Verdict(NamedTuple):
    status: str
    msg: str


def summarise_sample(leaf_counts, read_failures=0, total_leaves=TOTAL_LEAVES):
    """Extrapolate a whole-tree object count from a sample of leaf directories.

    A leaf we could not read is NOT a leaf with no files in it. Some cache
    directories are mode 0700 and a denied read is indistinguishable from an
    empty one unless you insist on the difference -- the first measurement taken
    while designing this came out 13x low for exactly that reason.
    """
    n = len(leaf_counts)
    if n == 0:
        return Sample(None, None, 0, read_failures)

    mean = sum(leaf_counts) / n
    objects = mean * total_leaves

    if n > 1:
        var = sum((c - mean) ** 2 for c in leaf_counts) / (n - 1)
        stderr = math.sqrt(var / n) * total_leaves
    else:
        stderr = float("inf")

    return Sample(objects, stderr, n, read_failures)


def zone_capacity_keys(index_size_mb):
    """Keys the configured keys_zone can hold, or None if the size is unknown.

    Unknown is returned rather than a default. #315 is an open bug about a bare
    constant standing in for a configurable value; this must not add another.
    """
    if not index_size_mb or index_size_mb <= 0:
        return None
    return int(index_size_mb) * KEYS_PER_MB


def ram_capacity_keys(ram_budget_bytes, bytes_per_key):
    """Keys the host has memory to hold.

    The zone is shared memory that grows as keys are inserted, so on this NAS the
    configured zone size is unreachable: 10000m needs ~10 GiB on a 15.4 GiB host
    that also carries an 8 GiB agent limit. See issue #346.
    """
    if not ram_budget_bytes or not bytes_per_key or bytes_per_key <= 0:
        return None
    return int(ram_budget_bytes / bytes_per_key)


def effective_capacity(zone_keys, ram_keys):
    """The nearest ceiling, named.

    Naming it is the point. A monitor that reports a number without saying what
    the number is bounded by cannot tell the operator what to do about it, which
    is the defect #326 and #330 both describe.
    """
    known = [(k, name) for k, name in ((zone_keys, "zone"), (ram_keys, "ram")) if k]
    if not known:
        return Ceiling(None, "unknown")
    keys, name = min(known)
    return Ceiling(keys, name)


def project_days_to(history, target):
    """Days until `target` objects at the observed rate, or None if unknowable.

    None covers three cases that must never be reported as reassurance: too few
    points to fit a line, a flat trend, and a SHRINKING one. A falling object
    count means eviction is already under way; extrapolating it yields a negative
    slope and an answer of 'never', which is the most dangerous possible output.
    """
    if len(history) < 2:
        return None

    (t0, n0), (t1, n1) = history[0], history[-1]
    elapsed = t1 - t0
    if elapsed <= 0:
        return None

    per_day = (n1 - n0) / (elapsed / 86400.0)
    if per_day <= 0:
        return None

    if n1 >= target:
        return 0.0
    return (target - n1) / per_day


def verdict(sample, ceiling, history, floor=0.75, horizon_days=90.0):
    """Decide the monitor's state and say why in one line."""
    if sample.objects is None:
        return Verdict("down", f"sample failed: 0 of {sample.read_failures} leaves readable")

    objects_m = sample.objects / 1e6

    if ceiling.keys is None:
        return Verdict(
            "down",
            f"ceiling unknown (CACHE_INDEX_SIZE and RAM both unreadable); {objects_m:.1f}M objects",
        )

    used = sample.objects / ceiling.keys
    floor_keys = floor * ceiling.keys
    days = project_days_to(history, floor_keys)

    trend = "trend unknown" if days is None else f"{days:.0f}d to floor"
    detail = (
        f"{objects_m:.1f}M objects, {used * 100:.0f}% of {ceiling.name} "
        f"ceiling {ceiling.keys / 1e6:.1f}M, {trend}"
    )

    if sample.objects >= floor_keys:
        return Verdict("down", "OVER FLOOR: " + detail)
    if days is not None and days < horizon_days:
        return Verdict("down", "FILLING FAST: " + detail)
    return Verdict("up", detail)
