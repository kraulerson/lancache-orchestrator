# keys_zone Alarm Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the lancache cache index an alarm — a leading gauge that says how full it is and how fast it is filling, and a lagging tripwire that says eviction is happening now.

**Architecture:** Two instruments living in `tools/cache_catcher/`, deployed into the existing `cache-catcher` container on the NAS, each pushing its own Uptime Kuma monitor so a partial failure stays visible. All decision logic goes in pure stdlib modules that CI can import; the deployed guard cannot be imported off the NAS because it loads `libc.so.6` at module scope.

**Tech Stack:** Python 3 stdlib only (no third-party imports — the container has none), pytest for the pure modules, Uptime Kuma push monitors, fanotify via `ctypes`.

**Spec:** `docs/superpowers/specs/2026-09-18-keys-zone-alarm-design.md`

## Execution record — 2026-09-18

Tasks 1–4 are complete and committed; Task 5 is complete through Step 4. The
seven remaining unticked boxes are all live-system steps. **Five things were
built differently from what is written below** — the code that shipped is the
authority, and the deviations are recorded here rather than by silently editing
the steps:

1. **The alert `kind` strings in Task 4 Step 2 were wrong.** The plan guessed
   `("evict", "attrib")`; the call sites actually pass `"eviction"`, `"mode000"`,
   `"purge"` and `"external"`. Shipped as
   `EVICT_ALERT_KINDS = ("eviction", "mode000")`. Had the guess shipped, no alert
   would ever have reached Kuma and the promotion would have been silently inert.
   Task 4 Step 2 told the implementer to verify this against the code; it paid
   for itself.
2. **Three monitors, not two.** Approved by Karl during execution and recorded as
   an amendment to the spec. `alert()` pushing DOWN and a liveness thread pushing
   UP to the *same* monitor meant an eviction would go green again within 15
   minutes while still running. `lancache:cache-eviction` is now its own monitor
   with a 1 h latch (`EVICT_LATCH_SEC`), and `lancache:cache-guard` carries
   liveness alone. Task 5 Steps 5 and 6 therefore cover **three** monitors and
   three `KUMA_PUSH_*` keys.
3. **f-strings, not `%` formatting,** in the three new modules. `tools/` is
   excluded from CI lint but **not** from the local pre-commit hook, which runs
   `ruff check` on staged files — and `UP031` rejects `%` formatting. Only
   `fanotify_guard.py` is exempt in `pyproject.toml`, so the guard's own edits
   keep `%` to match their surroundings. Behaviour is identical.
4. **`no-urllib-on-main-loop` had to be adjudicated.** The custom Semgrep rule
   fires on `kuma.py`. Suppressed at line level with the rationale in the file;
   see the spec amendment and
   `docs/security-audits/keys-zone-alarm-security-audit.md`.
5. **`probe_loop` is given the guard's `log`, not the default `print`.** print
   may sit in a buffer and never reach `docker logs`, which would break the
   Step 8 and Step 10 verifications; `log()` flushes stdout and also persists the
   line to `/log/deletions.log`.

Two counts in this plan were off, harmlessly: the tests are **40**, not 29
(pytest expands the parametrized cases to 8 + 32), and the pre-existing suite
baseline is **1944**, not the 1867 quoted in `CLAUDE.md`. Full suite after this
work: **1984 passed, 3 deselected**.

## Global Constraints

- **Stdlib only.** Every file deployed into `cache-catcher` must import nothing beyond the Python standard library. The container has no `pip` packages.
- **Dual import shape.** In the container the modules sit flat in `/log/`, so `fanotify_guard.py` does `from delete_actor import ...`. In the repo they are a package, so tests do `from tools.cache_catcher.key_budget import ...`. New modules must work under **both**: keep container-side imports flat.
- **Monitoring must never break the thing it monitors.** Every push and every sample is wrapped; no failure to report may change the outcome of the thing reported on.
- **Every run pushes, up or down.** Kuma treats silence as DOWN. Silence is never an acceptable outcome.
- **A failed sample pushes DOWN**, never silence.
- **Unknown must never read as safe.** A missing or unusable history yields `unknown`, not infinite runway.
- **An unreadable leaf directory is a read failure, never an empty leaf.** Counting denied as empty under-reported by 13× during design.
- `KEYS_PER_MB = 8000` — nginx OSS, from nginx's own docs. Commercial is ~4000; this deployment is OSS.
- `TOTAL_LEAVES = 65536` — `levels=2:2`.
- Defaults, all overridable from `/log/keybudget.env`: `RAM_BUDGET_BYTES = 9 GiB`, `FLOOR = 0.75`, `HORIZON_DAYS = 90`, `SAMPLE_LEAVES = 256`.
- Run tests as `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest` — the PATH prefix is required or `tests/test_licenses.py` false-fails.

