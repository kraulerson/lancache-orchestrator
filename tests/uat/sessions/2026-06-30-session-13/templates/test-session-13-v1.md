# UAT Session 13 — v1

**Date:** 2026-06-30
**Tester(s):** Claude (agent-driven adversarial review + live-system validation on the boxes)
**Features under test (2 since UAT-12):**
- `steam-auth-status-live` (PR #208) — source the steam platform `auth_status` from the live `/health` signal instead of the stale orphaned DB column.
- `validate-gid-match` (PR #209) — pin steam validate to the prefilled manifest gid + capture agent manifests into the durable archive (fixes false "Partial · N%" badges).

---

## Method

1. **Automated suite** — full `pytest` run (baseline regression).
2. **Adversarial review workflow** — 8 finder dimensions across both features (auth-status correctness, auth-status edge/malicious, locator gid-match, validate wiring, capture safety, settings, secret-exposure, regression/import-isolation), each finding then refute-verified by 2 perspective-diverse skeptics (correctness + reproduce lenses). Findings survive only if ≥1 verifier confirms the defect is real after reading the actual code.
3. **Live-system validation** — observed both features against the deployed control plane (LXC 1105) and data-plane agent (UGREEN .40). Mandatory per the live-validation rule for credentialed integrations.

---

## Results

### Automated suite
- **1316 passed**, 3 deselected, 1 warning. Only failure: `tests/test_licenses.py::test_all_licenses_in_allowlist` — documented local pip-licenses tooling gap, not a code defect.

### Live validation
| Feature | Live observation | Verdict |
|---------|------------------|---------|
| #208 auth-status | `GET /api/v1/platforms` on 1105 → steam `auth_status='ok'` (the stale "expired" is gone); epic `ok`. | ✅ PASS |
| #209 gid-match | "All Hail the Orb" 71%-false → **335/335 true**. Library-wide: 856 steam games full / 246 genuinely partial, all now showing *real* percentages (L4D2 2.4%, Borderlands 3 4.2%, Fallout 4 11%…). | ✅ PASS |
| #209 capture (live) | Agent `HOME=/tmp` confirmed; `/manifest-archive/v1` at **2529 .bin**, newest = `4262310_4262310_4262311_5398521309404131186.bin` (Orb's prefilled gid, captured today 20:50). Capture working live. | ✅ PASS |
| Sweep health | Lone `sweep failed` (job 34297) = ID6 reaper marking the in-flight sweep failed when I redeployed the control container for #209; replaced by running sweep 34299. Expected, not a defect. | ✅ benign |

### Adversarial review — confirmed findings (2 of 3 raised survived verify)

**F1 (SEV-2, #208) — `_live_steam_auth_status` can raise (invariant violation).**
`get_settings()` at `platforms.py:68` sits *outside* the function's try/except, but the docstring promises "Never raises." If `get_settings()` ever raises (e.g. a future runtime `reload_settings()` / `cache_clear()` with invalid config), it propagates uncaught → HTTP 500 on the status page (CLI + Game_shelf dashboard). Latent today (no runtime reload is wired), but a real invariant violation.

**F2 (SEV-2, #209) — manifest-capture HOME assumption unenforced + silent on drift.**
SteamPrefill writes manifests to `$HOME/.cache/SteamPrefill`; the capture reads `steam_prefill_live_cache_dir` (default `/tmp/.cache/SteamPrefill`). They match *only* because the deploy script sets `HOME=/tmp` — nothing in the image or driver enforces it, and `sync_manifests_to_archive` silently returns 0 (logged only at INFO) when the source dir is absent. A future deploy that omits the env would silently re-introduce the false-Partial bug. **Verified not broken live** (HOME=/tmp is set; archive growing), so this is hardening, not live breakage.

**Refuted / not surfaced:** secret-exposure (clean — only public manifest gids are logged), import-isolation (preserved), backward-compat of the locator change (prefilled_gids=None preserves prior behavior), validate 500-safety (downloaded_state wrapped), capture-never-fails-job (wrapped).

---

## Triage

| # | Severity | Decision | Rationale |
|---|----------|----------|-----------|
| F1 | SEV-2 | **Fix Now** | Cheap; makes the documented "Never raises" invariant actually hold on a status-page endpoint. |
| F2 | SEV-2 | **Fix Now** | Eliminates a silent-failure mode in the just-shipped subsystem; make the cache path deterministic by construction + loud on drift. |

No SEV-1. Both fixed test-first in this session (see remediation PR).

## Remediation (test-first)
- **F1:** move `get_settings()` inside the try → returns `None` (falls back to stored value) on any settings failure.
- **F2a (prevention):** `SteamPrefillDriver` sets the subprocess `HOME` from the same `steam_prefill_live_cache_dir` setting the capture reads, so SteamPrefill writes where capture reads regardless of container `HOME`. Identical live behavior (HOME stays `/tmp`).
- **F2b (observability):** capture logs a WARNING when the live cache `/v1` dir is missing after a successful prefill (the unambiguous drift symptom).
