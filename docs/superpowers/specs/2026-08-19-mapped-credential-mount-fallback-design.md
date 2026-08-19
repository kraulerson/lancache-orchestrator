# Design — mapped credential mount (fallback architecture for Steam badge custody)

**Date:** 2026-08-19
**Status:** **Designed, not adopted.** The adopted approach is checkout/check-in over the existing agent channel — ADR-0018 §5.11 (v3). This document exists so that if checkout/check-in develops unintended consequences, the alternative is already designed rather than improvised. Operator direction, 2026-08-19: *"go with option 1, but create a full architecture design for option 2, this way if we find that option 1 has unintended consequences, we can rebuild and try option 2."*
**Repo:** lancache_orchestrator. **Branch:** `docs/ownership-adrs` (PR #284)
**Relates to:** ADR-0018 §5.7 (token-rotation safety), §5.11 (Steam credential custody), §5.12 (management UI), §6 (migration sequence)

---

## 1. What this design does

Replaces each Steam tool's on-disk credential location with a **network mount of the
ownership service's credential store**, so the badge exists in exactly one place and no
copy operation is ever performed.

Under the adopted design (option 1), the service holds the authoritative badge in its
database and the NAS agent writes a working copy to the tool's expected path before a
run, reading back any renewal afterwards. Under *this* design, the tool's path **is**
the service's storage, reached over the network. There is no working copy, no
check-in, and no window in which two copies exist.

## 2. Trigger conditions — when to abandon option 1 for this

Adopt this design if any of the following is observed in production. These are the
specific unintended consequences option 1 is exposed to:

| # | Symptom | Why it favours this design |
| --- | --- | --- |
| T1 | A renewal is lost because check-in raced a still-running tool | SteamPrefill is known to hang after finishing (live finding, 2026-08-12 — it broke a naive `flock -n` guard on the host cron), so "the run is over" is not cleanly observable. A mount has no check-in to race. |
| T2 | Badge divergence between vault and host after an agent crash mid-run | With one file, divergence is structurally impossible. |
| T3 | Operator objects to the badge resting on the consumer host's disk | Under a mount the badge is never at rest on the NAS — a genuine security advantage of this design (§13). |
| T4 | Checkout latency or failure becomes a material share of prefill failures | A mount removes two network operations per run and replaces them with demand paging. |

If none of these appear, option 1 stands. This design is not better on balance; it is
better on specific axes, and §13 states them honestly.

## 3. Scope

**In scope:** credentials #2 (SteamPrefill `account.config`) and #3 (DepotDownloader
`-remember-password` session), per the ADR-0018 §5.11 inventory.

**Explicitly out of scope:**

- **Credential #1, the Steam Web API key.** It is never lent to any consumer under
  either design — the service calls `GetOwnedGames` itself and publishes entitlements
  (§5.2). No mount is involved. This does not change.
- **Merging #2 and #3 into one badge.** Rejected in §5.11 and still rejected here: two
  programs sharing one mutable token must be serialised to renew safely. This design
  keeps them distinct, each with its own mount, each with exactly one writer. That
  property is what makes the mount safe.
- **Hosting SteamPrefill on the service.** It is a data-plane workload and stays on the
  NAS (re-arch ②/④).
- **Eliminating attended 2FA.** A vault can hold and restore a badge; it can never mint
  one. Unchanged under every design.

## 4. Architecture

```
  Service host (LXC, 10.100.23.x)              NAS (192.168.1.30)
  ┌────────────────────────────────┐          ┌──────────────────────────────┐
  │ ownership service (container)  │          │ SteamPrefill (host binary)   │
  │  ├─ DB: metadata only          │          │   reads Config/              │
  │  │   (expiry, health, audit)   │          │        ▲                     │
  │  └─ /srv/vault/  ◄─ AUTHORITATIVE         │        │ bind                │
  │       ├─ steamprefill/         │          │   /mnt/vault/steamprefill/   │
  │       └─ depotdownloader/      │          │        ▲                     │
  └──────────────┬─────────────────┘          │        │ encrypted mount     │
                 └────────────────────────────┴────────┘
                     SMB3 w/ seal, or sshfs (§6)
```

**The single most important architectural consequence:** under this design the
**exported directory is the source of truth**, not the service's database. The DB holds
only metadata — expiry, last-renewal time, health, audit trail. This inverts option 1,
where the DB blob is authoritative and the filesystem holds a disposable copy. Any
implementation must not try to keep both authoritative; that is the divergence bug this
design exists to avoid, reintroduced by the back door.