---

## ⚠ Preconditions before executing this plan

1. **The feature gate is currently BLOCKED.** `scripts/test-gate.sh --check-batch` returns 1 ("2 features since last test"). CLAUDE.md forbids starting the next feature until it returns 0. Close UAT 16 first:
   ```
   scripts/process-checklist.sh --complete-step uat_session:remediation_complete
   scripts/process-checklist.sh --complete-step uat_session:gate_passed
   scripts/test-gate.sh --reset-counter
   ```
2. **Start the Build Loop** once the gate is clear:
   ```
   scripts/process-checklist.sh --start-feature "keys-zone-alarm"
   ```
3. **Before every commit**, recreate the single-use evaluation marker. It is consumed by each successful commit. From the repo root, as a lone unchained command with a reason containing no shell metacharacters:
   ```
   bash .claude/framework/hooks/mark-evaluated.sh "reason here"
   ```
4. **`enforce-plan-tracking` is armed** for the rest of any session in which `writing-plans` was invoked. It blocks `Write|Edit` on **source** files (`.py` outside `tests/`) unless a task is marked in_progress via a `TaskUpdate` tool. Docs (`.md`) and anything under `tests/` are exempt. A session without `TaskUpdate` can write the tests but not the implementation — execute this plan in a **fresh session**, where the marker is clear.
5. Run `.venv/bin/ruff format <files>` before each commit, or the pre-commit hook blocks on "would reformat".

---

## File Structure

| file | status | responsibility |
|---|---|---|
| `tools/cache_catcher/kuma.py` | create | one function: push a heartbeat, never raise |
| `tools/cache_catcher/key_budget.py` | create | pure decision logic: sampling maths, capacities, projection, verdict |
| `tools/cache_catcher/key_budget_probe.py` | create | thin I/O shell: read dirs, read RSS, read env, append history, push |
| `tools/cache_catcher/fanotify_guard.py` | modify | eviction → Kuma DOWN; new liveness thread → Kuma UP |
| `tools/cache_catcher/README.md` | modify | document both instruments and the new env file |
| `tests/tools/test_kuma.py` | create | push helper behaviour |
| `tests/tools/test_key_budget.py` | create | all decision logic, especially the degenerate cases |

Sampling maths lives in `key_budget.py`, **not** in the probe, so the part that can be wrong is the part under test. The probe only supplies raw directory listings.

---

### Task 1: Kuma push helper

**Files:**
- Create: `tools/cache_catcher/kuma.py`
- Test: `tests/tools/test_kuma.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `push(url: str | None, status: str, msg: str = "", opener=None) -> bool`. Returns `True` when delivered **or when no URL is configured** (a deliberate disable is nothing to retry); `False` only when a send was attempted and did not arrive. `MSG_MAX_CHARS = 200`, `TIMEOUT_SEC = 10.0`.

- [x] **Step 1: Write the failing test**

```python
"""Kuma push for cache-catcher — stdlib only, and it must never raise.

fanotify_guard.py has no third-party dependencies and cannot gain any: the
container has no pip packages. This mirrors the semantics already settled twice
in this system — src/orchestrator/clients/heartbeat.py and push_kuma() in
run-steam-prefill.sh — so there is one story for what a heartbeat means.
"""

from __future__ import annotations

import urllib.parse

import pytest
from tools.cache_catcher.kuma import MSG_MAX_CHARS, push


class _FakeResponse:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _recorder(status=200, raises=None):
    calls = []

    def opener(url, timeout=None):
        calls.append(url)
        if raises is not None:
            raise raises
        return _FakeResponse(status)

    return opener, calls


@pytest.mark.parametrize("url", [None, "", "   "], ids=["none", "empty", "blank"])
def test_an_unset_url_disables_the_push_and_is_not_a_failure(url):
    """Leaving a URL unset is the documented way to turn a monitor off. It must
    not look like an undelivered heartbeat, or callers will retry forever."""
    opener, calls = _recorder()
    assert push(url, "up", "anything", opener=opener) is True
    assert calls == []


