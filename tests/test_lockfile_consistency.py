"""Guards against the two silent-drift failures that let a vulnerable pin survive.

Both were real. `requirements-dev.txt` carried `idna==3.11` (PYSEC-2026-215) for **91
days** — 2026-05-20 (`6656cad`, which patched `requirements.txt` to 3.15) to 2026-08-19
— because the fix was applied to one lockfile and nothing compared them. Separately,
the licence allowlist is maintained by hand in two places and had drifted:
`tests/test_licenses.py` allowed the SPDX short form `MPL-2.0` while the CI gate
did not.

Neither failure is detectable by any other check in this repo: CI installs the `.txt`
files directly and never diffs them against each other or against the `.in` sources.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

from tests.test_licenses import ALLOWED_LICENSES

REPO_ROOT = Path(__file__).resolve().parent.parent

# `name==version` at the start of a line, tolerating extras (`uvicorn[standard]==...`)
# and the trailing ` \` that precedes pip-compile's hash block.
_PIN = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)(?:\[[^\]]*\])?==(?P<version>[^\s\\;]+)")


def _normalise(name: str) -> str:
    """PEP 503 normalisation, so `typing_extensions` and `typing-extensions` match."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _parse_pins(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _PIN.match(line)
        if match:
            pins[_normalise(match.group("name"))] = match.group("version")
    return pins


def test_runtime_lockfile_is_contained_in_the_dev_lockfile_at_matching_versions() -> None:
    """Every runtime pin must appear in the dev lockfile, at the same version.

    Otherwise CI (which installs requirements-dev.txt) tests against a different
    build than production (which installs requirements.txt) — and a security patch
    applied to one file silently fails to reach the other, which is exactly how
    idna 3.11 survived in the dev lockfile after 6656cad patched the runtime one.
    """
    runtime = _parse_pins(REPO_ROOT / "requirements.txt")
    dev = _parse_pins(REPO_ROOT / "requirements-dev.txt")

    assert runtime, "parsed no pins from requirements.txt — the parser is broken"
    assert dev, "parsed no pins from requirements-dev.txt — the parser is broken"

    # requirements-dev.in starts with `-r requirements.in`, so every runtime package
    # must appear in the dev lockfile. Comparing only the intersection would let a
    # runtime pin that never reached the dev lockfile vanish from the comparison
    # instead of failing it — while Lint and Test run against an environment missing
    # a production dependency.
    missing = sorted(set(runtime) - set(dev))
    assert not missing, (
        "these packages are pinned in requirements.txt but absent from "
        f"requirements-dev.txt, which is compiled from it: {missing}"
    )

    skewed = {name: (runtime[name], dev[name]) for name in runtime if runtime[name] != dev[name]}

    assert not skewed, (
        "these packages are pinned to different versions in requirements.txt vs "
        f"requirements-dev.txt (runtime, dev): {skewed}"
    )


def _licence_tokens(entries: str | Iterable[str]) -> set[str]:
    """Split on ';' the way pip-licenses does, so both sides compare identically.

    The CI gate passes one ';'-delimited string to `--allow-only`, which pip-licenses
    splits and strips. The Python set holds one compound entry
    ("Apache Software License; MIT License") that the CI form splits apart, so the
    two are only comparable after normalising both the same way.
    """
    items = [entries] if isinstance(entries, str) else list(entries)
    tokens: set[str] = set()
    for entry in items:
        tokens.update(part.strip() for part in entry.split(";") if part.strip())
    return tokens


def test_ci_licence_allowlist_matches_the_one_the_tests_enforce() -> None:
    """The CI gate and tests/test_licenses.py must allow the same licences.

    The list is duplicated by hand in both places with nothing detecting divergence.
    It had already drifted: `MPL-2.0` (which hypothesis reports) was allowed by the
    test and rejected by CI, so pointing the CI gate at the dev lockfile failed until
    the two were reconciled.
    """
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    # findall, not search: a second licence gate added later must be guarded too,
    # rather than silently drifting behind the first one's green result.
    allowlists = re.findall(r'--allow-only="([^"]+)"', workflow)
    assert allowlists, "could not find any --allow-only allowlist in ci.yml"

    for ci_allowlist in allowlists:
        _assert_allowlist_matches(ci_allowlist)


