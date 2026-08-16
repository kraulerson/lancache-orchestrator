# Manifest Locator Depot-Glob Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix `locate_manifest_bins` so it discovers every one of a Steam game's depot manifests, not just the one whose filename happens to repeat the app ID — closing a chronic false-partial bug that has kept Grim Dawn, Jedi: Survivor, and Battlefield 2042 stuck at `validation_failed` regardless of successful re-prefills.

**Architecture:** One glob pattern in `manifest_locator.py` currently assumes every depot's `.bin` filename is `{app_id}_{app_id}_{depot}_{gid}.bin`. Real secondary-depot filenames are `{app_id}_{depotGroupId}_{depot}_{gid}.bin` where `depotGroupId != app_id`. The fix widens the `.bin` glob to match on the app ID once (not the `.shas` glob, which stays narrow — that extension's naming is fixed and orchestrator-controlled). No other code changes: `_steam_chunk_paths` (validate + purge, both callers) and the sweep already consume `locate_manifest_bins`'s output generically.

**Tech Stack:** Python 3.12, pytest, FastAPI TestClient, pathlib globbing.

**Spec:** `docs/superpowers/specs/2026-08-16-manifest-locator-depot-glob-design.md` — approved after 5 rounds of independent adversarial review (round 5: APPROVE). Read §2 (root cause), §3 (fix), §7 (purge), §6/§8 (rollout — Task 6 below) before starting.

---

## Global Constraints

- **Interpreter:** `.venv/bin/python`. Bare `python` is NOT on PATH.
- **Run tests:** `.venv/bin/python -m pytest tests/... -q`
- **Run the FULL suite** as: `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest -q`
  The `PATH` prefix is REQUIRED — without it `tests/test_licenses.py` fails on a missing `pip-licenses` binary (an environment artifact, not a real failure).
- **Lint/format/types must be clean before every commit:**
  `.venv/bin/python -m ruff check src/ tests/`
  `.venv/bin/python -m ruff format src/ tests/`
  `.venv/bin/python -m mypy src/` (strict)
- **TDD is mandatory.** Write the test, RUN IT AND WATCH IT FAIL, then implement. A test that passes immediately proves nothing.
- **FRAMEWORK HOOKS are active and will block you if skipped:**
  1. Before editing any file under `src/`, invoke a Superpowers skill (`superpowers:test-driven-development`). The marker resets after every commit — re-invoke before each task's source edits.
  2. Before every `git commit`: present the change, get explicit approval, then run **from the repo root, as its own command, with a relative path**:
     `.claude/framework/hooks/mark-evaluated.sh "short reason"`
     No shell-special characters in the reason — no `;`, `&`, `|`, quotes, apostrophes.
  3. Never edit anything under `.claude/`, never access it via Bash.
  4. Before source edits, a plan task must be `in_progress` via `TaskUpdate` — the marker for this also appears to reset periodically; re-run `TaskUpdate` on the active task if a `[Planning Zone]` block appears.
- **Line length 100, double quotes, `from __future__ import annotations`** at the top of new modules (not needed here — no new modules).
- **Commit granularity:** one commit per task, after its tests pass.
- **Every citation in this plan was re-verified against `main` at commit `eb52cd9` immediately before writing it** — the design spec that preceded this plan went through 4 rounds of citation drift before converging; do not assume a citation is still correct without checking it yourself if time has passed.

---

## File Structure

| File | Responsibility | Tasks |
|---|---|---|
| `src/orchestrator/agent/manifest_locator.py` | The fix itself: per-extension glob pattern | 1 |
| `tests/agent/test_manifest_locator.py` | Unit coverage: differing-group-id shape, realistic multi-depot scale, cross-app collision | 1 |
| `tests/agent/test_steam_validate.py` | Integration: `/v1/steam/validate` sees the widened depot set | 2 |
| `tests/agent/test_steam_purge.py` | Integration: `/v1/steam/purge` sees the widened depot set; shared-redist exclusion still holds under the new naming shape | 3 |
| `src/orchestrator/agent/manifest_locator.py`, `src/orchestrator/agent/routers/steam.py` | Two docstrings that describe the old universal naming assumption | 4 |
| `CHANGELOG.md` | Record the fix | 5 |
| *(none — operational)* | Rollout runbook, human-executed | 6 |

---

## Task 1: The fix + unit tests