def test_a_delivered_heartbeat_reports_true():
    opener, calls = _recorder(status=200)
    assert push("http://kuma/push/abc", "up", "fine", opener=opener) is True
    assert len(calls) == 1


def test_status_and_message_are_url_encoded_into_the_query():
    """A message containing % or & must not corrupt the request — the disk
    monitor's first version was destroyed by exactly one unescaped percent."""
    opener, calls = _recorder()
    push("http://kuma/push/abc", "down", "zone 94% full & climbing", opener=opener)
    query = urllib.parse.parse_qs(urllib.parse.urlparse(calls[0]).query)
    assert query["status"] == ["down"]
    assert query["msg"] == ["zone 94% full & climbing"]


def test_an_http_error_reports_undelivered():
    """A mistyped push token answers 404: the request completed and nobody was
    told. For every caller that is the same outcome as a connection failure."""
    opener, _ = _recorder(status=404)
    assert push("http://kuma/push/typo", "up", opener=opener) is False


def test_a_connection_failure_reports_undelivered_and_never_raises():
    opener, _ = _recorder(raises=OSError("connection refused"))
    assert push("http://kuma/push/abc", "up", opener=opener) is False


def test_a_long_message_keeps_its_tail():
    """For an error the specific failure is at the end, not the start."""
    opener, calls = _recorder()
    push("http://kuma/push/abc", "down", "x" * 500 + "THE-REASON", opener=opener)
    query = urllib.parse.parse_qs(urllib.parse.urlparse(calls[0]).query)
    assert query["msg"][0].endswith("THE-REASON")
    assert len(query["msg"][0]) == MSG_MAX_CHARS
```

- [x] **Step 2: Run test to verify it fails**

Run: `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest tests/tools/test_kuma.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tools.cache_catcher.kuma'`

- [x] **Step 3: Write minimal implementation**

```python
"""Uptime Kuma push heartbeats for cache-catcher.

Wire format is a GET: ``<url>?status=up|down&msg=<text>``. The URL is the whole
credential, so it lives in /log/keybudget.env and never in the repo. An unset
variable disables that heartbeat.

**Monitoring must never break the thing it monitors.** Every failure here is
swallowed. A guard that dies because Kuma was unreachable is worse than no
guard. If Kuma cannot be reached its monitor goes DOWN for want of a heartbeat,
which is the correct signal anyway.

stdlib only: this is deployed beside fanotify_guard.py in a container with no
pip packages.
"""

from __future__ import annotations

import urllib.parse
import urllib.request

# Kuma stores the message and shows it on the monitor. An unbounded blob does not
# belong in a URL, and the useful part of a failure is its tail.
MSG_MAX_CHARS = 200

TIMEOUT_SEC = 10.0


def push(url, status, msg="", opener=None):
    """Send one heartbeat. Never raises. Reports whether it was delivered.

    Args:
        url: the monitor's push URL, or None/blank to disable this heartbeat.
        status: ``"up"`` or ``"down"``.
        msg: short human-readable detail, truncated to ``MSG_MAX_CHARS``.
        opener: injected by tests; production passes nothing.

    Returns:
        True if the monitor accepted the push, or if no URL is configured — a
        deliberate disable is nothing to retry. False only when a send was
        attempted and did not arrive.
    """
    if not url or not url.strip():
        return True

    trimmed = msg[-MSG_MAX_CHARS:] if len(msg) > MSG_MAX_CHARS else msg
    query = urllib.parse.urlencode({"status": status, "msg": trimmed})
    target = "%s?%s" % (url.strip(), query)

    try:
        open_url = opener or urllib.request.urlopen
        with open_url(target, timeout=TIMEOUT_SEC) as resp:
            return 200 <= int(resp.status) < 400
    except Exception:
        return False
```

- [x] **Step 4: Run test to verify it passes**

Run: `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest tests/tools/test_kuma.py -v`
Expected: PASS, 7 tests.

- [x] **Step 5: Commit**

```bash
.venv/bin/ruff format tools/cache_catcher/kuma.py tests/tools/test_kuma.py
git add tools/cache_catcher/kuma.py tests/tools/test_kuma.py
git commit -m "feat(cache-catcher): stdlib Kuma push helper"
```

---

### Task 2: Key-budget decision logic