The management UI (§5.12) reads metadata from the DB and, for credential *replacement*
(the attended re-auth flow), writes the exported file directly.

## 5. Per-credential mount design — the two are not symmetric

### 5.1 Credential #3, DepotDownloader — clean

DepotDownloader's entire state lives in one Docker volume, `depotdownloader-config`,
mounted into the agent at `/depotdownloader-config`. Its credential sits at a
runtime-computed .NET IsolatedStorage path beneath it
(`.local/share/IsolatedStorage/<hash>/<hash>/<hash>/AssemFiles/account.config`,
`manifest_fetcher.py:66-71`).

**Design: mount the whole volume from the vault.** Because that volume holds *nothing
but* DepotDownloader's own state, the hash-path fragility disappears entirely — we
never need to know or reproduce the hash, because the runtime computes it on a
filesystem that happens to be remote. A DepotDownloader version bump that changes the
hash is handled by the runtime, as it is today.

This is strictly simpler than option 1, which must locate the hash path by glob in
order to write into it.

### 5.2 Credential #2, SteamPrefill — the awkward one

SteamPrefill reads `Config/account.config`. Two properties make a narrow mount
impossible:

1. **Renewal probably replaces the file — unverified, and load-bearing.** The badge
   was replaced once (2025-07-28 → 2026-04-14), same path, new mtime, same 513-byte
   size. Temp-file-plus-rename is the usual safe-write pattern and is consistent with
   that, but **it was not observed**: an in-place rewrite fits the same evidence. If it
   *is* a rename, the inode swap **detaches a single-file bind mount** and the tool
   writes somewhere nothing else can see — so neither a single-file mount nor a symlink
   at `Config/account.config` survives (rename replaces the symlink itself), and the
   whole directory must be mounted. **If it turns out to be an in-place rewrite, a
   single-file mount becomes viable and §5.2's neighbour problem disappears entirely** —
   which would make this design materially better than it currently looks. Resolve
   before adopting: **OQ6**.
2. **`Config/` has hot neighbours.** The same directory holds
   `successfullyDownloadedDepots.json` — **176,498 bytes, rewritten every run** (4×/day)
   — plus `selectedAppsToPrefill.json` and six backup copies.

**Design: mount the whole `Config/` directory, and accept the neighbours.** Bandwidth
is not the issue (~700 KB/day). The issues are write latency on every run and the
correctness of a frequently-rewritten JSON file over a network filesystem. Mitigations:

- **M1.** Move the six `.bak*` files out of `Config/` before cutover; they are operator
  backups, not tool state, and they inflate the mount for no reason.
- **M2.** Treat `successfullyDownloadedDepots.json` as expendable, which it already is —
  `manifest_fetcher.py:111-114` records that its keys are unreliable and the fetcher
  deliberately does not use it. If it is corrupted by a partial write, SteamPrefill
  rebuilds it; nothing downstream trusts it.
- **M3.** Verify before cutover that SteamPrefill tolerates its `Config/` being remote.
  **Unproven — see §14 OQ1.**

If M3 fails, this design applies to credential #3 only, and #2 stays on
checkout/check-in. That is an acceptable partial outcome, not a blocker.

## 6. Transport — the wire must be encrypted and authenticated

NFSv3 with `AUTH_SYS` is rejected outright: cleartext on the wire, and the server
believes whatever UID the client asserts. A full Steam account session must not travel
that way. Three viable transports:

| Option | Encrypted | Auth | Cost | Verdict |
| --- | --- | --- | --- | --- |
| **SMB3 with `seal`** | Yes (per-session encryption) | User/password or Kerberos | Samba on the service host; `cifs-utils` on the NAS | **Recommended.** Simplest encrypted option; UGREEN already speaks SMB. |
| **sshfs** | Yes | SSH key | FUSE — needs `/dev/fuse` and `CAP_SYS_ADMIN` in a container, so mount on the *host* and bind into the container | Viable fallback; `sshd` already runs on the service host |
| NFSv4 + Kerberos (`krb5p`) | Yes | Kerberos | Requires a KDC | Rejected — disproportionate for a two-host homelab |