**Files:**
- Modify: `src/orchestrator/agent/manifest_locator.py:68-69`
- Test: `tests/agent/test_manifest_locator.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `locate_manifest_bins` now returns `.bin` candidates for every depot regardless of the filename's second segment. No signature change — `_steam_chunk_paths` (Tasks 2, 3) and every existing caller are unaffected by this task alone.

**Current code** (verified against `main` immediately before writing this plan):
```python
        for ext in _MANIFEST_EXTS:
            for path in v1.glob(f"{app_id}_{app_id}_*.{ext}"):
                parts = path.stem.split("_")
                if len(parts) != 4:
                    continue
                candidates.setdefault(parts[2], []).append(path)
```

- [ ] **Step 1: Write the failing tests**

Append to `tests/agent/test_manifest_locator.py` (after the last existing test, `test_shas_only_app_in_list_prefilled_app_ids`):

```python
# --- Differing-group-id .bin naming (real SteamPrefill secondary-depot shape) ---
#
# SteamPrefill names a game's PRIMARY depot {app}_{app}_{depot}_{gid}.bin, but a
# secondary depot is {app}_{depotGroupId}_{depot}_{gid}.bin where depotGroupId is
# frequently a DIFFERENT number from app_id (a Steam-internal grouping, not the
# app id). The old glob only matched the first shape.


def test_locates_bin_with_differing_group_id_segment(tmp_path):
    # depot 229003's "group id" (228980) differs from the app id (219990) --
    # this exact shape was found live on Grim Dawn.
    _write(tmp_path, "219990_228980_229003_8740933542064151477.bin", 1000)
    found = [p.name for p in locate_manifest_bins(219990, cache_roots=[tmp_path])]
    assert found == ["219990_228980_229003_8740933542064151477.bin"]


def test_mix_of_matching_and_differing_group_id_depots(tmp_path):
    # One depot matches the old app_id-repeated pattern, one doesn't -- both
    # must be found, matching the real Grim Dawn shape (1 of 9 depots matched
    # the old glob; the other 8 didn't).
    _write(tmp_path, "219990_219990_219991_111.bin", 1000)  # matches old pattern
    _write(tmp_path, "219990_483840_483840_222.bin", 1000)  # differing group id
    found = sorted(p.name for p in locate_manifest_bins(219990, cache_roots=[tmp_path]))
    assert found == [
        "219990_219990_219991_111.bin",
        "219990_483840_483840_222.bin",
    ]


def test_realistic_nine_depot_shape(tmp_path):
    # Reproduces Grim Dawn's real depot layout: 9 depots, only 1 whose group id
    # equals the app id. A minimal 2-depot test could miss an ordering or
    # dedup bug that only shows up at real scale.
    depots = [
        ("219990", "219991", "1"),  # matches old pattern (group id == app id)
        ("228980", "229003", "2"),  # differs
        ("483840", "483840", "3"),
        ("642280", "642280", "4"),
        ("642280", "642281", "5"),
        ("897670", "897670", "6"),
        ("897670", "897671", "7"),
        ("2699230", "2699230", "8"),
        ("2699230", "2699231", "9"),
    ]
    expected_depots = set()
    for group, depot, gid in depots:
        _write(tmp_path, f"219990_{group}_{depot}_{gid}.bin", 1000)
        expected_depots.add(depot)
    found_depots = {p.stem.split("_")[2] for p in locate_manifest_bins(219990, cache_roots=[tmp_path])}
    assert found_depots == expected_depots
    assert len(found_depots) == 9


def test_shas_glob_stays_narrow_for_differing_group_id_name(tmp_path):
    # The .shas extension must NOT widen -- the fetcher never actually writes
    # this shape (it always repeats app_id), but if a file like this existed
    # it must still be excluded, proving the two extensions are scoped
    # independently rather than sharing one widened pattern.
    _write(tmp_path, "219990_228980_229003_111.shas", 1000)
    assert locate_manifest_bins(219990, cache_roots=[tmp_path]) == []


# --- Cross-app-ID collision safety (the load-bearing safety argument for the
#     whole fix gets its own explicit regression test, not just informal
#     verification in the design doc) ---


def test_widened_glob_does_not_collide_short_app_id_is_prefix_of_long(tmp_path):
    # App 44's widened glob ("44_*") must not match app 440's files -- the
    # character immediately after "44" in "440_..." is "0", not "_".
    _write(tmp_path, "44_44_45_111.bin", 1000)
    _write(tmp_path, "440_440_441_222.bin", 1000)
    found = [p.name for p in locate_manifest_bins(44, cache_roots=[tmp_path])]
    assert found == ["44_44_45_111.bin"]