**Files:**
- Create: `tools/cache_catcher/key_budget.py`
- Test: `tests/tools/test_key_budget.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `KEYS_PER_MB = 8000`, `TOTAL_LEAVES = 65536`
  - `Sample = NamedTuple(objects: float | None, stderr: float | None, leaves_read: int, read_failures: int)`
  - `summarise_sample(leaf_counts: list[int], read_failures: int = 0, total_leaves: int = TOTAL_LEAVES) -> Sample`
  - `zone_capacity_keys(index_size_mb: int | None) -> int | None`
  - `ram_capacity_keys(ram_budget_bytes: int | None, bytes_per_key: float | None) -> int | None`
  - `Ceiling = NamedTuple(keys: int | None, name: str)`
  - `effective_capacity(zone_keys, ram_keys) -> Ceiling`
  - `project_days_to(history: list[tuple[float, float]], target: float) -> float | None`
  - `Verdict = NamedTuple(status: str, msg: str)`
  - `verdict(sample, ceiling, history, floor=0.75, horizon_days=90.0) -> Verdict`

  `history` is `[(epoch_seconds, objects), ...]` oldest first.

- [x] **Step 1: Write the failing test**

```python
"""The keys_zone gauge's decision logic.

The 2026-07-31 mass deletion was nginx evicting live game data from a ~94%-full
keys_zone. df cannot see that metric and nginx OSS publishes no gauge for it, so
it has to be derived — and a derived metric is only as trustworthy as its
degenerate cases. That is what most of this file tests.
"""

from __future__ import annotations

import pytest
from tools.cache_catcher.key_budget import (
    KEYS_PER_MB,
    TOTAL_LEAVES,
    effective_capacity,
    project_days_to,
    ram_capacity_keys,
    summarise_sample,
    verdict,
    zone_capacity_keys,
)

DAY = 86400.0


def _sample(objects, stderr=0.0):
    leaves = 256
    per_leaf = objects / TOTAL_LEAVES
    return summarise_sample([int(per_leaf)] * leaves, read_failures=0)


# --- sampling -------------------------------------------------------------


def test_the_estimate_scales_the_mean_leaf_to_the_whole_tree():
    s = summarise_sample([10, 20, 30])
    assert s.objects == pytest.approx(20 * TOTAL_LEAVES)
    assert s.leaves_read == 3


def test_an_unreadable_leaf_is_a_read_failure_not_an_empty_one():
    """The measurement that produced this design came out 13x low because a
    permission-denied directory read looked exactly like an empty directory.
    A denied leaf must never drag the mean down."""
    s = summarise_sample([500, 500], read_failures=2)
    assert s.objects == pytest.approx(500 * TOTAL_LEAVES)
    assert s.read_failures == 2
    assert s.leaves_read == 2


def test_a_sample_that_read_nothing_reports_unknown_rather_than_zero():
    """Zero objects and 'I could not look' are different facts. Reporting zero
    would read as a catastrophically empty cache and trip nothing at all."""
    s = summarise_sample([], read_failures=256)
    assert s.objects is None
    assert s.stderr is None


def test_the_standard_error_shrinks_as_more_leaves_are_read():
    few = summarise_sample([100, 900] * 5)
    many = summarise_sample([100, 900] * 50)
    assert many.stderr < few.stderr


# --- capacities -----------------------------------------------------------


def test_zone_capacity_uses_nginx_documented_key_density():
    assert zone_capacity_keys(10000) == 10000 * KEYS_PER_MB == 80_000_000


def test_an_unreadable_index_size_reports_unknown_not_a_guess():
    """CACHE_INDEX_SIZE is read from the running nginx. If that read fails we do
    not invent a number — #315 is an open bug about exactly this shape."""
    assert zone_capacity_keys(None) is None


def test_ram_capacity_divides_the_budget_by_measured_cost_per_key():
    assert ram_capacity_keys(9 * 1024**3, 146.0) == pytest.approx(66_182_878, rel=1e-3)


@pytest.mark.parametrize(
    "budget,per_key", [(None, 146.0), (9 * 1024**3, None), (9 * 1024**3, 0.0)]
)
def test_ram_capacity_is_unknown_when_either_input_is(budget, per_key):
    assert ram_capacity_keys(budget, per_key) is None


def test_the_effective_ceiling_is_the_nearest_one_and_names_itself():
    """A monitor that cannot say WHICH ceiling is near is the #326/#330 defect."""
    c = effective_capacity(zone_keys=80_000_000, ram_keys=66_000_000)
    assert c.keys == 66_000_000
    assert c.name == "ram"


def test_a_known_ceiling_wins_over_an_unknown_one():
    c = effective_capacity(zone_keys=80_000_000, ram_keys=None)
    assert c.keys == 80_000_000
    assert c.name == "zone"


