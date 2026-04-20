#!/usr/bin/env bash
# Install project git hooks from scripts/git-hooks/ into .git/hooks/
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOKS_SRC="$REPO_ROOT/scripts/git-hooks"
HOOKS_DST="$REPO_ROOT/.git/hooks"

for hook in "$HOOKS_SRC"/*; do
    name="$(basename "$hook")"
    if [ -f "$HOOKS_DST/$name" ]; then
        echo "Replacing existing $name hook"
    fi
    cp "$hook" "$HOOKS_DST/$name"
    chmod +x "$HOOKS_DST/$name"
    echo "Installed: $name"
done