def test_widened_glob_does_not_collide_long_app_id_is_prefix_of_short(tmp_path):
    # The reverse direction: app 4400's glob ("4400_*") must not match a file
    # for app 44 whose second segment happens to start with "400".
    _write(tmp_path, "44_400_401_111.bin", 1000)
    _write(tmp_path, "4400_4400_4401_222.bin", 1000)
    found = [p.name for p in locate_manifest_bins(4400, cache_roots=[tmp_path])]
    assert found == ["4400_4400_4401_222.bin"]
```

- [ ] **Step 2: Run and watch them fail**

Run: `.venv/bin/python -m pytest tests/agent/test_manifest_locator.py -q -k "differing_group or nine_depot or shas_glob_stays or collision"`
Expected: 6 tests FAIL. `test_locates_bin_with_differing_group_id_segment` and the two "mix"/"nine_depot" tests fail because the old glob finds nothing for the differing-group-id files (assertion mismatch, empty or partial result). `test_shas_glob_stays_narrow...` and both collision tests currently PASS by coincidence (the old code already doesn't match these shapes/doesn't collide) — that's fine, they're regression guards for the *new* code, not proof of the old bug; note in your run output which of the 6 actually failed vs already-passed, and don't be alarmed if it's not all 6.

- [ ] **Step 3: Implement the fix**

In `src/orchestrator/agent/manifest_locator.py`, replace exactly this block (inside `locate_manifest_bins`, currently lines 68-69):

```python
        for ext in _MANIFEST_EXTS:
            for path in v1.glob(f"{app_id}_{app_id}_*.{ext}"):
```

with:

```python
        for ext in _MANIFEST_EXTS:
            # SteamPrefill's OWN naming only repeats app_id for a game's primary
            # depot: {app}_{app}_{depot}_{gid}.bin. A secondary depot is
            # {app}_{depotGroupId}_{depot}_{gid}.bin, where depotGroupId is
            # frequently a DIFFERENT number -- a Steam-internal grouping, not
            # the app id. Widening only .bin to match the app id once (not
            # twice) picks up every depot; .shas keeps the narrow pattern
            # because that extension is written exclusively by this project's
            # own fetcher (manifest_fetcher.py), whose naming is fixed and
            # already correct.
            pattern = f"{app_id}_*.{ext}" if ext == "bin" else f"{app_id}_{app_id}_*.{ext}"
            for path in v1.glob(pattern):
