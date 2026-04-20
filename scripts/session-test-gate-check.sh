#!/usr/bin/env bash
# Solo Orchestrator — SessionStart hook for test gate enforcement and MCP requirements
# Checks Phase 2 test gate state, detects configured MCP servers, initializes
# session-start enforcement requirements.
# Only outputs when something needs attention.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── MCP Server Discovery ─────────────────────────────────────────
# Detect which MCP servers are configured and set up enforcement requirements.
# Known servers (set up by init.sh): context7, qdrant
# Unknown servers: anything else the user configured — flag for awareness.

QDRANT_CONFIGURED=false
CONTEXT7_CONFIGURED=false
UNKNOWN_SERVERS=""

if command -v jq &>/dev/null; then
  # Collect all configured MCP server names from all settings scopes
  ALL_MCP_SERVERS=""
  for settings_file in "$HOME/.claude/settings.json" "$HOME/.claude.json" ".claude/settings.json" ".claude/settings.local.json"; do
    if [ -f "$settings_file" ]; then
      SERVERS=$(jq -r '.mcpServers // {} | keys[]' "$settings_file" 2>/dev/null || true)
      if [ -n "$SERVERS" ]; then
        ALL_MCP_SERVERS="${ALL_MCP_SERVERS}${SERVERS}"$'\n'
      fi
    fi
  done

  # Deduplicate
  ALL_MCP_SERVERS=$(echo "$ALL_MCP_SERVERS" | sort -u | grep -v '^$' || true)

  # Classify each server
  while IFS= read -r server; do
    [ -z "$server" ] && continue
    case "$server" in
      context7|context7-mcp)
        CONTEXT7_CONFIGURED=true
        ;;
      qdrant|mcp-server-qdrant)
        QDRANT_CONFIGURED=true
        ;;
      *)
        # Unknown MCP server — user-configured
        UNKNOWN_SERVERS="${UNKNOWN_SERVERS}${server}, "
        ;;
    esac
  done <<< "$ALL_MCP_SERVERS"
fi

# ── Initialize Tool Usage Tracking ────────────────────────────────
TOOL_USAGE=".claude/tool-usage.json"
if command -v jq &>/dev/null; then
  SESSION_ID=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  mkdir -p .claude

  # Build additional_required array from unknown servers (empty for now —
  # the agent will ask the user about these, and the user can add them)
  cat > "$TOOL_USAGE" << TUEOF
{
  "session_id": "$SESSION_ID",
  "calls": [],
  "commits_since_last_context7": 0,
  "qdrant_find_called": false,
  "qdrant_store_called": false,
  "context7_called": false,
  "mcp_gate_satisfied": false,
  "mcp_requirements": {
    "qdrant_required": $QDRANT_CONFIGURED,
    "context7_required": $CONTEXT7_CONFIGURED,
    "additional_required": []
  }
}
TUEOF
fi

# ── Report Unknown MCP Servers ────────────────────────────────────
if [ -n "$UNKNOWN_SERVERS" ]; then
  # Strip trailing comma+space
  UNKNOWN_SERVERS="${UNKNOWN_SERVERS%, }"
  cat << EOF

MCP SERVER NOTICE: The following MCP server(s) are configured but not recognized by the Solo Orchestrator framework: $UNKNOWN_SERVERS

Ask the Orchestrator: "I see you have [$UNKNOWN_SERVERS] configured as MCP server(s). Would you like me to use them during this session? If so, what should I use them for?"

If the Orchestrator wants a server used at session start (like Qdrant), they can add it to .claude/tool-usage.json under mcp_requirements.additional_required.
EOF
fi

# ── Report MCP Gate Requirements ──────────────────────────────────
GATE_TOOLS=""
if [ "$QDRANT_CONFIGURED" = true ]; then
  GATE_TOOLS="${GATE_TOOLS}qdrant-find, "
fi
if [ "$CONTEXT7_CONFIGURED" = true ]; then
  GATE_TOOLS="${GATE_TOOLS}context7, "
