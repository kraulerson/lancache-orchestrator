from __future__ import annotations

import subprocess

ALLOWED_LICENSES = {
    "MIT",
    "MIT License",
    "MIT OR Apache-2.0",
    "BSD License",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "Apache Software License",
    "Apache Software License; MIT License",
    "Apache-2.0",
    "Apache-2.0 OR BSD-2-Clause",
    "PSF License",
    "PSF-2.0",
    "ISC License (ISCL)",
    "Mozilla Public License 2.0 (MPL 2.0)",
    "Python Software Foundation License",
    "The Unlicense (Unlicense)",
}


def test_all_licenses_in_allowlist() -> None:
    result = subprocess.run(
        ["pip-licenses", "--format=csv"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    violations = []
    for line in result.stdout.strip().splitlines()[1:]:
        parts = line.strip('"').split('","')
        if len(parts) >= 3:
            name, version, license_name = parts[0], parts[1], parts[2]
            if license_name not in ALLOWED_LICENSES:
                violations.append(f"{name}=={version}: {license_name}")

    assert not violations, "Dependencies with disallowed licenses:\n" + "\n".join(violations)