```

- [ ] **Step 4: Run and verify all pass**

Run: `.venv/bin/python -m pytest tests/agent/test_manifest_locator.py -q`
Expected: PASS — all 6 new tests plus every pre-existing test in the file (24 before this task; confirm the count grows by 6 and nothing regresses).

- [ ] **Step 5: Full verification**

```bash
.venv/bin/python -m ruff check src/ tests/
.venv/bin/python -m ruff format src/ tests/
.venv/bin/python -m mypy src/
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest -q
```

- [ ] **Step 6: Get approval, mark, and commit**

```bash
.claude/framework/hooks/mark-evaluated.sh "widen the bin glob to find every depot manifest not just the primary one"
git add src/orchestrator/agent/manifest_locator.py tests/agent/test_manifest_locator.py
git commit -m "fix(steam): find every depot's manifest, not just the primary one"
```

---

## Task 2: Integration test — `/v1/steam/validate` sees the widened depot set

**Files:**
- Test: `tests/agent/test_steam_validate.py`

**Interfaces:**
- Consumes: Task 1's fixed `locate_manifest_bins` (no direct import — reached through the real `/v1/steam/validate` endpoint, proving the fix end-to-end).
- Produces: nothing new for later tasks.

**Why:** Task 1 proves the locator in isolation. This proves the fix actually changes what the operator-facing endpoint reports — the whole point of the fix.

- [ ] **Step 1: Write the failing test**

Add to `tests/agent/test_steam_validate.py`, after `test_validate_all_cached` (reuses the module's existing `FIXTURE`, `APP`, `DEPOT`, `GID`, `TOKEN` constants and the `_build` helper's settings shape — don't redefine them):

```python
def test_validate_includes_a_second_depot_with_differing_group_id(tmp_path):
    """A second .bin under a differing-group-id name (real Grim Dawn shape)
    must be found and counted -- proving the widened glob end-to-end through
    the actual endpoint, not just the locator in isolation."""
    mcache = tmp_path / "spcache"
    (mcache / "v1").mkdir(parents=True)
    (mcache / "v1" / f"{APP}_{APP}_{DEPOT}_{GID}.bin").write_bytes(FIXTURE.read_bytes())
    depot2, group2, gid2 = 483840, 483840, 999
    (mcache / "v1" / f"{APP}_{group2}_{depot2}_{gid2}.bin").write_bytes(FIXTURE.read_bytes())

    cfg = tmp_path / "Config"
    cfg.mkdir()
    (cfg / "successfullyDownloadedDepots.json").write_text(json.dumps({str(APP): [GID]}))

    cache_root = tmp_path / "lancache"
    levels, ident, slice_sz = "2:2", "steam", 10_485_760
    slice_range = slice_range_zero(slice_sz)
    for depot_id in (DEPOT, depot2):
        for sha in parse_chunk_shas(FIXTURE.read_bytes()):
            h = cache_key(ident, steam_chunk_uri(depot_id, sha), slice_range)
            p = cache_path(cache_root, h, levels)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"data")

    settings = Settings(
        orchestrator_token=TOKEN,
        lancache_nginx_cache_path=cache_root,
        cache_levels=levels,
        steam_cache_identifier=ident,
        cache_slice_size_bytes=slice_sz,
        steam_manifest_cache_dir=mcache,
        steam_prefill_config_dir=cfg,
    )
    client = TestClient(create_agent_app(settings=settings))
    client.headers.update({"Authorization": f"Bearer {TOKEN}"})

    body = client.post("/v1/steam/validate", json={"app_id": APP}).json()
    # FIXTURE has 60 chunks; two depots fully cached -> 120 total, 120 cached.
    # Before the fix, the second depot was invisible and this would read 60/60.
    assert body["chunks_total"] == 120
    assert body["chunks_cached"] == 120
    assert body["outcome"] == "cached"
```

- [ ] **Step 2: Run and watch it fail**

Run: `.venv/bin/python -m pytest tests/agent/test_steam_validate.py -q -k differing_group_id`

This test targets code Task 1 already fixed and committed — it will most likely PASS immediately. That is not a TDD violation to address retroactively, but a passing-on-first-run test proves nothing about whether it actually exercises the fix. **Verify it by toggling the fix line off and back on** (NOT `git stash` — Task 1's change is committed, so stash has nothing uncommitted to hide):

```bash
# Temporarily restore the OLD buggy pattern to prove the test would have caught it:
```
Use the Edit tool to temporarily change `manifest_locator.py`'s widened line back to the original bug:
```python
            pattern = f"{app_id}_{app_id}_*.{ext}"
