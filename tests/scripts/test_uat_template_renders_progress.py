"""The shared UAT template must show a real progress count on load.

UAT-14. `updateProgress()` was called only from `setResult`, so a correctly generated
page kept the static `0 / 0 completed` in its markup until the tester clicked
something. That is indistinguishable from the generator fault that produced session
14's v1 — a page whose JavaScript died and rendered no scenarios at all, which
presented as headings plus `0 / 0`. The operator's report then was "The test session
html does not have any test instructions in it at all", and a page that looks broken
gets abandoned before anyone checks whether it is.

HONEST SCOPE: this is a structural assertion on a template, not a rendering test. It
checks the load path calls the counter refresh; it cannot prove what a browser paints.
Rendering was verified once by hand with jsdom when session 14's copy was fixed
(10 scenario blocks, steps visible, `0 / 10 completed`). Pinning it here stops the
shared template silently regressing for every future session.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = PROJECT_ROOT / "tests" / "uat" / "templates" / "test-session-template.html"


def _script_body(html: str) -> str:
    match = re.search(r"<script[^>]*>(.*?)</script>", html, re.S)
    assert match is not None, "the template has no <script> block"
    return match.group(1)


def test_the_template_exists() -> None:
    assert TEMPLATE.is_file(), f"missing shared UAT template at {TEMPLATE}"


def test_the_counter_is_refreshed_at_load_not_only_on_a_click() -> None:
    body = _script_body(TEMPLATE.read_text())

    # Strip function bodies' worth of indentation: a top-level call sits in column 0,
    # while the call inside setResult() is indented. That distinction is the whole
    # point — the indented one fires on a click, which is too late.
    top_level = [ln for ln in body.splitlines() if ln.startswith("updateProgress()")]

    assert top_level, (
        "updateProgress() is never called at load, so the header keeps its static "
        "'0 / 0 completed' until the tester clicks a result — the same thing a page "
        "whose JavaScript died looks like, which is how a working checklist gets "
        "mistaken for a broken one."
    )


def test_the_load_refresh_runs_after_the_scenarios_are_rendered() -> None:
    """Order matters: counting before rendering would report 0 of 0 regardless."""
    body = _script_body(TEMPLATE.read_text())
    lines = body.splitlines()

    render = next((i for i, ln in enumerate(lines) if ln.startswith("renderScenarios()")), None)
    refresh = next((i for i, ln in enumerate(lines) if ln.startswith("updateProgress()")), None)

    assert render is not None, "renderScenarios() is not called at load"
    assert refresh is not None, "updateProgress() is not called at load"
    assert refresh > render, "the counter must be refreshed AFTER the scenarios render"