def test_with_no_ceiling_known_at_all_the_result_is_unknown():
    assert effective_capacity(None, None).keys is None


# --- projection -----------------------------------------------------------


def test_a_steady_climb_projects_a_sensible_number_of_days():
    history = [(0.0, 10_000_000.0), (10 * DAY, 11_000_000.0)]
    assert project_days_to(history, 12_000_000.0) == pytest.approx(10.0, rel=1e-6)


@pytest.mark.parametrize(
    "history",
    [[], [(0.0, 1.0)]],
    ids=["empty", "single-point"],
)
def test_too_little_history_is_unknown_not_infinite(history):
    """Absence of a trend must never read as safe. This is the failure that let
    the July incident run for nine days."""
    assert project_days_to(history, 12_000_000.0) is None


def test_a_shrinking_cache_does_not_project_a_comfortable_runway():
    """A falling object count means eviction is already happening. Extrapolating
    it would produce a negative slope and a reassuring 'never' — the single most
    dangerous answer this function could give."""
    history = [(0.0, 12_000_000.0), (10 * DAY, 11_000_000.0)]
    assert project_days_to(history, 13_000_000.0) is None


def test_a_target_already_passed_projects_zero_days():
    history = [(0.0, 10_000_000.0), (10 * DAY, 14_000_000.0)]
    assert project_days_to(history, 13_000_000.0) == 0.0


# --- verdict --------------------------------------------------------------


def test_a_healthy_cache_well_under_the_floor_is_up():
    history = [(0.0, 35_000_000.0), (30 * DAY, 35_600_000.0)]
    v = verdict(_sample(35_600_000), effective_capacity(80_000_000, 66_000_000), history)
    assert v.status == "up"


def test_crossing_the_floor_trips_down():
    history = [(0.0, 50_000_000.0), (30 * DAY, 50_100_000.0)]
    v = verdict(_sample(50_000_000), effective_capacity(80_000_000, 66_000_000), history)
    assert v.status == "down"
    assert "floor" in v.msg.lower()


def test_a_fast_climb_trips_down_long_before_the_floor():
    """The whole point of the projection: an Epic-style storm must be caught
    weeks before the static floor would notice it."""
    history = [(0.0, 35_000_000.0), (10 * DAY, 40_000_000.0)]
    v = verdict(_sample(40_000_000), effective_capacity(80_000_000, 66_000_000), history)
    assert v.status == "down"


def test_an_unknown_trend_does_not_read_as_safe():
    """With no usable history the projection is unknown. The monitor must say so
    in words rather than quietly presenting the floor check as a full result."""
    v = verdict(_sample(35_600_000), effective_capacity(80_000_000, 66_000_000), [])
    assert "unknown" in v.msg.lower()


def test_a_failed_sample_pushes_down_rather_than_staying_silent():
    failed = summarise_sample([], read_failures=256)
    v = verdict(failed, effective_capacity(80_000_000, 66_000_000), [])
    assert v.status == "down"
    assert "sample" in v.msg.lower()


def test_an_unknown_ceiling_pushes_down_rather_than_assuming_room():
    v = verdict(_sample(35_600_000), effective_capacity(None, None), [])
    assert v.status == "down"


def test_the_message_names_the_binding_ceiling():
    v = verdict(_sample(35_600_000), effective_capacity(80_000_000, 66_000_000), [])
    assert "ram" in v.msg.lower()
```

- [x] **Step 2: Run test to verify it fails**

Run: `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest tests/tools/test_key_budget.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tools.cache_catcher.key_budget'`

- [x] **Step 3: Write minimal implementation**

```python
"""Is the lancache cache index running out of room, and how fast?

nginx OSS publishes no gauge for keys_zone occupancy: there is no
http_api_module, stub_status omits cache zones, and the error log stays silent
through the normal evict-to-fit path — it logged nothing at all through the
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
    empty one unless you insist on the difference — the first measurement taken
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
        return Verdict(
            "down",
            "sample failed: 0 of %d leaves readable" % sample.read_failures,
        )

    objects_m = sample.objects / 1e6

    if ceiling.keys is None:
        return Verdict(
            "down",
            "ceiling unknown (CACHE_INDEX_SIZE and RAM both unreadable); %.1fM objects"
            % objects_m,
        )

    used = sample.objects / ceiling.keys
    floor_keys = floor * ceiling.keys
    days = project_days_to(history, floor_keys)

    trend = "trend unknown" if days is None else "%.0fd to floor" % days
    detail = "%.1fM objects, %.0f%% of %s ceiling %.1fM, %s" % (
        objects_m,
        used * 100,
        ceiling.name,
        ceiling.keys / 1e6,
        trend,
    )

    if sample.objects >= floor_keys:
        return Verdict("down", "OVER FLOOR: " + detail)
    if days is not None and days < horizon_days:
        return Verdict("down", "FILLING FAST: " + detail)
    return Verdict("up", detail)
```

