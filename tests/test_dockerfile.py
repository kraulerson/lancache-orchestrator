"""Regression tests for the runtime image build.

Static checks over the Dockerfile (no docker build required), so they run in
CI without a Docker daemon.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DOCKERFILE = _REPO_ROOT / "Dockerfile"


def _dockerfile_text() -> str:
    return _DOCKERFILE.read_text()


def test_dockerfile_exists():
    assert _DOCKERFILE.is_file()


def test_no_steam_worker_venv_remains():
    """re-arch ③c: the legacy ValvePython steam-worker venv is gone; the image
    must no longer build or copy it."""
    text = _dockerfile_text()
    assert ".venv-steam-worker" not in text
    assert "requirements-steam-worker" not in text


def test_entrypoint_defaults_to_loopback_not_hardcoded_0_0_0_0():
    """UAT-11 F-INT-3: the image must not hardcode --host 0.0.0.0 (which exposes
    the trigger endpoints to the LAN and fires the non-loopback warning every
    boot). It binds ORCH_API_HOST, defaulting to loopback; operators opt into
    0.0.0.0 explicitly."""
    text = _dockerfile_text()
    entry = next(line for line in text.splitlines() if "uvicorn" in line and "ENTRYPOINT" in line)
    assert '"--host", "0.0.0.0"' not in entry
    assert "ORCH_API_HOST" in entry
    assert "127.0.0.1" in entry  # the secure default


def test_entrypoint_uses_python_m_uvicorn_not_console_script():
    """The venv is copied build->runtime, so the `uvicorn` console-script shebang
    is broken; the entrypoint must invoke `python -m uvicorn` (shebang-independent)
    so the container actually starts (caught live, UAT-11)."""
    text = _dockerfile_text()
    entry = next(line for line in text.splitlines() if "uvicorn" in line and "ENTRYPOINT" in line)
    assert "python -m uvicorn" in entry


def test_venv_console_script_shebangs_rewritten_to_runtime_path():
    """The copied venv's console scripts (incl. the bundled `orchestrator-cli`)
    hardcode the build-stage `#!/build/.venv/bin/python` shebang, which doesn't
    exist in the runtime image. The Dockerfile must rewrite them to /app/.venv so
    `orchestrator-cli` works inside the container (caught live, UAT-11)."""
    text = _dockerfile_text()
    assert "/build/.venv/bin/python" in text  # the broken shebang it rewrites
    # rewritten to the runtime path via sed over the matching scripts
    assert "sed" in text and "/app/.venv/bin/python" in text


_HELPER_CSPROJ = "SteamManifestHelper/SteamManifestHelper.csproj"
_HELPER_SDK = (
    "mcr.microsoft.com/dotnet/sdk:10.0"
    "@sha256:e70cdb7f80b0348f5cb85f19a8f670fca061f033d57eed12fa003d58b0e06317"
)


def _run_blocks() -> list[str]:
    """Each RUN instruction as one string: comments dropped, backslash
    continuations joined."""
    lines = [ln for ln in _dockerfile_text().splitlines() if not ln.lstrip().startswith("#")]
    blocks: list[str] = []
    current: list[str] = []
    for ln in lines:
        if current or ln.startswith("RUN "):
            current.append(ln.rstrip().removesuffix("\\"))
            if not ln.rstrip().endswith("\\"):
                blocks.append(" ".join(part.strip() for part in current))
                current = []
    return blocks


def _helper_commands(verb: str, target: str) -> list[str]:
    """Every `dotnet <verb> ... <target>` command across the RUN blocks."""
    cmds = [c.strip() for block in _run_blocks() for c in block.split("&&")]
    return [c for c in cmds if c.startswith(f"dotnet {verb} ") and target in c]


def test_helper_restore_is_locked_and_keeps_both_rids():
    """#361 NU1004: `dotnet restore -r <rid>` narrows the project's
    RuntimeIdentifiers to one, which no longer matches packages.lock.json
    (linux-arm64;linux-x64), so a locked-mode restore fails. Restore must stay
    locked and must not pass -r; `publish -r` picks the RID afterwards."""
    restores = _helper_commands("restore", _HELPER_CSPROJ)
    assert restores, "no `dotnet restore` of the helper project in the Dockerfile"
    for cmd in restores:
        assert "--locked-mode" in cmd, cmd
        assert " -r " not in f" {cmd} ", cmd
        assert "--runtime" not in cmd, cmd


def test_helper_publish_keeps_steamkit2_replaceable():
    """SteamKit2 is LGPL-2.1: it must ship as a separate, replaceable DLL, so no
    single-file, trimming, AOT or ReadyToRun."""
    publishes = _helper_commands("publish", _HELPER_CSPROJ)
    assert publishes, "no `dotnet publish` of the helper project in the Dockerfile"
    for cmd in publishes:
        assert "-p:PublishSingleFile=false" in cmd, cmd
        assert "-p:PublishTrimmed=false" in cmd, cmd
    text = _dockerfile_text()
    assert "PublishAot" not in text
    assert "PublishReadyToRun" not in text


def test_helper_sdk_image_pinned_by_digest():
    from_lines = [ln for ln in _dockerfile_text().splitlines() if ln.startswith("FROM ")]
    helper = [ln for ln in from_lines if ln.rstrip().endswith(" AS helper")]
    assert len(helper) == 1
    assert _HELPER_SDK in helper[0]


def test_depotdownloader_is_gone_from_the_image():
    """#361 replaced DepotDownloader (GPL-2.0) with the SteamKit2 helper."""
    text = _dockerfile_text()
    assert "/depotdownloader" not in text
    assert "DepotDownloader" not in text
    assert "DEPOTDOWNLOADER" not in text


def test_helper_rid_mapping_fails_loud():
    """An unmapped TARGETARCH (empty, arm, 386, riscv64...) must fail the build,
    not silently publish linux-x64 and fail later with an exec-format error."""
    publish_runs = [b for b in _run_blocks() if "dotnet publish" in b]
    assert len(publish_runs) == 1
    run = publish_runs[0]
    assert "amd64) RID=linux-x64" in run
    assert "arm64) RID=linux-arm64" in run
    assert "unsupported TARGETARCH" in run
    assert "exit 1" in run


def test_helper_tests_fail_the_build_when_none_are_discovered():
    """VSTest exits 0 when it discovers no tests; a broken adapter would silently
    void `a failing helper test fails the image build`."""
    tests = _helper_commands("test", "SteamManifestHelper.Tests.csproj")
    assert len(tests) == 1
    assert "RunConfiguration.TreatNoTestsAsError=true" in tests[0]