```
(replacing the `if ext == "bin" else ...` conditional entirely — this is a scratch edit, not a commit)
```bash
.venv/bin/python -m pytest tests/agent/test_steam_validate.py -q -k differing_group_id
```
Expected: FAIL (`chunks_total == 60`, not 120) — confirms the test is real. Then use Edit to restore the fixed conditional exactly as Task 1 left it, and confirm:
```bash
.venv/bin/python -m pytest tests/agent/test_steam_validate.py -q -k differing_group_id
```
Expected: PASS again.

- [ ] **Step 3: Run and verify it passes on the fixed code**

Run: `.venv/bin/python -m pytest tests/agent/test_steam_validate.py -q`
Expected: PASS — the new test plus every pre-existing test in the file.

- [ ] **Step 4: Full verification, approval, mark, commit**

```bash
.venv/bin/python -m ruff check src/ tests/ && .venv/bin/python -m ruff format src/ tests/ && .venv/bin/python -m mypy src/
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest -q
.claude/framework/hooks/mark-evaluated.sh "add validate integration test proving the second depot is now counted"
git add tests/agent/test_steam_validate.py
git commit -m "test(steam): validate counts a second depot with a differing group id"
```

---

## Task 3: Integration tests — `/v1/steam/purge` sees the widened depot set

**Files:**
- Test: `tests/agent/test_steam_purge.py`

**Interfaces:**
- Consumes: Task 1's fix, reached through `/v1/steam/purge` (which shares `_steam_chunk_paths` with validate — no code change of its own needed).
- Produces: nothing new for later tasks.

**Why (design spec §7):** `_steam_chunk_paths` (`steam.py:244-319`) is shared verbatim by both routers. Task 1's fix widens what purge deletes too — files a differing-group-id secondary depot's chunks, previously invisible to purge, now get deleted along with the rest of the game. `tests/agent/test_steam_purge.py` already has 6 tests (do not claim otherwise, or duplicate `test_steam_purge_skips_shared_redist_depot` — a prior draft of the design spec made exactly that mistake and a reviewer caught it). Only the differing-group-id case is genuinely new coverage.

- [ ] **Step 1: Write the failing test**

**Correction (found during execution, not caught during planning): depot 2's manifest MUST be a `.bin` file, not `.shas`.** The fix widens only the `.bin` glob — `.shas` deliberately stays narrow (that extension is written exclusively by this project's own fetcher, whose naming is fixed). A `.shas` file under the differing-group-id shape would never be found either before or after the fix, so it wouldn't exercise anything — this was tried first and failed with `deleted == 5` (only depot 1) even after Task 1's fix landed, which is how the mistake was caught. Since a real `.bin` has to be a parseable protobuf, reuse the same fixture `tests/agent/test_steam_validate.py` uses (`tests/agent/fixtures/sample_manifest.bin`) under a second filename — the code trusts the filename's depot id, not the manifest's internal content, so identical bytes work for a different depot. This requires two new top-level imports in `test_steam_purge.py`: `from pathlib import Path` (replacing the `TYPE_CHECKING`-only one) and `from orchestrator.agent.manifest_parser import parse_chunk_shas`, plus a module constant `FIXTURE = Path(__file__).parent / "fixtures" / "sample_manifest.bin"`.

Add to `tests/agent/test_steam_purge.py`, after `test_steam_purge_skips_shared_redist_depot` (reuses the module's `APP`, `DEPOT`, `GID`, `CHUNKS`, `LEVELS`, `IDENT`, `SLICE`, `CHUNK_BODY`, `TOKEN`, `FIXTURE` constants):

```python
def test_steam_purge_deletes_a_second_depot_with_differing_group_id(tmp_path):
    """A differing-group-id secondary depot (real Grim Dawn shape) was
    previously invisible to purge -- the old glob never enumerated it, so
    purge silently left its chunks behind. After the fix, purge must delete
    them too.

    Depot 2 must be a .bin file, not .shas: the fix widens ONLY the .bin
    glob (by design -- .shas is written exclusively by this project's own
    fetcher, whose naming is fixed and already correct), so a .shas file
    under the differing-group-id shape would never be found either before
    or after the fix and wouldn't exercise anything. Reuses the same real
    parseable protobuf fixture test_steam_validate.py uses, under a second
    filename -- the code trusts the FILENAME's depot id, not the manifest's
    internal content, so the same bytes work for a different depot.
    """
    depot2, group2, gid2 = 483840, 483840, 999999
    chunks2 = list(parse_chunk_shas(FIXTURE.read_bytes()))

    mcache = tmp_path / "spcache"
    (mcache / "v1").mkdir(parents=True)
    (mcache / "v1" / f"{APP}_{APP}_{DEPOT}_{GID}.shas").write_text("\n".join(CHUNKS) + "\n")
    (mcache / "v1" / f"{APP}_{group2}_{depot2}_{gid2}.bin").write_bytes(FIXTURE.read_bytes())
    cfg = tmp_path / "Config"
    cfg.mkdir()
    (cfg / "successfullyDownloadedDepots.json").write_text("{}")

    cache_root = tmp_path / "lancache"
    slice_range = slice_range_zero(SLICE)
    all_files = []
    for depot_id, shas in ((DEPOT, CHUNKS), (depot2, chunks2)):
        for sha in shas:
            p = cache_path(
                cache_root, cache_key(IDENT, steam_chunk_uri(depot_id, sha), slice_range), LEVELS
            )
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(CHUNK_BODY)
            all_files.append(p)

    settings = Settings(
        orchestrator_token=TOKEN,
        lancache_nginx_cache_path=cache_root,
        cache_levels=LEVELS,
        steam_cache_identifier=IDENT,
        cache_slice_size_bytes=SLICE,
        steam_manifest_cache_dir=mcache,
        steam_prefill_config_dir=cfg,
    )
    client = TestClient(create_agent_app(settings=settings))
    client.headers.update({"Authorization": f"Bearer {TOKEN}"})

    body = client.post("/v1/steam/purge", json={"app_id": APP}).json()

    assert body["deleted"] == len(CHUNKS) + len(chunks2)
    assert all(not p.exists() for p in all_files)
