"""Agent-side constants.

Kept separate from ``api/_constants.py`` because the two services have genuinely
different limits — see the cap below.
"""

from __future__ import annotations

# Maximum request body the agent will accept (#298).
#
# The API caps at 32 KiB. The agent CANNOT: /v1/epic/validate carries the game's
# entire manifest base64-encoded, and in this library the largest is game 15035 at
# 66 MB raw — about 88 MB on the wire — with two more at 40 MB and 21 MB against a
# 1.1 MB average across 922 manifests. A 32 KiB cap here would reject every Epic
# validation, starting with the biggest games.
#
# So this sits above the legitimate maximum and below the abuse case: a 238 MB POST
# to /v1/stat returned 200 after 88 seconds and moved the agent's RSS from 63 MB to
# 698 MB, where it stayed — on the CPU-constrained NAS beside lancache.
#
# 128 MiB gives ~45% headroom over today's largest manifest while refusing that
# request. It is deliberately a sanity limit rather than a tight one; a service that
# must accept 88 MB cannot have a tight one. Making it genuinely small would mean not
# sending the manifest in the body at all, which is a larger redesign.
AGENT_BODY_SIZE_CAP_BYTES: int = 128 * 1024 * 1024