fi
if [ -n "$GATE_TOOLS" ]; then
  GATE_TOOLS="${GATE_TOOLS%, }"
  echo ""
  echo "MCP GATE ACTIVE: Write/Edit operations are blocked until you call: $GATE_TOOLS"
  echo "Call these tools now before beginning any file modifications."
fi

PHASE_STATE=".claude/phase-state.json"
BUILD_PROGRESS=".claude/build-progress.json"

# Only relevant in Phase 2 (Construction)
if [ ! -f "$PHASE_STATE" ]; then
  exit 0
fi

if ! command -v jq &>/dev/null; then
  exit 0
fi

CURRENT_PHASE=$(jq -r '.current_phase // 0' "$PHASE_STATE" 2>/dev/null)
if [ "$CURRENT_PHASE" != "2" ]; then
  exit 0
fi

# Check build-progress.json exists
if [ ! -f "$BUILD_PROGRESS" ]; then
  echo "TEST GATE WARNING: In Phase 2 but .claude/build-progress.json is missing. Run: scripts/test-gate.sh --check-batch"
  exit 0
fi

# Read state
FEATURES_COMPLETED=$(jq -r '.features_completed | length' "$BUILD_PROGRESS" 2>/dev/null || echo "0")
SINCE_LAST=$(jq -r '.features_since_last_test' "$BUILD_PROGRESS" 2>/dev/null || echo "0")
INTERVAL=$(jq -r '.test_interval' "$BUILD_PROGRESS" 2>/dev/null || echo "2")
TESTING_REQUIRED=$(jq -r '.testing_required' "$BUILD_PROGRESS" 2>/dev/null || echo "false")

# Check 1: Testing session is overdue
if [ "$TESTING_REQUIRED" = "true" ] || [ "$SINCE_LAST" -ge "$INTERVAL" ]; then
  cat << EOF
URGENT — TEST GATE BLOCKED. Report this to the Orchestrator IMMEDIATELY as your FIRST response.

Testing session required: $SINCE_LAST features completed since last test (interval is every $INTERVAL).
Do NOT start the next feature. Run a UAT testing session first.
Steps: scripts/test-gate.sh --check-batch
EOF
  exit 0
fi

# Check 2: Phase 2 with no features recorded — likely missed --record-feature calls
# Look for evidence of work: merged PRs, source code commits, test files
if [ "$FEATURES_COMPLETED" -eq 0 ]; then
  # Count commits on main since Phase 1→2 gate date
  PHASE2_DATE=$(jq -r '.gates.phase_1_to_2 // empty' "$PHASE_STATE" 2>/dev/null)
  COMMIT_COUNT=0
  if [ -n "$PHASE2_DATE" ]; then
    COMMIT_COUNT=$(git log --oneline --since="$PHASE2_DATE" --no-merges 2>/dev/null | wc -l | tr -d ' ')
  fi

  if [ "$COMMIT_COUNT" -gt 5 ]; then
    cat << EOF
TEST GATE WARNING: Report this to the Orchestrator as your FIRST response.

Phase 2 has $COMMIT_COUNT commits since Phase 1→2 gate ($PHASE2_DATE) but build-progress.json shows 0 features recorded.
This likely means scripts/test-gate.sh --record-feature was not called after completing features.

After each feature completion, you MUST run:
  scripts/test-gate.sh --record-feature "feature-name"

Ask the Orchestrator how many features have been completed so you can record them now.
EOF
  fi
fi

# Context Health Check reminder
PROGRESS_FILE=".claude/build-progress.json"
if [ -f "$PROGRESS_FILE" ] && command -v jq &>/dev/null; then
  health_count=$(jq '.features_since_last_health_check // 0' "$PROGRESS_FILE" 2>/dev/null)
  if [ "$health_count" -ge 3 ] 2>/dev/null; then
    echo ""
    echo -e "\033[33m[REMINDER]\033[0m Context Health Check recommended — $health_count features since last check."
    echo "  Verify PROJECT_BIBLE.md still accurately reflects the codebase."
    echo "  After checking: scripts/test-gate.sh --reset-health-check"
  fi
fi