```

- [ ] **Step 2: Run and confirm it exercises the fix**

Run: `.venv/bin/python -m pytest tests/agent/test_steam_purge.py -q -k differing_group_id`

Same reasoning and same mechanism as Task 2 Step 2 (NOT `git stash` — Task 1's change is committed). Use Edit to temporarily restore the old buggy pattern in `manifest_locator.py` (`pattern = f"{app_id}_{app_id}_*.{ext}"`, dropping the conditional), run:
```bash
.venv/bin/python -m pytest tests/agent/test_steam_purge.py -q -k differing_group_id
```
Expected: FAIL (`deleted == len(CHUNKS)` only — depot2's 5 chunks missing). Then use Edit to restore the fixed conditional exactly as Task 1 left it, and confirm PASS again.

- [ ] **Step 3: Run the full purge file and verify all pass**

Run: `.venv/bin/python -m pytest tests/agent/test_steam_purge.py -q`
Expected: PASS — 7 tests total (6 existing + 1 new).

- [ ] **Step 4: Full verification, approval, mark, commit**

```bash
.venv/bin/python -m ruff check src/ tests/ && .venv/bin/python -m ruff format src/ tests/ && .venv/bin/python -m mypy src/
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest -q
.claude/framework/hooks/mark-evaluated.sh "add purge integration test proving the second depot chunks are now deleted"
git add tests/agent/test_steam_purge.py
git commit -m "test(steam): purge deletes a second depot with a differing group id"
```

---

## Task 4: Fix the two docstrings that describe the old universal naming assumption

**Files:**
- Modify: `src/orchestrator/agent/manifest_locator.py:3-4`
- Modify: `src/orchestrator/agent/routers/steam.py:259-260`

**Interfaces:** None — doc-only, no behavior change, no test needed (nothing to assert against; this is a comment-accuracy fix flagged by round-5 review as a non-blocking nit).

**Why:** Both docstrings currently claim `.bin` and `.shas` "both named `{app}_{app}_{depot}_{gid}.<ext>`" — true for `.shas` (fixed, fetcher-controlled), no longer true for `.bin` after Task 1.

- [ ] **Step 1: Fix `manifest_locator.py`'s module docstring**

Current (lines 3-4):
```python
Two manifest formats live side by side under <cache_root>/v1/, both named
{app}_{app}_{depot}_{gid}.<ext>:
```

Replace with:
```python
Two manifest formats live side by side under <cache_root>/v1/:
  * .shas is always named {app}_{app}_{depot}_{gid}.shas (fixed, written only
    by this project's own fetcher — see manifest_fetcher.py).
  * .bin is SteamPrefill's own naming, which repeats the app id only for a
    game's primary depot ({app}_{app}_{depot}_{gid}.bin); a secondary depot is
    {app}_{depotGroupId}_{depot}_{gid}.bin, where depotGroupId is frequently a
    DIFFERENT number. Both shapes are exactly 4 underscore-separated segments.
```

- [ ] **Step 2: Fix `_steam_chunk_paths`'s docstring in `steam.py`**

Current (lines 258-260):
```python
    skipped, never fatal (COR-1). ``.shas`` is the fetcher's sidecar (one SHA per
    line); ``.bin`` is SteamPrefill's protobuf — same
    ``{app}_{app}_{depot}_{gid}`` filename layout.
    """
```

Replace with:
```python
    skipped, never fatal (COR-1). ``.shas`` is the fetcher's sidecar (one SHA per
    line), always ``{app}_{app}_{depot}_{gid}.shas``. ``.bin`` is SteamPrefill's
    protobuf; only a game's PRIMARY depot repeats the app id in that shape — a
    secondary depot is ``{app}_{depotGroupId}_{depot}_{gid}.bin`` with a
    differing group id. See ``manifest_locator.py`` module docstring.
    """