**Direction matters.** The NAS is the *client*; the service host is the *server*. Note
that LXC→NAS SSH currently fails host-key verification (session handoff, 2026-08-17) —
that is the opposite direction to the one this design needs, but NAS→service
connectivity must be verified as a prerequisite rather than assumed.

**Mount options are load-bearing:** `soft`, with a bounded `timeo`/`retrans`. A `hard`
mount that hangs would hang the prefill cron indefinitely, which compounds a known
existing problem — SteamPrefill already hangs after finishing, which is why naive
`flock -n` was insufficient. Failing fast is mandatory.

## 7. Availability — the degraded-mode design

This design's central weakness is that it makes prefill depend on the service being
reachable. That must be designed for, not hoped away.

**Pre-run gate.** The cron wrapper verifies the mount is alive and the badge readable
*before* invoking the tool. If not:

1. **Skip the run**, do not start a download that will fail partway.
2. Emit an `attention_required` health event the service's status board surfaces
   (§5.4), and the existing NAS alert-monitor can subscribe to.
3. Exit non-zero so the failure is visible in cron mail rather than silent.

**Emergency local fallback (optional, operator-enabled).** A read-only copy of the last
known-good badge on NAS local disk, used *only* when the mount is unavailable. This
deliberately reintroduces option 1's copy — as a safety net rather than the normal
path. If enabled, §8's divergence rule becomes mandatory.

**Recommendation: ship the pre-run gate; leave the fallback off by default.** A skipped
prefill is a non-event — the next tick is six hours later and the cache is not
time-critical. A stale badge silently diverging is a much worse failure. Enable the
fallback only if skipped runs become frequent enough to matter.

## 8. Renewal and divergence rules

With the fallback disabled there is one file and one writer per badge, and divergence
cannot occur. The rules below exist only for the fallback path.

- **R1. Newest `iat` wins — *if* the badge is a JWT.** The badge is understood to be a
  refresh-token JWT carrying an issued-at claim, in which case the agent compares `iat`
  on recovery and promotes the newer — decidable without guessing, which is why it
  beats mtime. **The encoding is unverified (OQ6).** If it is not a JWT, R1 falls back
  to mtime plus a successful-login probe, which is weaker and must be stated as such
  rather than silently substituted.
- **R2. Persist-before-use.** Adopted from §5.7 rule 2, unchanged.
- **R3. Previous-badge slot.** Retain the immediately-prior badge in the vault, one
  generation. Adopted from §5.7 rule 3. This is what distinguishes "renewed and lost"
  from "revoked upstream" during incident response, and it is the only thing standing
  between a corrupted renewal and an attended 2FA recovery.
- **R4. No concurrent renewers by construction.** Each badge has exactly one consuming
  program. This design never merges badges, so no distributed lock is required. If a
  future change gives two programs one badge, this rule fails and a lock becomes
  mandatory — flag it at review time.

## 9. Failure modes

| Failure | Behaviour under this design | Behaviour under option 1 (adopted) |
| --- | --- | --- |
| Service host down at cron time | Run skipped, alert raised (§7) | Run proceeds — badge already local |
| Network partition mid-run | `soft` mount errors; tool fails; next tick retries | Run unaffected; check-in fails, retried |
| Renewal during a partition | Renewal fails or lands on fallback copy; R1 reconciles | Renewal lands locally; check-in retried |
| Torn write during renewal | Badge damaged; R3's previous-badge slot recovers it | Same risk, on local disk (lower) |
| Service DB restored from backup | Harmless — DB holds metadata only | **Badge rolled back**; may be dead |
| DepotDownloader version bump changes hash path | Transparent (§5.1) | Glob must re-locate the path |
| Operator re-auths on the NAS directly | Writes straight into the vault; nothing to reconcile | Must be checked in, or is overwritten |

Two rows favour this design (last two, plus the DB-restore row). That is a real result
and it is why this document exists.

## 10. Cutover from option 1

Option 1 leaves the vault already holding authoritative copies, which makes cutover
short:

1. Verify NAS→service transport (§6) and OQ1/OQ2 (§14).
2. Stop the prefill cron and the agent.
3. Export the current badges from the vault to `/srv/vault/{steamprefill,depotdownloader}/`.
4. Move `Config/*.bak*` aside (M1).
5. Mount, with `soft` and a bounded timeout.
6. Run one prefill and one manifest fetch **manually**, confirming login succeeds and
   no file is written to the old local paths.
7. Re-enable cron; disable option 1's checkout/check-in path by configuration, not
   deletion.

