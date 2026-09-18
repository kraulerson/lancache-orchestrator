"""The keys_zone gauge: sample the cache, compare against the nearest ceiling,
push a Kuma heartbeat. Runs as a thread inside the cache-catcher guard.

Deliberately thin. Everything that can be arithmetically wrong lives in
key_budget.py where CI can test it; this file only reads directories, reads
/proc, appends a CSV line, and pushes.

Cost discipline: the daily path uses listdir and NEVER stat. Stat runs at about
180 files/sec on this NAS under sweep load -- a 54k-file walk took over five
minutes during design. Mean object size is therefore a separate weekly sample.
"""

from __future__ import annotations

import os
import random
import time

import kuma
from key_budget import (
    TOTAL_LEAVES,
    effective_capacity,
    ram_capacity_keys,
    summarise_sample,
    verdict,
    zone_capacity_keys,
)

CACHE_ROOT = "/volume1/cache/cache"
HISTORY = "/log/key_budget.csv"
ENV_FILE = "/log/keybudget.env"

# The three KUMA_PUSH_* keys are listed here with empty defaults so this file is
# the single place that documents the whole /log/keybudget.env surface. An unset
# URL disables that heartbeat -- see kuma.push.
DEFAULTS = {
    "SAMPLE_LEAVES": "256",
    "RAM_BUDGET_BYTES": str(9 * 1024**3),
    "FLOOR": "0.75",
    "HORIZON_DAYS": "90",
    "PROBE_INTERVAL_SEC": str(24 * 3600),
    "KUMA_PUSH_KEY_BUDGET": "",
    "KUMA_PUSH_CACHE_GUARD": "",
    "KUMA_PUSH_CACHE_EVICTION": "",
}


def load_cfg(path=ENV_FILE):
    cfg = dict(DEFAULTS)
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip()
    except OSError:
        pass
    return cfg


def sample_cache(root, leaves, rng):
    """Count entries in `leaves` distinct random leaf dirs. listdir only."""
    chosen = rng.sample(range(TOTAL_LEAVES), min(leaves, TOTAL_LEAVES))
    counts, failures = [], 0
    for idx in chosen:
        path = os.path.join(root, f"{idx >> 8:02x}", f"{idx & 0xFF:02x}")
        try:
            counts.append(len(os.listdir(path)))
        except OSError:
            # Denied or missing. NOT an empty leaf -- see key_budget.summarise_sample.
            failures += 1
    return summarise_sample(counts, read_failures=failures)


def nginx_rss_bytes():
    """Total RSS of the nginx workers, from host /proc (container is pid=host).

    Workers share the zone mapping, so the largest worker's RSS approximates the
    zone rather than the sum, which would multiply it by the worker count.
    """
    best = 0
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/comm") as fh:
                if fh.read().strip() != "nginx":
                    continue
            with open(f"/proc/{pid}/statm") as fh:
                rss_pages = int(fh.read().split()[1])
        except (OSError, ValueError, IndexError):
            continue
        best = max(best, rss_pages * os.sysconf("SC_PAGE_SIZE"))
    return best or None


def nginx_index_size_mb():
    """CACHE_INDEX_SIZE from the running nginx's environment.

    Read from the process rather than hardcoded so resizing the zone does not
    silently invalidate the alarm. Returns None on failure -- never a default.
    """
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/comm") as fh:
                if fh.read().strip() != "nginx":
                    continue
            with open(f"/proc/{pid}/environ", "rb") as fh:
                environ = fh.read().decode("utf-8", "replace")
        except OSError:
            continue
        for entry in environ.split("\0"):
            if entry.startswith("CACHE_INDEX_SIZE="):
                raw = entry.split("=", 1)[1].strip().lower().rstrip("m")
                try:
                    return int(raw)
                except ValueError:
                    return None
    return None


def read_history(path=HISTORY):
    rows = []
    try:
        with open(path) as fh:
            for line in fh:
                parts = line.strip().split(",")
                if len(parts) >= 2:
                    try:
                        rows.append((float(parts[0]), float(parts[1])))
                    except ValueError:
                        continue
    except OSError:
        pass
    return rows


def append_history(ts, objects, rss, per_key, path=HISTORY):
    try:
        with open(path, "a") as fh:
            fh.write(f"{ts:.0f},{objects:.0f},{rss or ''},{per_key or ''}\n")
    except OSError:
        pass


def run_once(cfg, rng=None, now=None):
    rng = rng or random.SystemRandom()
    now = now or time.time()

    sample = sample_cache(CACHE_ROOT, int(cfg["SAMPLE_LEAVES"]), rng)
    rss = nginx_rss_bytes()
    per_key = (rss / sample.objects) if (rss and sample.objects) else None

    ceiling = effective_capacity(
        zone_capacity_keys(nginx_index_size_mb()),
        ram_capacity_keys(int(cfg["RAM_BUDGET_BYTES"]), per_key),
    )

    history = read_history()
    result = verdict(
        sample,
        ceiling,
        history,
        floor=float(cfg["FLOOR"]),
        horizon_days=float(cfg["HORIZON_DAYS"]),
    )

    if sample.objects is not None:
        append_history(now, sample.objects, rss, per_key)

    kuma.push(cfg.get("KUMA_PUSH_KEY_BUDGET"), result.status, result.msg)
    return result


def probe_loop(cfg, log=print):
    """Daily gauge. Its OWN Kuma monitor, so if this thread dies its monitor goes
    silent and red while the guard's stays green -- partial failure stays visible.
    """
    interval = float(cfg["PROBE_INTERVAL_SEC"])
    while True:
        try:
            result = run_once(cfg)
            log(f"KEY-BUDGET {result.status}: {result.msg}")
        except Exception as exc:
            log(f"KEY-BUDGET probe failed ({type(exc).__name__})")
            kuma.push(
                cfg.get("KUMA_PUSH_KEY_BUDGET"),
                "down",
                f"probe raised {type(exc).__name__}",
            )
        time.sleep(interval)
