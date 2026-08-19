"""Guards against the two silent-drift failures that let a vulnerable pin survive.

Both were real. `requirements-dev.txt` carried `idna==3.11` (PYSEC-2026-215) for ten
days while `requirements.txt` had the patched 3.15, because the fix was applied to one
lockfile and nothing compared them. Separately, the licence allowlist is maintained by
hand in two places and had drifted — `tests/test_licenses.py` allowed the SPDX short
form `MPL-2.0` while the CI gate did not.

Neither failure is detectable by any other check in this repo: CI installs the `.txt`
files directly and never diffs them against each other or against the `.in` sources.
"""

from __future__ import annotations

import re
from pathlib import Path

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


def test_shared_packages_pin_the_same_version_in_both_lockfiles() -> None:
    """A package in both lockfiles must be the same version in both.

    Otherwise CI (which installs requirements-dev.txt) tests against a different
    build than production (which installs requirements.txt) — and a security patch
    applied to one file silently fails to reach the other, which is exactly how
    idna 3.11 survived in the dev lockfile after 6656cad patched the runtime one.
    """
    runtime = _parse_pins(REPO_ROOT / "requirements.txt")
    dev = _parse_pins(REPO_ROOT / "requirements-dev.txt")

    assert runtime, "parsed no pins from requirements.txt — the parser is broken"
    assert dev, "parsed no pins from requirements-dev.txt — the parser is broken"

    shared = sorted(set(runtime) & set(dev))
    assert shared, "expected overlapping packages between the two lockfiles"

    skewed = {name: (runtime[name], dev[name]) for name in shared if runtime[name] != dev[name]}

    assert not skewed, (
        "these packages are pinned to different versions in requirements.txt vs "
        f"requirements-dev.txt (runtime, dev): {skewed}"
    )


def _licence_tokens(entries: object) -> set[str]:
    """Split on ';' the way pip-licenses does, so both sides compare identically.

    The CI gate passes one ';'-delimited string to `--allow-only`, which pip-licenses
    splits and strips. The Python set holds one compound entry
    ("Apache Software License; MIT License") that the CI form splits apart, so the
    two are only comparable after normalising both the same way.
    """
    if isinstance(entries, str):
        entries = [entries]
    tokens: set[str] = set()
    for entry in entries:  # type: ignore[union-attr]
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

    match = re.search(r'--allow-only="(?P<licences>[^"]+)"', workflow)
    assert match, "could not find the --allow-only allowlist in ci.yml"

    ci_tokens = _licence_tokens(match.group("licences"))
    test_tokens = _licence_tokens(ALLOWED_LICENSES)

    assert ci_tokens == test_tokens, (
        "licence allowlists have drifted — "
        f"only in ci.yml: {sorted(ci_tokens - test_tokens)}; "
        f"only in tests/test_licenses.py: {sorted(test_tokens - ci_tokens)}"
    )