- [x] **Step 4: Run test to verify it passes**

Run: `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest tests/tools/test_key_budget.py -v`
Expected: PASS, 22 tests.

- [x] **Step 5: Mark the Build Loop test steps**

```bash
scripts/process-checklist.sh --complete-step build_loop:tests_written
scripts/process-checklist.sh --complete-step build_loop:tests_verified_failing
```

- [x] **Step 6: Commit**

```bash
.venv/bin/ruff format tools/cache_catcher/key_budget.py tests/tools/test_key_budget.py
git add tools/cache_catcher/key_budget.py tests/tools/test_key_budget.py
git commit -m "feat(cache-catcher): key-budget decision logic"
```

---

### Task 3: The probe — sampling, RSS, history, push

**Files:**
- Create: `tools/cache_catcher/key_budget_probe.py`

**Interfaces:**
- Consumes: `key_budget.summarise_sample/zone_capacity_keys/ram_capacity_keys/effective_capacity/verdict`, `kuma.push`.
- Produces: `run_once(cfg) -> Verdict` and `probe_loop(cfg)` for the guard's thread. Container-side imports are flat (`from key_budget import ...`).

This task has no unit tests: it is the I/O shell, and every decision it could get wrong was moved into Task 2 precisely so it would not need them. It is verified live in Task 5.

- [x] **Step 1: Write the implementation**

```python
"""The keys_zone gauge: sample the cache, compare against the nearest ceiling,
push a Kuma heartbeat. Runs as a thread inside the cache-catcher guard.

Deliberately thin. Everything that can be arithmetically wrong lives in
key_budget.py where CI can test it; this file only reads directories, reads
/proc, appends a CSV line, and pushes.

Cost discipline: the daily path uses listdir and NEVER stat. Stat runs at about
180 files/sec on this NAS under sweep load — a 54k-file walk took over five
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

DEFAULTS = {
    "SAMPLE_LEAVES": "256",
    "RAM_BUDGET_BYTES": str(9 * 1024**3),
    "FLOOR": "0.75",
    "HORIZON_DAYS": "90",
    "PROBE_INTERVAL_SEC": str(24 * 3600),
    "KUMA_PUSH_KEY_BUDGET": "",
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
        path = os.path.join(root, "%02x" % (idx >> 8), "%02x" % (idx & 0xFF))
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
            with open("/proc/%s/comm" % pid) as fh:
                if fh.read().strip() != "nginx":
                    continue
            with open("/proc/%s/statm" % pid) as fh:
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
            with open("/proc/%s/comm" % pid) as fh:
                if fh.read().strip() != "nginx":
                    continue
            with open("/proc/%s/environ" % pid, "rb") as fh:
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
            fh.write("%.0f,%.0f,%s,%s\n" % (ts, objects, rss or "", per_key or ""))
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
            log("KEY-BUDGET %s: %s" % (result.status, result.msg))
        except Exception as exc:
            log("KEY-BUDGET probe failed (%s)" % type(exc).__name__)
            kuma.push(
                cfg.get("KUMA_PUSH_KEY_BUDGET"),
                "down",
                "probe raised %s" % type(exc).__name__,
            )
        time.sleep(interval)
```

- [x] **Step 2: Verify it at least imports and the maths wires up**

Run:
```bash
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -c "
import sys; sys.path.insert(0, 'tools/cache_catcher')
import key_budget_probe as p
cfg = p.load_cfg('/nonexistent')
print('defaults ok:', cfg['FLOOR'], cfg['SAMPLE_LEAVES'])
print('history of missing file:', p.read_history('/nonexistent'))
"
```
Expected: `defaults ok: 0.75 256` and `history of missing file: []`

- [x] **Step 3: Commit**

```bash
.venv/bin/ruff format tools/cache_catcher/key_budget_probe.py
git add tools/cache_catcher/key_budget_probe.py
git commit -m "feat(cache-catcher): keys_zone gauge probe"
```

