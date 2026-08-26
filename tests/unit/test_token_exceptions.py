"""Token extraction failures are typed (#131) and stay ValueError-compatible."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

import graftpunk
from graftpunk.exceptions import (
    GraftpunkError,
    SessionInvalidatedError,
    TokenExtractionError,
    TokenPatternMismatchError,
)
from graftpunk.tokens import Token, extract_token


def test_hierarchy_and_backward_compatibility() -> None:
    for cls in (TokenExtractionError, SessionInvalidatedError, TokenPatternMismatchError):
        assert issubclass(cls, GraftpunkError)
        assert issubclass(cls, ValueError)  # earlier releases raised plain ValueError
    assert issubclass(SessionInvalidatedError, TokenExtractionError)
    assert issubclass(TokenPatternMismatchError, TokenExtractionError)
    for name in ("TokenExtractionError", "SessionInvalidatedError", "TokenPatternMismatchError"):
        assert name in graftpunk.__all__ and getattr(graftpunk, name) is not None


def test_missing_cookie_is_session_invalidated() -> None:
    session = requests.Session()
    token = Token(name="csrf", source="cookie", cookie_name="csrftoken")
    with pytest.raises(SessionInvalidatedError, match="Cookie 'csrftoken' not found"):
        extract_token(session, token, "https://example.com")


def test_missing_header_is_pattern_mismatch() -> None:
    session = MagicMock()
    session.head.return_value = MagicMock(headers={})
    token = Token(name="t", source="response_header", response_header="X-Token", extraction="http")
    with pytest.raises(TokenPatternMismatchError, match="Header 'X-Token' not found"):
        extract_token(session, token, "https://example.com")


def test_unmatched_pattern_is_pattern_mismatch() -> None:
    session = MagicMock()
    session.get.return_value = MagicMock(text="<html>no token here</html>")
    token = Token(name="t", source="page", pattern=r"token=(\w+)", extraction="http")
    with pytest.raises(TokenPatternMismatchError, match="Token pattern not found"):
        extract_token(session, token, "https://example.com")


def test_browser_extraction_failure_is_extraction_error() -> None:
    from graftpunk.tokens import TokenConfig, prepare_session

    session = requests.Session()
    token = Token(name="t", source="page", pattern=r"token=(\w+)", extraction="browser")
    with (
        patch("graftpunk.tokens._run_browser_extraction", return_value={}),
        pytest.raises(TokenExtractionError, match="Browser extraction failed for token 't'"),
    ):
        prepare_session(session, TokenConfig(tokens=(token,)), "https://example.com")
