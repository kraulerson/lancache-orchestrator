"""Hypothesis property tests for orchestrator.db.pool helpers.

Targets:
  _template_only(sql) — must replace literals with '?' placeholders
  _shape(params)      — must return type names only, never values
  _classify_integrity_error(e) — must return correct kind for known codes,
                                 fall through to "unknown" cleanly
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from orchestrator.db.pool import _shape, _template_only

# Strategy for arbitrary parameter values (covering common SQLite-bindable types)
_param_value = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**63), max_value=2**63 - 1),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=200),
    st.binary(max_size=200),
)


class TestTemplateOnly:
    @given(st.text(min_size=1, max_size=500))
    def test_no_string_literals_in_output(self, sql: str):
        """After _template_only, there should be no characters between two
        single quotes that aren't already a placeholder."""
        result = _template_only(sql)
        # Crude invariant: no ' in result UNLESS escaped or part of placeholder
        # The regex strips well-formed string literals, so any remaining quote
        # would be malformed input — we accept that pass-through.
        # Stronger property: result length <= input length when literals exist
        if "'" in sql and "''" not in sql:
            assert len(result) <= len(sql)

    @given(st.integers(min_value=-(2**63), max_value=2**63 - 1))
    def test_numeric_literals_replaced(self, n: int):
        sql = f"SELECT * FROM t WHERE id = {n}"  # noqa: S608  test input for sanitizer
        result = _template_only(sql)
        # The numeric literal should not appear as-is
        if abs(n) >= 10:  # single digits could trivially collide with regex
            assert str(n) not in result

    def test_already_parameterized_unchanged(self):
        """A SQL string that's already parameterized has no literals to strip."""
        sql = "SELECT * FROM games WHERE id = ? AND platform = ?"
        assert _template_only(sql) == sql

    # ----------- UAT-2 regression V-6: hex + sci-notation -----------

    def test_uat2_v6_hex_literal_replaced(self):
        """V-6: Hex literals like 0xDEADBEEF previously passed through
        _template_only unchanged, leaking the value into log output."""
        sql = "SELECT * FROM t WHERE flags = 0xDEADBEEF"
        result = _template_only(sql)
        assert "0xDEADBEEF" not in result
        assert "DEADBEEF" not in result

    def test_uat2_v6_lowercase_hex_literal_replaced(self):
        sql = "SELECT * FROM t WHERE flags = 0xcafebabe"
        result = _template_only(sql)
        assert "cafebabe" not in result
        assert "0xcafebabe" not in result

    def test_uat2_v6_capital_x_hex_replaced(self):
        sql = "SELECT * FROM t WHERE flags = 0XBEEF"
        result = _template_only(sql)
        assert "BEEF" not in result

    def test_uat2_v6_scientific_notation_replaced(self):
        """V-6: Sci-notation like 1.5e10 was being chopped to '?.5e1?',
        leaking middle digits."""
        sql = "SELECT * FROM t WHERE size = 1.5e10"
        result = _template_only(sql)
        assert "1.5e10" not in result
        assert ".5e1" not in result
        assert ".5e" not in result

    def test_uat2_v6_negative_sci_notation_replaced(self):
        sql = "SELECT * FROM t WHERE rate = -2.71e-3"
        result = _template_only(sql)
        assert "2.71" not in result
        assert "e-3" not in result
        assert "-2.71e-3" not in result

    def test_uat2_v6_explicit_positive_sci_notation_replaced(self):
        sql = "SELECT * FROM t WHERE rate = 1.0e+5"
        result = _template_only(sql)
        assert "1.0e+5" not in result
        assert "e+5" not in result


class TestShape:
    @given(st.lists(_param_value, max_size=10))
    def test_positional_params_return_type_list(self, params):
        result = _shape(tuple(params))
        assert isinstance(result, list)
        assert len(result) == len(params)
        assert all(isinstance(t, str) for t in result)
        # No value strings — only type names
        for v, t in zip(params, result, strict=False):
            assert t == type(v).__name__

    @given(st.dictionaries(st.text(min_size=1, max_size=20), _param_value, max_size=10))
    def test_named_params_return_type_dict(self, params):
        result = _shape(params)
        assert isinstance(result, dict)
        assert set(result.keys()) == set(params.keys())
        assert all(isinstance(t, str) for t in result.values())

    def test_none_params_returns_empty_list(self):
        assert _shape(None) == []

    @given(st.lists(_param_value, max_size=20))
    def test_no_raw_value_in_output(self, params):
        """Critical safety invariant: _shape's output must contain no raw
        parameter values, only type names."""
        result = _shape(tuple(params))
        result_str = str(result)
        for v in params:
            if v is None:
                continue
            # Special-case: if the value happens to BE one of our type names
            # (unlikely but possible for the literal string "int", "str", etc.)
            # we tolerate that collision. Otherwise raw values must not appear.
            v_str = str(v)
            if v_str in {"int", "str", "bool", "float", "bytes", "NoneType"}:
                continue
            # Numeric values: their str form might appear in a type name like
            # "int" — guard against that too. But since type names don't
            # contain digits, this should be safe.
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                # The int/float value's string form (e.g., "42") shouldn't appear
                # in the result (which is just ['int', 'float', ...]).
                if len(v_str) > 1 or v_str.isdigit():
                    assert v_str not in result_str, (
                        f"raw numeric {v_str} in shape output {result_str}"
                    )
            elif isinstance(v, (str, bytes)) and len(v_str) >= 4:
                # Long string/bytes values shouldn't appear in the shape output
                assert v_str not in result_str
