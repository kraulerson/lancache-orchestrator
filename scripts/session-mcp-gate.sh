#!/usr/bin/env bash
set -euo pipefail

# Solo Orchestrator — PreToolUse hook for MCP session-start enforcement
# Blocks Write and Edit tool calls until required MCP tools have been called.
# Registered as a PreToolUse hook on Write and Edit tool calls.
#
# This closes the enforcement gap identified in the hook architecture:
# session-start MCP requirements (qdrant-find, context7) were advisory only.
# This hook makes them mechanical — the agent cannot produce output until
# it has loaded prior context and verified library documentation.
#
# Input: Claude Code passes tool input JSON on stdin
# Output:
#   - No output = allow
#   - JSON with permissionDecision: "deny" = block

TOOL_USAGE=".claude/tool-usage.json"

# No tracking file = no enforcement (project not initialized with hooks)
if [ ! -f "$TOOL_USAGE" ]; then
  exit 0
fi

command -v jq &>/dev/null || exit 0

# Fast path: if gate already satisfied, exit immediately.
# This check runs on EVERY Write/Edit call — must be fast.
GATE_SATISFIED=$(jq -r '.mcp_gate_satisfied // false' "$TOOL_USAGE" 2>/dev/null)
if [ "$GATE_SATISFIED" = "true" ]; then
  exit 0
fi

# Check each required MCP tool
MISSING=""

# Qdrant requirement: qdrant-find must be called at session start
QDRANT_REQUIRED=$(jq -r '.mcp_requirements.qdrant_required // false' "$TOOL_USAGE" 2>/dev/null)
if [ "$QDRANT_REQUIRED" = "true" ]; then
  QDRANT_CALLED=$(jq -r '.qdrant_find_called // false' "$TOOL_USAGE" 2>/dev/null)
  if [ "$QDRANT_CALLED" = "false" ]; then
    MISSING="${MISSING}qdrant-find (retrieve prior session context before starting work). "
  fi
fi

# Context7 requirement: at least one Context7 call must be made
CONTEXT7_REQUIRED=$(jq -r '.mcp_requirements.context7_required // false' "$TOOL_USAGE" 2>/dev/null)
if [ "$CONTEXT7_REQUIRED" = "true" ]; then
  CONTEXT7_CALLED=$(jq -r '.context7_called // false' "$TOOL_USAGE" 2>/dev/null)
  if [ "$CONTEXT7_CALLED" = "false" ]; then
    MISSING="${MISSING}context7 resolve-library-id or query-docs (verify library documentation is current before writing). "
  fi
fi

# Check any additional required MCP tools (user-configured)
ADDITIONAL=$(jq -r '.mcp_requirements.additional_required // [] | .[]' "$TOOL_USAGE" 2>/dev/null)
if [ -n "$ADDITIONAL" ]; then
  while IFS= read -r tool_pattern; do
    [ -z "$tool_pattern" ] && continue
    TOOL_CALLED=$(jq --arg pat "$tool_pattern" '[.calls[] | select(.tool | test($pat))] | length > 0' "$TOOL_USAGE" 2>/dev/null)
    if [ "$TOOL_CALLED" != "true" ]; then
      MISSING="${MISSING}${tool_pattern} (required MCP tool not yet called this session). "
    fi
  done <<< "$ADDITIONAL"
fi

if [ -n "$MISSING" ]; then
  # Strip trailing space
  MISSING="${MISSING% }"
  ESCAPED=$(echo "$MISSING" | sed 's/"/\\"/g')
  cat << HOOKEOF
{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "SESSION START REQUIREMENTS NOT MET. Before making any file changes, you must call the following MCP tools: $ESCAPED These tools are required at the start of every session to ensure you are working with current context and accurate documentation. Call them now, then retry your Write/Edit operation."}}
HOOKEOF
  exit 0
fi

# All requirements met — set gate satisfied for fast-path on future calls
jq '.mcp_gate_satisfied = true' "$TOOL_USAGE" > "$TOOL_USAGE.tmp" 2>/dev/null && mv "$TOOL_USAGE.tmp" "$TOOL_USAGE" 2>/dev/null

exit 0