```

- [ ] **Step 3: Run the full suite to confirm nothing depends on the old docstring text**

Run: `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest -q`
Expected: PASS, identical count to Task 3's final run (docstrings aren't asserted on anywhere).

- [ ] **Step 4: Lint/format/type check, approval, mark, commit**

```bash
.venv/bin/python -m ruff check src/ tests/ && .venv/bin/python -m ruff format src/ tests/ && .venv/bin/python -m mypy src/
.claude/framework/hooks/mark-evaluated.sh "correct two docstrings that described the old universal bin naming assumption"
git add src/orchestrator/agent/manifest_locator.py src/orchestrator/agent/routers/steam.py
git commit -m "docs(steam): correct docstrings describing the old bin naming assumption"
```

---

## Task 5: CHANGELOG

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Read the current `## [Unreleased]` section header**

Run: `head -30 CHANGELOG.md` to confirm the exact current top-of-file structure before inserting (other work may have added entries above this plan's start; insert as the newest entry, immediately under `## [Unreleased]`).

- [ ] **Step 2: Add the entry**

```markdown
### Fixed — Steam validation and purge silently ignored most of a multi-depot game's secondary depots — 2026-08-16

`locate_manifest_bins` globbed `.bin` manifests as `{app_id}_{app_id}_*` — true only for a game's primary depot. SteamPrefill names secondary depots `{app_id}_{depotGroupId}_{depot}_{gid}.bin` with a differing group id, so the old glob silently excluded them as candidates. Live investigation found Grim Dawn's 9 real depots: only 1 matched the old pattern; the locator fell back to a 51-day-old `.shas` sidecar for 5 more and found nothing at all for the remaining 3 — explaining why Grim Dawn, STAR WARS Jedi: Survivor, and Battlefield 2042 stayed `validation_failed` despite the host Steam prefill cron completing successfully every 6h. Design: `docs/superpowers/specs/2026-08-16-manifest-locator-depot-glob-design.md` (5 rounds of independent adversarial review).

- **Fixed:** the `.bin` glob widens to match the app id once instead of twice; `.shas` is unchanged (its naming is fixed, written only by this project's own fetcher).
- **Also affects `/v1/steam/purge`:** it shares the same lookup (`_steam_chunk_paths`), so purging a multi-depot game now deletes every depot's chunks, not just the ones the old glob happened to see. The shared-redist cross-game-deletion protection (`steam_shared_redist_depots`, #245) is keyed by depot id inside that shared function, independent of which glob found the file, so it is unaffected.
- **Deploy note:** every currently-`up_to_date` owned Steam game is re-validated on the next scheduled sweep tick (its candidate query already includes `up_to_date`, not just `validation_failed`) — see the design spec §6/§8 for the rollout runbook and decision rule.
```

- [ ] **Step 3: Approval, mark, commit**

```bash
.claude/framework/hooks/mark-evaluated.sh "add changelog entry for the manifest locator depot glob fix"
git add CHANGELOG.md
git commit -m "docs: changelog for the manifest locator depot-glob fix"
```

- [ ] **Step 4: Open the PR**

```bash
git push -u origin <branch-name>
```
Then open a PR summarizing Tasks 1-5, linking the design spec, and noting Task 6 is a separate, human-executed rollout step once this merges and deploys — do not merge it yourself.

---

## Task 6: Rollout runbook (human-executed, not code — do NOT automate this)

This is operational, not a coding task. It runs on the live LXC (`10.100.23.105`) after Tasks 1-5 merge and the orchestrator image is redeployed — a deploy this plan does not trigger. Per the design spec §6/§8:

1. **Before deploying**, confirm no sweep is currently in flight:
   ```sql
   SELECT id FROM jobs WHERE kind='sweep' AND state='running';
   ```
   If that returns a row, wait for it to clear before deploying — a deploy mid-sweep validates with the OLD code and can be killed mid-run, reaped to `failed` at next boot rather than completing.
2. Deploy.
3. Let the next scheduled gated sweep run naturally (`0 3,9,15,21 * * *` — within 6h; no forced trigger, no `cache validate-all`).
4. Read that sweep's own `sweep.completed` structured log line for its `evicted` and `recovered` fields.
5. **Decision rule:** if `evicted` exceeds **20**, treat it as suspicious rather than confirmatory — halt before letting any downstream automation (F8 scheduled prefill, etc.) act on the new statuses, and manually inspect a sample of the flipped games' on-disk manifests. Below that threshold, the flips are expected.
6. `recovered` should include Grim Dawn and Jedi: Survivor. Battlefield 2042 will **not** self-correct — it was separately removed from the Steam selection list around Aug 4-9 (design spec §5) and needs to be re-added to `selectedAppsToPrefill.json` on the NAS as an unrelated, separate operator action.