def _assert_allowlist_matches(ci_allowlist: str) -> None:
    ci_tokens = _licence_tokens(ci_allowlist)
    test_tokens = _licence_tokens(ALLOWED_LICENSES)

    assert ci_tokens == test_tokens, (
        "licence allowlists have drifted — "
        f"only in ci.yml: {sorted(ci_tokens - test_tokens)}; "
        f"only in tests/test_licenses.py: {sorted(test_tokens - ci_tokens)}"
    )

    # Token equality alone cannot see a compound entry disappearing from
    # ALLOWED_LICENSES: "Apache Software License; MIT License" splits into two halves
    # that are already separate members, so both sides stay equal while
    # tests/test_licenses.py — which matches the whole CSV column verbatim — starts
    # failing on sniffio and uvloop. Counting segments catches it, because the CI
    # string still encodes one more segment than the set accounts for.
    expected_segments = sum(entry.count(";") + 1 for entry in ALLOWED_LICENSES)
    actual_segments = len([p for p in ci_allowlist.split(";") if p.strip()])

    assert actual_segments == expected_segments, (
        f"ci.yml encodes {actual_segments} allowlist segments but "
        f"tests/test_licenses.py accounts for {expected_segments} — an entry "
        "containing ';' was added to or removed from only one of them"
    )


# Spellings pip-licenses actually emits for the copyleft licences this project will
# not ship. Trove classifiers and SPDX ids, including the "or later" forms, which are
# more common than the plain ones. A floor, not an equality: adding more is fine.
REQUIRED_FAIL_ON = {
    "GNU General Public License (GPL)",
    "GNU General Public License v2 (GPLv2)",
    "GNU General Public License v2 or later (GPLv2+)",
    "GNU General Public License v3 (GPLv3)",
    "GNU General Public License v3 or later (GPLv3+)",
    "GNU Affero General Public License v3",
    "GNU Affero General Public License v3 or later (AGPLv3+)",
    "GPL-2.0",
    "GPL-2.0-only",
    "GPL-2.0-or-later",
    "GPL-3.0",
    "GPL-3.0-only",
    "GPL-3.0-or-later",
    "AGPL-3.0",
    "AGPL-3.0-only",
    "AGPL-3.0-or-later",
}


def test_ci_licence_denylist_covers_every_copyleft_spelling_pip_licenses_emits() -> None:
    """`--fail-on` is the gate that actually stops a GPL dependency, and had no guard.

    `--allow-only` fails a package only when *every* one of its licences is
    disallowed, so a dual-licensed `MIT OR GPL-3.0-only` sails through it. `--fail-on`
    is what catches that — and it carried the same SPDX-vs-trove drift the allowlist
    did, matching only two long-form spellings while pip-licenses emits short forms
    too (this repo's own `hypothesis` reports `MPL-2.0`).
    """
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    denylists = re.findall(r'--fail-on="([^"]+)"', workflow)
    assert denylists, "could not find any --fail-on denylist in ci.yml"

    for denylist in denylists:
        missing = REQUIRED_FAIL_ON - _licence_tokens(denylist)
        assert not missing, (
            f"ci.yml --fail-on omits copyleft spellings pip-licenses emits: {sorted(missing)}"
        )


def test_no_uv_lockfile_is_present() -> None:
    """`uv.lock` must not exist, not merely be gitignored.

    A 142-byte stub declaring the project with zero `[[package]]` entries sat here
    until 2026-08-19. Gitignoring it hid it from `git status` while leaving it on
    disk, so `uv sync` still produced an environment with none of the pinned runtime
    dependencies. `uvx` is invoked by scripts/lib/helpers.sh, so uv can recreate it at
    any time — and the ignore rule guarantees nobody would see it happen.
    """
    stray = REPO_ROOT / "uv.lock"

    assert not stray.exists(), (
        "uv.lock exists. This project pins with pip-compile; a uv lockfile is a "
        "second, contradictory source of pins that `git status` will not show you "
        "because it is gitignored. Delete it."
    )
