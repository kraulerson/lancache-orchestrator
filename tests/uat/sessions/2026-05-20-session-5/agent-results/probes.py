"""UAT-5 Agent 2 (Exploratory / Malicious User) probes.

Each test fires a deliberately-bad request and captures status + body.
Most tests are expected to PASS (server hardens correctly). Tests that
FAIL the assertion below are CANDIDATE BUGS — re-examined by hand.

Run from project root with:
  PYTHONPATH=src .venv/bin/pytest \
    tests/uat/sessions/2026-05-20-session-5/agent-results/probes.py -v -s
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make sibling tests.* package discoverable so we can reuse fixtures.
sys.path.insert(0, str(Path(__file__).resolve().parents[5]))

# Re-import the conftest fixtures so pytest discovers them.
from tests.api.conftest import (  # noqa: F401,E402
    client,
    external_client,
    games_pool_100,
    jobs_pool_seeded,
    lifespan_app,
    loopback_client,
    manifests_pool_seeded,
    unit_app,
)
from tests.db.conftest import (  # noqa: F401,E402
    _isolated_env,
    db_path,
    mem_pool,
    pool,
    populated_pool,
)

VALID_TOKEN = "a" * 32
AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}


FINDINGS: list[dict] = []


def _record(test_id: str, observed: dict, expected: str, severity: str | None = None) -> None:
    """Print a structured finding line so we can grep results out of pytest -s."""
    line = {
        "test_id": test_id,
        "severity_guess": severity,
        "expected": expected,
        "observed": observed,
    }
    print("UAT5_FINDING:" + json.dumps(line, default=str), flush=True)


# ===========================================================================
# 1. AUTH BYPASS ATTEMPTS
# ===========================================================================


class TestAuthBypass:
    async def test_a1_empty_bearer(self, client, manifests_pool_seeded):
        r = await client.get("/api/v1/manifests", headers={"Authorization": ""})
        _record(
            "A1_empty_authorization_header", {"status": r.status_code, "body": r.text[:200]}, "401"
        )
        assert r.status_code == 401, "empty Authorization must be rejected"

    async def test_a2_bearer_no_token(self, client, manifests_pool_seeded):
        r = await client.get("/api/v1/manifests", headers={"Authorization": "Bearer"})
        _record("A2_bearer_no_token", {"status": r.status_code, "body": r.text[:200]}, "401")
        assert r.status_code == 401

    async def test_a3_bearer_space_only(self, client, manifests_pool_seeded):
        r = await client.get("/api/v1/manifests", headers={"Authorization": "Bearer "})
        _record("A3_bearer_space", {"status": r.status_code, "body": r.text[:200]}, "401")
        assert r.status_code == 401

    async def test_a4_bearer_wrong_token(self, client, manifests_pool_seeded):
        r = await client.get("/api/v1/manifests", headers={"Authorization": "Bearer x"})
        _record("A4_bearer_x", {"status": r.status_code}, "401")
        assert r.status_code == 401

    async def test_a5_bearer_lowercase(self, client, manifests_pool_seeded):
        # Per RFC 7235 §2.1 scheme is case-insensitive; UAT-3 S3-m
        r = await client.get(
            "/api/v1/manifests", headers={"Authorization": f"bearer {VALID_TOKEN}"}
        )
        _record("A5_lowercase_bearer", {"status": r.status_code}, "200 (RFC 7235)")
        assert r.status_code == 200

    async def test_a6_no_authorization_at_all(self, client, manifests_pool_seeded):
        # Pass NO Authorization header. unit_app's default httpx client may add
        # one — explicitly create a fresh request without any auth.
        r = await client.get("/api/v1/manifests")
        _record("A6_no_auth_header", {"status": r.status_code}, "401")
        assert r.status_code == 401

    async def test_a7_authorization_with_basic_scheme(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests",
            headers={"Authorization": f"Basic {VALID_TOKEN}"},
        )
        _record("A7_basic_scheme", {"status": r.status_code}, "401")
        assert r.status_code == 401

    async def test_a8_extra_whitespace_in_token(self, client, manifests_pool_seeded):
        # "Bearer   <token>   " — multiple internal+trailing spaces.
        # auth_header.find(" ") returns idx of FIRST space; token = rest.strip()
        # so "  TOKEN  " becomes "TOKEN" after .strip(). Expected: 200.
        r = await client.get(
            "/api/v1/manifests",
            headers={"Authorization": f"Bearer    {VALID_TOKEN}   "},
        )
        _record("A8_extra_whitespace", {"status": r.status_code}, "200 (token stripped)")
        # Document behavior — either 200 or 401 is defensible.

    async def test_a9_two_authorization_headers(self, client, manifests_pool_seeded):
        # httpx joins duplicate headers with comma — RFC compliant.
        # The middleware uses dict(scope["headers"]) which DROPS DUPLICATES,
        # keeping the LAST occurrence. Worth observing.
        import httpx

        req = httpx.Request(
            "GET",
            "http://testserver/api/v1/manifests",
            headers=[
                ("Authorization", "Bearer wrong"),
                ("Authorization", f"Bearer {VALID_TOKEN}"),
            ],
        )
        r = await client.send(req)
        _record(
            "A9_two_auth_headers",
            {"status": r.status_code, "body": r.text[:120]},
            "401 (safe) or 200 (last-wins)",
        )

    async def test_a10_token_with_null_byte(self, client, manifests_pool_seeded):
        # token with embedded null byte
        try:
            r = await client.get(
                "/api/v1/manifests",
                headers={"Authorization": f"Bearer {VALID_TOKEN}\x00extra"},
            )
            _record(
                "A10_null_byte_token", {"status": r.status_code}, "401 or rejection at header level"
            )
        except Exception as e:
            _record("A10_null_byte_token", {"raised": str(e)}, "client-side reject (httpx)")


# ===========================================================================
# 2. TYPE CONFUSION (query params)
# ===========================================================================


class TestTypeConfusion:
    async def test_t1_long_filter_value(self, client, manifests_pool_seeded):
        # 10k-char value on a `version` (string) filter
        v = "x" * 10000
        r = await client.get(f"/api/v1/manifests?version={v}", headers=AUTH)
        _record(
            "T1_long_version", {"status": r.status_code, "len": len(v)}, "200 (no match) or 400"
        )
        assert r.status_code in (200, 400)

    async def test_t2_url_encoded_null_byte_filter(self, client, manifests_pool_seeded):
        # %00 in a string filter
        r = await client.get("/api/v1/manifests?version=%00bad", headers=AUTH)
        _record(
            "T2_nullbyte_filter",
            {"status": r.status_code, "body": r.text[:200]},
            "200 with no match",
        )

    async def test_t3_array_style_param(self, client, manifests_pool_seeded):
        # ?game_id[]=1 — PHP-style array syntax
        r = await client.get("/api/v1/manifests?game_id[]=1", headers=AUTH)
        _record(
            "T3_phpish_array", {"status": r.status_code, "body": r.text[:200]}, "400 unknown field"
        )
        assert r.status_code == 400

    async def test_t4_duplicate_query_keys(self, client, manifests_pool_seeded):
        # ?game_id=1&game_id=2 — Starlette QueryParams returns last for .get()
        # but iterating yields each occurrence. parse_filters uses both.
        # Bug if it silently picks one.
        r = await client.get("/api/v1/manifests?game_id=1&game_id=2", headers=AUTH)
        _record(
            "T4_duplicate_keys",
            {"status": r.status_code, "body": r.text[:400]},
            "either consistent OR 400",
        )
        # Document only — behavior worth confirming.

    async def test_t5_unicode_filter(self, client, manifests_pool_seeded):
        # Unicode passed to a string field — should pass as a non-match (200)
        r = await client.get("/api/v1/manifests?version=%E2%98%A0%EF%B8%8F", headers=AUTH)
        _record("T5_unicode_filter", {"status": r.status_code}, "200 with no match")
        assert r.status_code == 200

    async def test_t6_int_field_with_string_value(self, client, manifests_pool_seeded):
        r = await client.get("/api/v1/manifests?game_id=banana", headers=AUTH)
        _record("T6_int_with_string", {"status": r.status_code, "body": r.text[:200]}, "400")
        assert r.status_code == 400


# ===========================================================================
# 3. RESOURCE EXHAUSTION
# ===========================================================================


class TestResourceExhaustion:
    async def test_r1_max_int64_offset(self, client, manifests_pool_seeded):
        # offset just under INT64_MAX
        big = 9223372036854775806
        r = await client.get(f"/api/v1/manifests?offset={big}", headers=AUTH)
        _record("R1_huge_offset", {"status": r.status_code, "body": r.text[:200]}, "200 empty rows")
        assert r.status_code == 200, "very large in-range offset should still succeed"

    async def test_r2_over_int64_offset(self, client, manifests_pool_seeded):
        # parse_pagination raises if offset > INT64_MAX
        too_big = 2**63 + 5
        r = await client.get(f"/api/v1/manifests?offset={too_big}", headers=AUTH)
        _record("R2_over_int64_offset", {"status": r.status_code, "body": r.text[:200]}, "400")
        assert r.status_code == 400

    async def test_r3_negative_int64_offset(self, client, manifests_pool_seeded):
        r = await client.get("/api/v1/manifests?offset=-1", headers=AUTH)
        _record("R3_neg_offset", {"status": r.status_code, "body": r.text[:200]}, "400")
        assert r.status_code == 400

    async def test_r4_many_filter_params(self, client, manifests_pool_seeded):
        # 100 unknown filter keys — first one should trigger 400 immediately
        # but if parser iterates all, we should still see 400 quickly.
        params = "&".join(f"unknown_{i}=1" for i in range(100))
        r = await client.get(f"/api/v1/manifests?{params}", headers=AUTH)
        _record("R4_100_unknown_fields", {"status": r.status_code, "body": r.text[:200]}, "400")
        assert r.status_code == 400

    async def test_r5_max_limit(self, client, manifests_pool_seeded):
        r = await client.get("/api/v1/manifests?limit=500", headers=AUTH)
        _record("R5_max_limit", {"status": r.status_code}, "200")
        assert r.status_code == 200

    async def test_r6_in_at_cap(self, client, manifests_pool_seeded):
        # MAX_IN_VALUES = 100; cap should be inclusive
        vals = ",".join(str(i) for i in range(1, 101))  # 100 vals
        r = await client.get(f"/api/v1/manifests?game_id_in={vals}", headers=AUTH)
        _record(
            "R6_in_at_cap", {"status": r.status_code, "body": r.text[:200]}, "200 (cap inclusive)"
        )
        assert r.status_code == 200

    async def test_r7_in_over_cap(self, client, manifests_pool_seeded):
        vals = ",".join(str(i) for i in range(1, 102))  # 101 vals
        r = await client.get(f"/api/v1/manifests?game_id_in={vals}", headers=AUTH)
        _record("R7_in_over_cap", {"status": r.status_code, "body": r.text[:200]}, "400")
        assert r.status_code == 400

    async def test_r8_huge_in_value_count_with_repeat(self, client, manifests_pool_seeded):
        # 1000 vals — way over cap
        vals = ",".join(str(i) for i in range(1, 1001))
        r = await client.get(f"/api/v1/manifests?game_id_in={vals}", headers=AUTH)
        _record("R8_huge_in", {"status": r.status_code}, "400")
        assert r.status_code == 400

    async def test_r9_long_sort_field_list(self, client, manifests_pool_seeded):
        # 100 entries in sort, all valid duplicate field names
        s = ",".join(["id:asc"] * 100)
        r = await client.get(f"/api/v1/manifests?sort={s}", headers=AUTH)
        _record(
            "R9_long_sort",
            {"status": r.status_code, "body": r.text[:300]},
            "200 (no dedup of repeats)",
        )
        # Document: 100 redundant ORDER BY clauses is wasteful — confirm not 500.


# ===========================================================================
# 4. ENCODING & OPERATOR TRICKS
# ===========================================================================


class TestEncodingTricks:
    async def test_e1_mixed_case_operator(self, client, manifests_pool_seeded):
        # gTe (mixed case) — parser only knows lowercase
        r = await client.get("/api/v1/manifests?chunk_count_gTe=100", headers=AUTH)
        _record(
            "E1_mixed_case_op", {"status": r.status_code, "body": r.text[:200]}, "400 unknown field"
        )
        assert r.status_code == 400

    async def test_e2_trailing_underscore_field(self, client, manifests_pool_seeded):
        # game_id_ (no operator) — parser may strip the trailing underscore
        r = await client.get("/api/v1/manifests?game_id_=1", headers=AUTH)
        _record("E2_trailing_underscore", {"status": r.status_code, "body": r.text[:200]}, "400")
        # parse_filters: key="game_id_"; checks suffixes (gte,lte,gt,lt,ne,in,eq).
        # "_eq" suffix found → field_name="game_id", op="eq"; but the actual key
        # was "game_id_eq" no — it's "game_id_". Suffix "_eq" doesn't match.
        # Falls through to field_name="game_id_" which is not in allow_list.
        # Expect 400.

    async def test_e3_whitespace_in_sort_direction(self, client, manifests_pool_seeded):
        r = await client.get("/api/v1/manifests?sort=id:%20desc", headers=AUTH)
        _record(
            "E3_ws_in_direction",
            {"status": r.status_code, "body": r.text[:200]},
            "200 (parser strips)",
        )
        # parse_sort calls .strip().lower() on direction → "desc"

    async def test_e4_whitespace_in_field_name(self, client, manifests_pool_seeded):
        r = await client.get("/api/v1/manifests?sort=%20id:asc", headers=AUTH)
        _record(
            "E4_ws_in_field", {"status": r.status_code, "body": r.text[:200]}, "200 (parser strips)"
        )
        # parse_sort calls field_name.strip()

    async def test_e5_negative_zero_int(self, client, manifests_pool_seeded):
        r = await client.get("/api/v1/manifests?game_id=-0", headers=AUTH)
        _record(
            "E5_neg_zero", {"status": r.status_code, "body": r.text[:200]}, "200 (int('-0')==0)"
        )
        assert r.status_code == 200

    async def test_e6_int_with_plus_sign(self, client, manifests_pool_seeded):
        r = await client.get("/api/v1/manifests?game_id=%2B1", headers=AUTH)  # "+1"
        _record("E6_int_with_plus", {"status": r.status_code}, "200 (int('+1')==1)")
        assert r.status_code == 200

    async def test_e7_int_with_underscores(self, client, manifests_pool_seeded):
        # Python int() accepts "1_000" — this is a PARSER quirk that bypasses
        # the apparent "digits only" expectation.
        r = await client.get("/api/v1/manifests?game_id=1_000", headers=AUTH)
        _record(
            "E7_int_with_underscores", {"status": r.status_code, "body": r.text[:200]}, "either"
        )

    async def test_e8_float_in_int_field(self, client, manifests_pool_seeded):
        r = await client.get("/api/v1/manifests?game_id=1.5", headers=AUTH)
        _record("E8_float_in_int", {"status": r.status_code, "body": r.text[:200]}, "400")
        assert r.status_code == 400


# ===========================================================================
# 5. OPERATOR MISUSE
# ===========================================================================


class TestOperatorMisuse:
    async def test_o1_ne_on_field_not_allowing_ne(self, client, manifests_pool_seeded):
        # manifests game_id allows {eq, in} only — _ne not in spec
        r = await client.get("/api/v1/manifests?game_id_ne=1", headers=AUTH)
        _record("O1_ne_not_allowed", {"status": r.status_code, "body": r.text[:200]}, "400")
        assert r.status_code == 400

    async def test_o2_gte_lte_empty_range(self, client, manifests_pool_seeded):
        # gte=100 and lte=1 — server should run query, get 0 rows
        r = await client.get(
            "/api/v1/manifests?chunk_count_gte=100&chunk_count_lte=1",
            headers=AUTH,
        )
        _record("O2_empty_range", {"status": r.status_code, "body": r.text[:300]}, "200 empty")
        assert r.status_code == 200
        body = r.json()
        assert body["manifests"] == []
        assert body["meta"]["total"] == 0
        assert body["meta"]["has_more"] is False

    async def test_o3_in_on_field_not_allowing_in(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?chunk_count_in=1,2,3",
            headers=AUTH,
        )
        _record("O3_in_not_allowed", {"status": r.status_code, "body": r.text[:200]}, "400")
        assert r.status_code == 400

    async def test_o4_both_eq_and_in(self, client, manifests_pool_seeded):
        # Same field gets both eq and in — parse_filters builds {game_id:{eq:1,in:[2,3]}}
        # build_where_clause emits "game_id = ? AND game_id IN (?,?)" — always empty.
        r = await client.get(
            "/api/v1/manifests?game_id=1&game_id_in=2,3",
            headers=AUTH,
        )
        _record(
            "O4_eq_and_in_same_field",
            {"status": r.status_code, "body": r.text[:400]},
            "200 (consistent AND); or 400 if endpoint rejects",
        )

    async def test_o5_in_with_empty_value(self, client, manifests_pool_seeded):
        # ?game_id_in= — split(",") yields [""] which is len 1 (under cap)
        # then _coerce_value("", int, ...) raises → 400
        r = await client.get("/api/v1/manifests?game_id_in=", headers=AUTH)
        _record("O5_empty_in", {"status": r.status_code, "body": r.text[:200]}, "400")
        assert r.status_code == 400

    async def test_o6_in_with_only_commas(self, client, manifests_pool_seeded):
        # ?game_id_in=,,, — split(",") yields ["","","",""] (4 empty strings)
        r = await client.get("/api/v1/manifests?game_id_in=,,,", headers=AUTH)
        _record("O6_only_commas_in", {"status": r.status_code, "body": r.text[:200]}, "400")
        assert r.status_code == 400


# ===========================================================================
# 6. SORT EDGE CASES
# ===========================================================================


class TestSortEdgeCases:
    async def test_s1_empty_sort(self, client, manifests_pool_seeded):
        # ?sort= — empty raw → default applies (UAT-4 S2-B)
        r = await client.get("/api/v1/manifests?sort=", headers=AUTH)
        _record("S1_empty_sort", {"status": r.status_code}, "200 default sort")
        assert r.status_code == 200
        assert r.json()["meta"]["applied_sort"][0]["field"] == "fetched_at"

    async def test_s2_sort_colon_only(self, client, manifests_pool_seeded):
        # ?sort=: — entry="":; split → field_name="", direction=""; field_name not in allow → 400
        r = await client.get("/api/v1/manifests?sort=:", headers=AUTH)
        _record("S2_colon_only", {"status": r.status_code, "body": r.text[:200]}, "400")
        assert r.status_code == 400

    async def test_s3_sort_colon_asc(self, client, manifests_pool_seeded):
        # ?sort=:asc — field="" → 400
        r = await client.get("/api/v1/manifests?sort=:asc", headers=AUTH)
        _record("S3_colon_asc", {"status": r.status_code, "body": r.text[:200]}, "400")
        assert r.status_code == 400

    async def test_s4_uppercase_direction(self, client, manifests_pool_seeded):
        # parse_sort lowercases direction → ASC → "asc" → ok
        r = await client.get("/api/v1/manifests?sort=id:ASC", headers=AUTH)
        _record("S4_uppercase_dir", {"status": r.status_code}, "200")
        assert r.status_code == 200

    async def test_s5_whitespace_only_sort(self, client, manifests_pool_seeded):
        # "  ,  ,  ," → all stripped to "" → default applies (UAT-4 S2-B)
        r = await client.get("/api/v1/manifests?sort=%20,%20,%20,%20", headers=AUTH)
        _record("S5_ws_only_sort", {"status": r.status_code, "body": r.text[:300]}, "200 default")
        assert r.status_code == 200
        assert r.json()["meta"]["applied_sort"][0]["field"] == "fetched_at"

    async def test_s6_duplicate_field_diff_directions(self, client, manifests_pool_seeded):
        # ?sort=id:asc,id:desc — same field, different direction.
        # parse_sort doesn't dedupe; emits ORDER BY id ASC, id DESC.
        # SQLite executes — result rows ordered by first occurrence.
        # tie_breaker (id:asc) — sees id already in user_sort → skipped.
        r = await client.get("/api/v1/manifests?sort=id:asc,id:desc", headers=AUTH)
        _record(
            "S6_dup_field_diff_dirs",
            {
                "status": r.status_code,
                "applied_sort": r.json().get("meta", {}).get("applied_sort")
                if r.status_code == 200
                else None,
            },
            "200 (both emitted)",
        )
        # Confirmed: server emits both. Worth flagging as possible smell.

    async def test_s7_invalid_direction(self, client, manifests_pool_seeded):
        r = await client.get("/api/v1/manifests?sort=id:sideways", headers=AUTH)
        _record("S7_invalid_dir", {"status": r.status_code, "body": r.text[:200]}, "400")
        assert r.status_code == 400

    async def test_s8_sort_with_double_colon(self, client, manifests_pool_seeded):
        # ?sort=id::asc — split(":",1) yields ["id", ":asc"]; direction=":asc" → invalid → 400
        r = await client.get("/api/v1/manifests?sort=id::asc", headers=AUTH)
        _record("S8_double_colon", {"status": r.status_code, "body": r.text[:200]}, "400")
        assert r.status_code == 400


# ===========================================================================
# 7. INCLUDE EDGE CASES (BL9)
# ===========================================================================


class TestIncludeEdgeCases:
    async def test_i1_leading_comma(self, client, manifests_pool_seeded):
        # ?include=,game — split(",")=["","game"]; empty stripped; "game" valid
        r = await client.get("/api/v1/manifests?include=,game", headers=AUTH)
        _record(
            "I1_leading_comma",
            {"status": r.status_code, "applied": r.json().get("meta", {}).get("applied_includes")},
            "200 ['game']",
        )
        assert r.status_code == 200
        assert r.json()["meta"]["applied_includes"] == ["game"]

    async def test_i2_trailing_comma(self, client, manifests_pool_seeded):
        r = await client.get("/api/v1/manifests?include=game,", headers=AUTH)
        _record(
            "I2_trailing_comma",
            {"status": r.status_code, "applied": r.json().get("meta", {}).get("applied_includes")},
            "200",
        )
        assert r.status_code == 200

    async def test_i3_include_empty(self, client, manifests_pool_seeded):
        r = await client.get("/api/v1/manifests?include=", headers=AUTH)
        _record(
            "I3_empty_include",
            {"status": r.status_code, "applied": r.json().get("meta", {}).get("applied_includes")},
            "200 []",
        )
        assert r.status_code == 200
        assert r.json()["meta"]["applied_includes"] == []

    async def test_i4_include_case_sensitive(self, client, manifests_pool_seeded):
        # Game (uppercase G) — not in allow_list (lowercase only)
        r = await client.get("/api/v1/manifests?include=Game", headers=AUTH)
        _record("I4_uppercase_include", {"status": r.status_code, "body": r.text[:200]}, "400")
        assert r.status_code == 400

    async def test_i5_include_mixed_case(self, client, manifests_pool_seeded):
        r = await client.get("/api/v1/manifests?include=game,Game", headers=AUTH)
        _record(
            "I5_mixed_case", {"status": r.status_code, "body": r.text[:200]}, "400 Game unknown"
        )
        assert r.status_code == 400

    async def test_i6_include_url_encoded_space(self, client, manifests_pool_seeded):
        # ?include=%20game%20 — whitespace stripped per impl
        r = await client.get("/api/v1/manifests?include=%20game%20", headers=AUTH)
        _record(
            "I6_ws_padded_include",
            {"status": r.status_code, "applied": r.json().get("meta", {}).get("applied_includes")},
            "200 ['game']",
        )
        assert r.status_code == 200

    async def test_i7_include_duplicate(self, client, manifests_pool_seeded):
        r = await client.get("/api/v1/manifests?include=game,game,game", headers=AUTH)
        _record(
            "I7_dup_include",
            {"status": r.status_code, "applied": r.json().get("meta", {}).get("applied_includes")},
            "200 ['game']",
        )
        assert r.status_code == 200
        assert r.json()["meta"]["applied_includes"] == ["game"]

    async def test_i8_include_on_endpoint_without_include(self, client, games_pool_100):
        # /api/v1/games has no IncludeAllowList. ?include= becomes an unknown FILTER
        # field since "include" is in _RESERVED_PARAM_NAMES so parse_filters skips it.
        # Result: include silently ignored on games — that's a UX issue, not a security one.
        r = await client.get("/api/v1/games?include=foo", headers=AUTH)
        _record(
            "I8_include_on_games_silently_ignored",
            {"status": r.status_code, "body": r.text[:300]},
            "200 silently ignored — possible UX bug",
        )

    async def test_i9_include_on_jobs(self, client, jobs_pool_seeded):
        r = await client.get("/api/v1/jobs?include=foo", headers=AUTH)
        _record(
            "I9_include_on_jobs_silently_ignored",
            {"status": r.status_code, "body": r.text[:200]},
            "200 silently ignored",
        )


# ===========================================================================
# 8. PAGINATION MATH
# ===========================================================================


class TestPaginationMath:
    async def test_p1_offset_over_total(self, client, manifests_pool_seeded):
        # populated_pool seeds 3 baseline + manifests_pool_seeded adds 21 = 24 total
        r = await client.get("/api/v1/manifests?offset=100", headers=AUTH)
        _record(
            "P1_offset_over_total",
            {"status": r.status_code, "meta": r.json()["meta"], "rows": len(r.json()["manifests"])},
            "200 empty",
        )
        body = r.json()
        assert body["manifests"] == []
        assert body["meta"]["total"] == 24
        assert body["meta"]["has_more"] is False

    async def test_p2_offset_equals_total(self, client, manifests_pool_seeded):
        r = await client.get("/api/v1/manifests?offset=24", headers=AUTH)
        body = r.json()
        _record(
            "P2_offset_eq_total",
            {"meta": body["meta"], "rows": len(body["manifests"])},
            "200 empty",
        )
        assert body["manifests"] == []
        assert body["meta"]["has_more"] is False

    async def test_p3_offset_total_minus_one(self, client, manifests_pool_seeded):
        r = await client.get("/api/v1/manifests?offset=23&limit=10", headers=AUTH)
        body = r.json()
        _record(
            "P3_offset_total_minus_1",
            {"meta": body["meta"], "rows": len(body["manifests"])},
            "200 with 1 row",
        )
        assert len(body["manifests"]) == 1
        assert body["meta"]["has_more"] is False

    async def test_p4_limit_1_has_more_correct(self, client, manifests_pool_seeded):
        r = await client.get("/api/v1/manifests?limit=1&offset=0", headers=AUTH)
        body = r.json()
        _record("P4_limit_1", {"meta": body["meta"]}, "has_more=True (23 more rows)")
        assert body["meta"]["has_more"] is True

    async def test_p5_limit_1_last_page(self, client, manifests_pool_seeded):
        r = await client.get("/api/v1/manifests?limit=1&offset=23", headers=AUTH)
        body = r.json()
        _record("P5_limit_1_last", {"meta": body["meta"]}, "has_more=False")
        assert body["meta"]["has_more"] is False

    async def test_p6_iterate_full_set(self, client, manifests_pool_seeded):
        # walk every row at limit=5 ensure no overlap / no skip
        ids: list[int] = []
        offset = 0
        while True:
            r = await client.get(f"/api/v1/manifests?limit=5&offset={offset}", headers=AUTH)
            body = r.json()
            ids.extend(m["id"] for m in body["manifests"])
            if not body["meta"]["has_more"]:
                break
            offset += 5
            if offset > 100:
                break
        _record(
            "P6_full_iterate",
            {"distinct": len(set(ids)), "got": len(ids)},
            "24 unique IDs, no dupes",
        )
        assert len(ids) == 24
        assert len(set(ids)) == 24

    async def test_p7_limit_zero(self, client, manifests_pool_seeded):
        r = await client.get("/api/v1/manifests?limit=0", headers=AUTH)
        _record("P7_limit_zero", {"status": r.status_code, "body": r.text[:200]}, "400")
        assert r.status_code == 400


# ===========================================================================
# 9. HEADER ATTACKS
# ===========================================================================


class TestHeaderAttacks:
    async def test_h1_xff_spoof_loopback(self, external_client):
        # external_client simulates 192.168.1.100. X-Forwarded-For: 127.0.0.1
        # OQ2 reads scope[client] directly — XFF spoof MUST fail.
        r = await external_client.get(
            "/api/v1/openapi.json",
            headers={"X-Forwarded-For": "127.0.0.1"},
        )
        _record(
            "H1_xff_loopback_spoof",
            {"status": r.status_code, "body": r.text[:200]},
            "403 loopback-only",
        )
        assert r.status_code == 403

    async def test_h2_x_real_ip_spoof(self, external_client):
        r = await external_client.get(
            "/api/v1/openapi.json",
            headers={"X-Real-IP": "127.0.0.1"},
        )
        _record("H2_x_real_ip_spoof", {"status": r.status_code}, "403")
        assert r.status_code == 403

    async def test_h3_oversized_auth_header(self, client, manifests_pool_seeded):
        # 100kb fake token in Authorization header — must NOT crash server
        # Note: BodySizeCapMiddleware only inspects BODY; Authorization header
        # is unbounded by middleware. ASGI server may impose its own limit.
        long_tok = "x" * 100000
        try:
            r = await client.get(
                "/api/v1/manifests",
                headers={"Authorization": f"Bearer {long_tok}"},
            )
            _record(
                "H3_oversized_auth",
                {"status": r.status_code, "body": r.text[:200]},
                "401 (compared via hmac, constant-time)",
            )
            assert r.status_code == 401
        except Exception as e:
            _record("H3_oversized_auth", {"raised": str(e)[:200]}, "rejected at transport")

    async def test_h4_origin_unexpected(self, client, manifests_pool_seeded):
        # CORS Origin header — endpoint should still respond normally;
        # CORS middleware only sets ACAO; doesn't reject non-OPTIONS.
        r = await client.get(
            "/api/v1/manifests?limit=1",
            headers={**AUTH, "Origin": "http://evil.example.com"},
        )
        _record(
            "H4_unexpected_origin",
            {"status": r.status_code, "ACAO": r.headers.get("access-control-allow-origin")},
            "200 (CORS sets origin or empty)",
        )
        assert r.status_code == 200


# ===========================================================================
# 10. CORRELATION ID
# ===========================================================================


class TestCorrelationId:
    async def test_c1_huge_correlation_id(self, client, manifests_pool_seeded):
        # 1000-char correlation ID — middleware regenerates if not UUID4
        big = "z" * 1000
        r = await client.get(
            "/api/v1/manifests?limit=1",
            headers={**AUTH, "X-Correlation-ID": big},
        )
        echoed = r.headers.get("x-correlation-id", "")
        _record(
            "C1_huge_corr_id",
            {"status": r.status_code, "echoed_len": len(echoed), "echoed_prefix": echoed[:60]},
            "200, echoed is fresh UUID4 (server regenerates)",
        )
        assert r.status_code == 200
        # Must NOT echo the client's 1000-char value
        assert echoed != big
        # Must be a UUID4 length
        assert len(echoed) == 36

    async def test_c2_corr_id_with_crlf(self, client, manifests_pool_seeded):
        # \r\n in correlation ID — header injection attempt.
        # httpx itself disallows control chars in header values.
        try:
            r = await client.get(
                "/api/v1/manifests?limit=1",
                headers={**AUTH, "X-Correlation-ID": "abc\r\nX-Injected: yes"},
            )
            _record(
                "C2_crlf_corr_id",
                {"status": r.status_code, "headers": dict(r.headers)},
                "client may reject or server regenerates",
            )
        except Exception as e:
            _record("C2_crlf_corr_id", {"raised": str(e)[:200]}, "client-side rejection (httpx)")


# ===========================================================================
# 11. RESPONSE SHAPE / EDGE DATA
# ===========================================================================


class TestResponseShape:
    async def test_d1_include_with_zero_rows(self, client, manifests_pool_seeded):
        # ?include=game with filter that matches nothing — games_by_id stays empty
        r = await client.get(
            "/api/v1/manifests?game_id=99999&include=game",
            headers=AUTH,
        )
        body = r.json()
        _record(
            "D1_include_zero_rows", {"meta": body["meta"]}, "200 empty, applied_includes=['game']"
        )
        assert body["manifests"] == []
        assert body["meta"]["applied_includes"] == ["game"]

    async def test_d2_applied_filters_int_serialization(self, client, manifests_pool_seeded):
        # Verify applied_filters echoes int values correctly (not strings)
        r = await client.get(
            "/api/v1/manifests?chunk_count_gte=1000",
            headers=AUTH,
        )
        body = r.json()
        _record(
            "D2_applied_filters_int",
            {"applied_filters": body["meta"]["applied_filters"]},
            "{'chunk_count': {'gte': 1000}} as int",
        )
        assert body["meta"]["applied_filters"]["chunk_count"]["gte"] == 1000
        assert isinstance(body["meta"]["applied_filters"]["chunk_count"]["gte"], int)

    async def test_d3_applied_filters_timestamp_string(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?fetched_at_gte=2026-05-01",
            headers=AUTH,
        )
        body = r.json()
        _record(
            "D3_applied_filters_ts",
            {"applied_filters": body["meta"]["applied_filters"]},
            "string value echoed",
        )
        assert body["meta"]["applied_filters"]["fetched_at"]["gte"] == "2026-05-01"

    async def test_d4_invalid_timestamp_format(self, client, manifests_pool_seeded):
        r = await client.get(
            "/api/v1/manifests?fetched_at_gte=2026/05/01",
            headers=AUTH,
        )
        _record("D4_bad_ts_format", {"status": r.status_code, "body": r.text[:200]}, "400")
        assert r.status_code == 400

    async def test_d5_impossible_timestamp_month(self, client, manifests_pool_seeded):
        # YYYY-13-01 — month 13 — must fail strict parse
        r = await client.get(
            "/api/v1/manifests?fetched_at_gte=2026-13-01",
            headers=AUTH,
        )
        _record("D5_bad_month", {"status": r.status_code, "body": r.text[:200]}, "400")
        assert r.status_code == 400

    async def test_d6_ts_with_invalid_day(self, client, manifests_pool_seeded):
        # Feb 30 — strptime should reject
        r = await client.get(
            "/api/v1/manifests?fetched_at_gte=2026-02-30",
            headers=AUTH,
        )
        _record("D6_bad_day", {"status": r.status_code, "body": r.text[:200]}, "400")
        assert r.status_code == 400


# ===========================================================================
# 12. CROSS-ENDPOINT CONSISTENCY
# ===========================================================================


class TestCrossEndpoint:
    async def test_x1_jobs_unknown_field(self, client, jobs_pool_seeded):
        r = await client.get("/api/v1/jobs?nope=1", headers=AUTH)
        _record("X1_jobs_unknown_field", {"status": r.status_code, "body": r.text[:200]}, "400")
        assert r.status_code == 400

    async def test_x2_games_unknown_field(self, client, games_pool_100):
        r = await client.get("/api/v1/games?nope=1", headers=AUTH)
        _record("X2_games_unknown_field", {"status": r.status_code, "body": r.text[:200]}, "400")
        assert r.status_code == 400

    async def test_x3_games_progress_filter(self, client, games_pool_100):
        # progress is a Jobs-only field; on games it must be 400
        r = await client.get("/api/v1/games?progress_gte=0.5", headers=AUTH)
        _record("X3_games_progress_filter", {"status": r.status_code, "body": r.text[:200]}, "400")
        assert r.status_code == 400

    async def test_x4_jobs_invalid_state_value(self, client, jobs_pool_seeded):
        # state is str-typed; allow_list doesn't enum-validate — server returns 200
        # with empty matches. Possible smell: server should validate or just allow.
        r = await client.get("/api/v1/jobs?state=nonexistent", headers=AUTH)
        body = r.json()
        _record(
            "X4_jobs_unknown_state_value",
            {"status": r.status_code, "total": body["meta"]["total"]},
            "200 empty (str field has no enum validator)",
        )
        assert r.status_code == 200

    async def test_x5_games_invalid_platform(self, client, games_pool_100):
        r = await client.get("/api/v1/games?platform=psn", headers=AUTH)
        body = r.json()
        _record("X5_games_bad_platform", {"total": body["meta"]["total"]}, "200 empty")
        assert r.status_code == 200
        assert body["meta"]["total"] == 0

    async def test_x6_jobs_progress_out_of_range(self, client, jobs_pool_seeded):
        # progress=2.0 — value_type=float; no range validation on float
        r = await client.get("/api/v1/jobs?progress_gte=2.0", headers=AUTH)
        body = r.json()
        _record(
            "X6_jobs_progress_overrange",
            {"status": r.status_code, "total": body["meta"]["total"]},
            "200 empty (no per-field range validation)",
        )
        assert r.status_code == 200

    async def test_x7_progress_negative(self, client, jobs_pool_seeded):
        r = await client.get("/api/v1/jobs?progress_lte=-1", headers=AUTH)
        body = r.json()
        _record(
            "X7_jobs_progress_negative",
            {"status": r.status_code, "total": body["meta"]["total"]},
            "200 empty",
        )

    async def test_x8_progress_nan(self, client, jobs_pool_seeded):
        """BUG: float('NaN') passes _coerce_value (float() accepts 'NaN'),
        flows into Pydantic float field, then JSONResponse via stdlib json.dumps
        raises ValueError mid-serialization. ASGI returns a 500 if a handler
        catches it (here the exception escapes the handler — test reproduces
        the crash). Should be 400 at parse time."""
        try:
            r = await client.get("/api/v1/jobs?progress_gte=NaN", headers=AUTH)
            _record("X8_jobs_progress_nan", {"status": r.status_code, "body": r.text[:300]}, "400")
            assert r.status_code == 400, "NaN must be rejected at parse time"
        except ValueError as e:
            _record(
                "X8_jobs_progress_nan_UNHANDLED",
                {"raised": str(e), "severity": "SEV-2"},
                "400 at parse; ACTUAL: ValueError mid-response — SEV-2 BUG",
            )
            raise

    async def test_x9_progress_infinity(self, client, jobs_pool_seeded):
        """BUG: same root cause — float('Infinity') succeeds; later JSON
        serialization explodes."""
        try:
            r = await client.get("/api/v1/jobs?progress_gte=Infinity", headers=AUTH)
            _record("X9_jobs_progress_inf", {"status": r.status_code, "body": r.text[:300]}, "400")
            assert r.status_code == 400, "Infinity must be rejected at parse time"
        except ValueError as e:
            _record(
                "X9_jobs_progress_inf_UNHANDLED",
                {"raised": str(e), "severity": "SEV-2"},
                "400 at parse; ACTUAL: ValueError mid-response — SEV-2 BUG",
            )
            raise


# ===========================================================================
# 13. POST/DELETE on read-only endpoints
# ===========================================================================


class TestMethodNotAllowed:
    async def test_m1_post_to_manifests(self, client, manifests_pool_seeded):
        r = await client.post("/api/v1/manifests", headers=AUTH)
        _record("M1_post_manifests", {"status": r.status_code}, "405")
        assert r.status_code == 405

    async def test_m2_delete_to_games(self, client, games_pool_100):
        r = await client.delete("/api/v1/games", headers=AUTH)
        _record("M2_delete_games", {"status": r.status_code}, "405")
        assert r.status_code == 405

    async def test_m3_put_to_jobs(self, client, jobs_pool_seeded):
        r = await client.put("/api/v1/jobs", headers=AUTH)
        _record("M3_put_jobs", {"status": r.status_code}, "405")
        assert r.status_code == 405


# ===========================================================================
# 14. GET with body (FastAPI behavior)
# ===========================================================================


class TestGetWithBody:
    async def test_b1_get_with_giant_body(self, client, manifests_pool_seeded):
        # 64 KiB body on a GET — exceeds 32 KiB cap → 413
        big = b"x" * (64 * 1024)
        r = await client.request(
            "GET",
            "/api/v1/manifests",
            headers={**AUTH, "Content-Type": "application/octet-stream"},
            content=big,
        )
        _record("B1_get_giant_body", {"status": r.status_code, "body": r.text[:200]}, "413")
        assert r.status_code == 413

    async def test_b2_get_with_small_body(self, client, manifests_pool_seeded):
        # 1 KiB body — under cap → 200 (FastAPI ignores body on GET)
        small = b"x" * 1024
        r = await client.request(
            "GET",
            "/api/v1/manifests?limit=1",
            headers={**AUTH, "Content-Type": "application/json"},
            content=small,
        )
        _record("B2_get_small_body", {"status": r.status_code}, "200 (body ignored)")
        assert r.status_code == 200


# ===========================================================================
# 15. PLATFORMS / HEALTH
# ===========================================================================


class TestPlatformsHealth:
    async def test_pl1_platforms_query_param_silently_ignored(self, client, manifests_pool_seeded):
        # Platforms has NO query-helpers. Random query params should be ignored.
        r = await client.get("/api/v1/platforms?evil=1&nuke=please", headers=AUTH)
        _record(
            "PL1_platforms_random_params",
            {"status": r.status_code, "body": r.text[:300]},
            "200 (ignored)",
        )
        assert r.status_code == 200

    async def test_h1_health_no_auth(self, client, manifests_pool_seeded):
        r = await client.get("/api/v1/health")
        _record("HL1_health_no_auth", {"status": r.status_code, "body": r.text[:300]}, "200 or 503")
        assert r.status_code in (200, 503)

    async def test_h2_health_with_unknown_query(self, client, manifests_pool_seeded):
        r = await client.get("/api/v1/health?include=secret_token")
        _record("HL2_health_query_ignored", {"status": r.status_code}, "200")
        assert r.status_code in (200, 503)

    async def test_h3_healthxxx_substring(self, client, manifests_pool_seeded):
        # /api/v1/healthxxx must NOT be exempt (UAT-3 S2-A)
        r = await client.get("/api/v1/healthxxx")
        _record("HL3_healthxxx_substring", {"status": r.status_code}, "401 (not exempt)")
        assert r.status_code == 401