---

### Task 4: Promote the eviction tripwire to Kuma

**Files:**
- Modify: `tools/cache_catcher/fanotify_guard.py` (imports ~line 26; constants ~line 52; `alert()` at line 149; `main()` at line 321)

**Interfaces:**
- Consumes: `kuma.push`, `key_budget_probe.probe_loop`, `key_budget_probe.load_cfg`.
- Produces: no new public API. Behaviour change only.

- [x] **Step 1: Add the imports and constants**

After the `delete_actor` import (line 26), add:

```python
import threading

import kuma
from key_budget_probe import load_cfg, probe_loop
```

After `COOLDOWN = 900` (line 52), add:

```python
# The tripwire's own liveness. Kuma treats silence as DOWN, so pushing on a wall
# clock -- not on event arrival -- is what makes "no heartbeat" mean "the guard
# is dead" rather than "the LAN was quiet tonight".
LIVENESS_INTERVAL_SEC = 900
```

- [x] **Step 2: Make an eviction alert reach Kuma**

In `alert()` (line 149), after `send_email(subject, body)`, add:

```python
    # #337 keeps commanded purges out of here: a purge is a NOTICE, not an alarm,
    # and must not flip the monitor. Only real eviction and mode-000 do.
    if kind in ("evict", "attrib"):
        cfg = load_cfg()
        kuma.push(cfg.get("KUMA_PUSH_CACHE_GUARD"), "down", subject)
```

Confirm the `kind` strings against the two `alert(...)` calls at lines 293 and 311 and use whatever they actually pass; the commanded-purge call at line 277 must NOT be included.

- [x] **Step 3: Add the liveness thread and start both threads**

Add above `main()`:

```python
def liveness_loop(cfg):
    """Push UP on a wall clock so that silence means this guard is dead."""
    while True:
        kuma.push(
            cfg.get("KUMA_PUSH_CACHE_GUARD"),
            "up",
            "guard alive; evict %d/%ds window, purges %d"
            % (len(_evict_ts), EVICT_WINDOW, len(_purge_ts)),
        )
        time.sleep(LIVENESS_INTERVAL_SEC)
```

At the top of `main()` (line 321), after the existing startup log line, add:

```python
    cfg = load_cfg()
    for target in (liveness_loop, probe_loop):
        threading.Thread(target=target, args=(cfg,), daemon=True).start()
```

- [x] **Step 4: Verify the guard still parses**

`fanotify_guard.py` cannot be imported off the NAS (it loads `libc.so.6` at module scope), so syntax is all that can be checked here:

```bash
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m py_compile tools/cache_catcher/fanotify_guard.py && echo "syntax OK"
```
Expected: `syntax OK`

- [x] **Step 5: Run the whole suite to prove nothing regressed**

Run: `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest`
Expected: 1867 + 29 new tests passing, 3 deselected.

- [x] **Step 6: Mark implementation and commit**

```bash
scripts/process-checklist.sh --complete-step build_loop:implemented
.venv/bin/ruff format tools/cache_catcher/fanotify_guard.py
git add tools/cache_catcher/fanotify_guard.py
git commit -m "feat(cache-catcher): push eviction and liveness to Kuma"
```

---

### Task 5: Documentation, deployment and live verification

**Files:**
- Modify: `tools/cache_catcher/README.md`, `CHANGELOG.md`, `FEATURES.md`, `docs/deploy/live-configuration.md`

- [x] **Step 1: Update the README**

Add a section documenting: the two instruments and which Kuma monitor each feeds; that `/log/keybudget.env` holds `KUMA_PUSH_KEY_BUDGET`, `KUMA_PUSH_CACHE_GUARD`, `RAM_BUDGET_BYTES`, `FLOOR`, `HORIZON_DAYS`, `SAMPLE_LEAVES` and is **not** version controlled; and that counting the cache must be done as root inside a container because some leaf dirs are mode 0700 and a denied read looks identical to an empty one.

- [x] **Step 2: Update CHANGELOG.md and FEATURES.md**

`CHANGELOG.md` under `[Unreleased]` → **Added** and **Infrastructure**. `FEATURES.md` gains the feature row. Reference the spec, this plan, and issue #346.

- [x] **Step 3: Update `docs/deploy/live-configuration.md`**

Add the two monitors to §3.1 and the `/log/keybudget.env` variables to the secrets table. Record that both are bound to notification 3.

