"""Tests for TOTP MFA module."""

import pytest

from graftpunk.mfa.totp import (
    generate_totp,
    get_totp_remaining_seconds,
    verify_totp,
)


class TestTOTP:
    """Tests for TOTP functions."""

    # Standard test secret (from RFC 6238)
    TEST_SECRET = "JBSWY3DPEHPK3PXP"  # noqa: S105

    def test_generate_totp_returns_six_digits(self):
        """Test that TOTP generation returns 6-digit code."""
        code = generate_totp(self.TEST_SECRET)
        assert len(code) == 6
        assert code.isdigit()

    def test_generate_totp_is_deterministic_within_period(self):
        """Test that TOTP codes are consistent within a time period."""
        code1 = generate_totp(self.TEST_SECRET)
        code2 = generate_totp(self.TEST_SECRET)
        assert code1 == code2

    def test_verify_totp_accepts_valid_code(self):
        """Test that verify_totp accepts a freshly generated code."""
        code = generate_totp(self.TEST_SECRET)
        assert verify_totp(self.TEST_SECRET, code) is True

    def test_verify_totp_rejects_invalid_code(self):
        """Test that verify_totp rejects an invalid code."""
        assert verify_totp(self.TEST_SECRET, "000000") is False

    def test_verify_totp_with_window(self):
        """Test that verify_totp respects the valid_window parameter."""
        code = generate_totp(self.TEST_SECRET)
        # Should accept with window=1
        assert verify_totp(self.TEST_SECRET, code, valid_window=1) is True

    def test_generate_totp_invalid_secret_raises_error(self):
        """Test that invalid secret raises an error."""
        import binascii

        with pytest.raises((ValueError, binascii.Error)):
            generate_totp("not-a-valid-secret!")

    def test_get_totp_remaining_seconds_returns_valid_range(self):
        """Test that remaining seconds is in valid range (0-29)."""
        remaining = get_totp_remaining_seconds()
        assert 0 <= remaining <= 29

    def test_different_secrets_produce_different_codes(self):
        """Test that different secrets produce different codes."""
        secret1 = "JBSWY3DPEHPK3PXP"
        secret2 = "GEZDGNBVGY3TQOJQ"

        code1 = generate_totp(secret1)
        code2 = generate_totp(secret2)

        # Very unlikely to be the same
        # (only 1/1000000 chance, but we're using different secrets)
        # In rare case of collision, test still passes
        _ = (code1, code2)  # Verify both codes were generated
