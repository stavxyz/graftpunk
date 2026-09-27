"""The request policy behind ``CommandContext.request_json``/``request_text``.

Every hand-written and generated command needs to decide two things: which
header role a request carries, and what a non-2xx or a not-JSON 2xx means.
``SiteRequests`` owns both, out of ``CommandContext`` itself, so the policy
has its own file and tests (plugin tooling spec, 2026-09-11, design note on
the "session expired" primitive).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from graftpunk.exceptions import CommandError, SessionRejectedError, UnexpectedResponseError
from graftpunk.har.documents import is_login_document
from graftpunk.logging import get_logger

LOG = get_logger(__name__)

_REJECTED_STATUSES = frozenset({401, 403})
_ROLES_WARNED_ATTR = "_gp_roles_unavailable_warned"
# The request arguments whose values a site reads as text, so a Python value
# has to be spelled the way the site spells it.
_NORMALISED_ARGUMENTS = ("params", "data")
# What a site means by a boolean. ``requests`` serialises a Python bool with
# str(), which sends "True"/"False": a site that recorded "keywordSearch=false"
# does not recognise either.
_BOOLEAN_TEXT = {True: "true", False: "false"}

__all__ = ["SiteRequests"]


def _path_of(url: str | None) -> str:
    return urlparse(url or "").path or "/"


def _normalised_value(value: Any) -> Any:
    """One ``params``/``data`` value as the site spells it: a bool becomes
    ``"true"``/``"false"``, a sequence has each of its items normalised and its
    ``None`` items dropped, anything else is passed through untouched."""
    if isinstance(value, bool):
        return _BOOLEAN_TEXT[value]
    if isinstance(value, (list, tuple)):
        return [_normalised_value(item) for item in value if item is not None]
    return value


def _normalised_arguments(mapping: Any) -> Any:
    """*mapping* with every value normalised and every ``None``-valued key dropped.

    A generated command stub declares each site parameter ``... | None = None``
    and passes the lot, so ``None`` is the stub's way of saying "the caller did
    not ask for this parameter": sending it would add an empty value the site
    never saw. Anything that is not a mapping (a raw string or bytes body, or a
    list of pairs) is returned as it came, booleans included; a generated stub
    never passes that shape.
    """
    if not isinstance(mapping, Mapping):
        return mapping
    return {key: _normalised_value(value) for key, value in mapping.items() if value is not None}


class SiteRequests:
    """Applies the role, rejection, and body-shape policy for one plugin's requests."""

    def __init__(self, session: requests.Session, plugin_name: str, base_url: str) -> None:
        self._session = session
        self._plugin_name = plugin_name
        self._base_url = base_url

    def _resolve(self, url: str) -> str:
        return urljoin(self._base_url, url) if self._base_url else url

    def _warn_no_roles_once(self) -> None:
        if getattr(self._session, _ROLES_WARNED_ATTR, False):
            return
        setattr(self._session, _ROLES_WARNED_ATTR, True)
        LOG.warning("session_roles_unavailable", plugin=self._plugin_name)

    def _send(self, method: str, url: str, role: str, **kwargs: Any) -> requests.Response:
        full_url = self._resolve(url)
        for name in _NORMALISED_ARGUMENTS:
            if name in kwargs:
                kwargs[name] = _normalised_arguments(kwargs[name])
        request_with_role = getattr(self._session, "request_with_role", None)
        if callable(request_with_role):
            return request_with_role(role, method, full_url, **kwargs)
        self._warn_no_roles_once()
        return self._session.request(method, full_url, **kwargs)

    def _raise_for_rejection(self, response: requests.Response) -> None:
        method = (response.request.method if response.request is not None else None) or "GET"
        path = _path_of(response.url)
        # requests types status_code as int | None; a response object that
        # reached this point always has one set, so 0 here is unreachable in
        # practice and only satisfies the type checker.
        status = response.status_code or 0
        if status in _REJECTED_STATUSES:
            raise SessionRejectedError(self._plugin_name, method, path, status)
        if status >= 400:
            raise CommandError(f"{method} {path} returned {status}")

    def json(self, method: str, url: str, *, role: str = "xhr", **kwargs: Any) -> Any:
        """Send *method* *url* with role headers; return the parsed JSON body.

        A ``params`` or ``data`` mapping is normalised first: ``True`` and
        ``False`` are sent as ``"true"`` and ``"false"``, and a key whose value
        is ``None`` is left out of the request entirely.

        Raises:
            SessionRejectedError: 401/403, or a 2xx whose body is a login
                page (``har.documents.is_login_document``).
            UnexpectedResponseError: A 2xx whose body is neither JSON nor a
                login page, or that declares JSON but does not parse as JSON
                (a truncated response).
            CommandError: Any other 4xx or 5xx.
        """
        response = self._send(method, url, role, **kwargs)
        self._raise_for_rejection(response)
        content_type = response.headers.get("Content-Type", "")
        if "json" not in content_type.lower():
            if is_login_document(response.text):
                raise SessionRejectedError(
                    self._plugin_name, method, _path_of(response.url), response.status_code or 0
                )
            raise UnexpectedResponseError(method, _path_of(response.url), content_type)
        try:
            return response.json()
        except ValueError:
            # A declared content type is not a guarantee. An escaping
            # JSONDecodeError reached the CLI as a traceback rather than as the
            # one refusal line every other failure gets.
            raise UnexpectedResponseError(method, _path_of(response.url), content_type) from None

    def text(self, method: str, url: str, *, role: str = "navigation", **kwargs: Any) -> str:
        """Send *method* *url* with role headers; return any 2xx body as text.

        A ``params`` or ``data`` mapping is normalised the way :meth:`json`
        normalises it: ``True`` and ``False`` are sent as ``"true"`` and
        ``"false"``, and a key whose value is ``None`` is left out of the
        request entirely.

        Rejection is detected by status only, so an HTML endpoint's real
        2xx body is never mistaken for an expired session.
        """
        response = self._send(method, url, role, **kwargs)
        self._raise_for_rejection(response)
        return response.text