- [x] **Step 4: Mark documentation and commit**

```bash
scripts/process-checklist.sh --complete-step build_loop:security_audit
scripts/process-checklist.sh --complete-step build_loop:documentation_updated
git add -A
git commit -m "docs: keys_zone alarm"
```

- [ ] **Step 5: Create the two Kuma monitors**

On CT 1057, using the §3.2 procedure — **back up the DB first**:

```bash
ssh root@10.100.23.57 'systemctl stop uptime-kuma && \
  cp /opt/uptime-kuma/data/kuma.db /opt/uptime-kuma/data/kuma.db.bak-prekeybudget-$(date +%Y%m%d-%H%M%S)'
```

Create `lancache:key-budget` and `lancache:cache-guard` as **push** monitors in group **119**, each with `notification_id = 3`. Binding is not optional — monitors 176–182 spent months red on a dashboard telling nobody. Restart with `systemctl start uptime-kuma`.

- [ ] **Step 6: Write `/log/keybudget.env` on the NAS**

Never print or paste the push URLs. Write the file directly on the NAS with the two URLs from Step 5 plus the tuning values.

- [ ] **Step 7: Deploy**

```bash
scp tools/cache_catcher/kuma.py tools/cache_catcher/key_budget.py \
    tools/cache_catcher/key_budget_probe.py tools/cache_catcher/fanotify_guard.py \
    karl@192.168.1.30:/tmp/
ssh karl@192.168.1.30 'for f in kuma.py key_budget.py key_budget_probe.py fanotify_guard.py; do \
    docker cp /tmp/$f cache-catcher:/log/$f; done && docker restart cache-catcher'
```

This touches only `cache-catcher` — not lancache, not the agent, not the orchestrator — so it does not need to wait for an inter-sweep gap.

- [ ] **Step 8: Verify the guard came up**

```bash
ssh karl@192.168.1.30 'docker logs --since 3m cache-catcher | head -20'
```
Expected: the `FANOTIFY guard started` line, then within 15 minutes a `KEY-BUDGET up:` line naming the binding ceiling.

- [ ] **Step 9: Verify against a hand measurement**

Re-measure independently and confirm the gauge agrees within the sampling error (design baseline: **35.6 M objects, ~44.5% of the zone**, and RAM expected to be the binding ceiling at the 9 GiB budget):

```bash
ssh karl@192.168.1.30 'docker exec cache-catcher tail -3 /log/key_budget.csv'
```

- [ ] **Step 10: Wait for a real scheduled run**

Do not accept a manual invocation as proof. Confirm both Kuma monitors are green the next day, and that `lancache:cache-guard` has received liveness pushes. A manual test of a scheduled job proves nothing about the schedule — the disk monitor's `%` bug passed its manual test and never ran once.

- [ ] **Step 11: Close the Build Loop**

```bash
scripts/process-checklist.sh --complete-step build_loop:feature_recorded
scripts/test-gate.sh --record-feature "keys-zone-alarm"
```

---

## Self-Review

**Spec coverage.** Components → Tasks 1–4. Daily cheap path → Task 3 `sample_cache` (listdir only). `CACHE_INDEX_SIZE` from `/proc` → Task 3 `nginx_index_size_mb`. Thresholds → Task 2 `verdict` + Task 3 `DEFAULTS`. Tripwire promotion → Task 4. Kuma monitors + binding → Task 5 Step 5. Failure modes (failed sample DOWN, unknown-not-safe, every run pushes) → Task 2 tests. Testing → Tasks 1–2. Rollout → Task 5.

**One spec item is deliberately deferred:** the **weekly mean-object-size measurement**, which the spec describes as context-only and never alarmed on. It is not on the critical path for the alarm, needs the expensive stat walk, and would be its own task. **Add it as a follow-up issue rather than silently dropping it** — without it, `disk_keys` reports `unknown`, which the spec already permits.

**Type consistency.** `Sample`, `Ceiling`, `Verdict` are used with the same field names in Tasks 2 and 3. `push(url, status, msg)` matches between Tasks 1, 3 and 4. `load_cfg`/`probe_loop` names match between Tasks 3 and 4.

**Placeholder scan.** No TBDs. Every code step carries real code. Task 4 Step 2 asks the implementer to confirm the actual `kind` strings at lines 277/293/311 rather than trusting mine — that is a verification instruction, not a placeholder, and it exists because getting it wrong would make a commanded purge flip the monitor, re-creating #337.