## 11. Rollback

Fully reversible, which is the strongest operational argument for trying it:

1. Unmount.
2. Copy the badges from the vault back to the local paths.
3. Re-enable checkout/check-in by configuration.

No data model change, no migration, no re-auth. Rollback is minutes and needs no 2FA —
provided the vault copy is intact, which R3 protects.

## 12. Test plan and acceptance criteria

| # | Test | Pass criterion |
| --- | --- | --- |
| A1 | Prefill run against mounted `Config/` | Login succeeds without 2FA; game downloads |
| A2 | Manifest fetch against mounted DD volume | `.shas` produced; no re-login prompt |
| A3 | **Forced renewal** across the mount | New badge lands in the vault; `iat` advances; next run succeeds |
| A4 | Mount killed before a run | Run is skipped, alert raised, exit non-zero — no partial download |
| A5 | Mount killed *during* a run | Tool fails within the `timeo` budget; cron does not hang |
| A6 | `tcpdump` on the NAS during a run | No credential material in cleartext |
| A7 | Corrupted badge, R3 recovery | Previous-badge slot restores service without attended 2FA |
| A8 | Service DB restored from an old backup | Badges unaffected |

A3 and A5 are the two that would actually fail if this design is wrong. A3 cannot be
scheduled — renewal happens on Steam's timetable — so it must be forced by installing a
near-expiry badge in a scratch account, or accepted as unproven at cutover with R3 as
the net. **Flagged in §14 OQ3.**

## 13. Where this design genuinely beats option 1

Stated plainly, because a fallback design that only lists its own drawbacks is useless
when the time comes to choose it:

1. **The badge never rests on the consumer host.** Under option 1 it is written to NAS
   local disk before every run. Under this design it exists only on the service and in
   the tool's page cache. For a full account session, that is a real reduction in
   at-rest exposure.
2. **Divergence is structurally impossible** (fallback disabled), rather than prevented
   by correct sequencing. Option 1's correctness depends on check-in reliably following
   a run whose completion is hard to observe (T1).
3. **No hash-path handling for DepotDownloader** (§5.1).
4. **Operator re-auth on the host writes straight into the vault** — no reconciliation
   step that can be forgotten.
5. **A service DB restore cannot roll a badge backwards**, because the DB is not
   authoritative for badge bytes.

## 14. Open questions — unproven, must be resolved before adopting

- **OQ1.** Does SteamPrefill tolerate `Config/` on a network filesystem? Unknown. .NET
  file handling over SMB/FUSE is usually fine, but unverified here. Resolution: a
  scratch mount and one prefill run. If it fails, apply this design to #3 only (§5.2).
- **OQ2.** Does .NET IsolatedStorage work correctly on a network mount? Unknown, and
  the more likely of the two to break — the runtime makes ownership and locking
  assumptions. Note this is *also* an open question for option 1, which must write into
  that store; here it is merely differently exposed.
- **OQ3.** Can a renewal be forced for A3, or is it accepted as unproven at cutover?
- **OQ4.** UID mapping across hosts. The agent container runs as `orchestrator`
  (observed: `-rw-r--r-- 1 orchestrator orchestrator` on DD's `account.config`).
  SMB `uid=`/`gid=` mount options handle this, but the exact mapping must be pinned or
  the tools will fail to write.
- **OQ5.** Does the ownership service container gain a filesystem-export
  responsibility, or does the LXC host serve it? Serving from the host keeps the
  container as "a single container other systems connect to" (§5.12) but splits the
  service across two deployment units.
- **OQ6 (blocks §5.2 and R1).** Two unverified facts about the badge, both
  load-bearing here. (a) **Is renewal a rename or an in-place rewrite?** Determines
  whether a single-file mount works — an in-place rewrite makes this design
  substantially simpler and removes the hot-neighbour problem. (b) **Is the badge a
  JWT?** R1's newest-`iat` rule depends on it. Both are answerable by decoding
  `Config/account.config`, which the agent sandbox blocks as a live credential — the
  operator must run it, or an `inotify`/`fanotify` watch on `Config/` during a renewal
  will answer (a) without reading the file at all.

## 15. What is NOT decided by this document

This is a contingency design. It commits to nothing. ADR-0018 §5.11 records the adopted
approach, and this document is referenced there as the fallback. Adopting it later
requires a new decision and an ADR amendment — not merely following this file.
