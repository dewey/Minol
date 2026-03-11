"""Unit tests for the pure helper functions in api.py."""

import pytest

from custom_components.minol_energy.api import (
    _b2c_base_url,
    _extract_b2c_settings,
)


# ---------------------------------------------------------------------------
# _extract_b2c_settings
# ---------------------------------------------------------------------------


class TestExtractB2cSettings:
    """Tests for _extract_b2c_settings()."""

    def test_config_pattern(self):
        """Parses $Config={...} correctly."""
        html = (
            "<script>$Config="
            '{"csrf":"tok","transId":"tx1","policy":"B2C_1A_P"};\n'
            "</script>"
        )
        result = _extract_b2c_settings(html)
        assert result == {"csrf": "tok", "transId": "tx1", "policy": "B2C_1A_P"}

    def test_var_settings_pattern(self):
        """Parses var SETTINGS={...} as a fallback."""
        html = (
            "<script>var SETTINGS = "
            '{"csrf":"tok2","transId":"tx2","policy":"B2C_1A_Q"};\n'
            "</script>"
        )
        result = _extract_b2c_settings(html)
        assert result == {"csrf": "tok2", "transId": "tx2", "policy": "B2C_1A_Q"}

    def test_config_pattern_takes_priority_over_settings(self):
        """$Config wins when both patterns are present."""
        html = (
            "<script>$Config="
            '{"csrf":"from_config","transId":"tx","policy":"B2C_1A_P"};\n'
            "var SETTINGS = "
            '{"csrf":"from_settings","transId":"tx","policy":"B2C_1A_Q"};\n'
            "</script>"
        )
        result = _extract_b2c_settings(html)
        assert result["csrf"] == "from_config"

    def test_missing_returns_empty_dict(self):
        """Returns {} when no recognised pattern is present."""
        html = "<html><body>No config here</body></html>"
        assert _extract_b2c_settings(html) == {}

    def test_invalid_json_returns_empty_dict(self):
        """Returns {} when the matched block is not valid JSON."""
        html = "<script>$Config={broken json;\n</script>"
        assert _extract_b2c_settings(html) == {}

    def test_empty_string(self):
        """Returns {} for an empty input string."""
        assert _extract_b2c_settings("") == {}

    def test_nested_json(self):
        """Correctly parses a config with nested objects."""
        html = (
            "<script>$Config="
            '{"csrf":"c","transId":"t","policy":"p","extra":{"a":1}};\n'
            "</script>"
        )
        result = _extract_b2c_settings(html)
        assert result["extra"] == {"a": 1}


# ---------------------------------------------------------------------------
# _b2c_base_url
# ---------------------------------------------------------------------------


class TestB2cBaseUrl:
    """Tests for _b2c_base_url()."""

    def test_full_b2c_url(self):
        """Strips everything after tenant/policy."""
        url = (
            "https://minolauth.b2clogin.com"
            "/minolauth.onmicrosoft.com/B2C_1A_SIGNIN"
            "/api/StateProperties=ABC/confirmed"
        )
        result = _b2c_base_url(url)
        assert result == (
            "https://minolauth.b2clogin.com"
            "/minolauth.onmicrosoft.com/B2C_1A_SIGNIN"
        )

    def test_url_with_only_two_path_parts(self):
        """Returns scheme://host/part1/part2 when exactly two path segments."""
        url = "https://minolauth.b2clogin.com/tenant/policy"
        result = _b2c_base_url(url)
        assert result == "https://minolauth.b2clogin.com/tenant/policy"

    def test_url_with_single_path_part(self):
        """Falls back to scheme://host when only one path segment."""
        url = "https://minolauth.b2clogin.com/onlyone"
        result = _b2c_base_url(url)
        assert result == "https://minolauth.b2clogin.com"

    def test_url_with_no_path(self):
        """Falls back to scheme://host when path is empty."""
        url = "https://minolauth.b2clogin.com"
        result = _b2c_base_url(url)
        assert result == "https://minolauth.b2clogin.com"
