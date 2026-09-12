"""The request policy behind ``CommandContext.request_json``/``request_text``.

Every hand-written and generated command needs to decide two things: which
header role a request carries, and what a non-2xx or a not-JSON 2xx means.
``SiteRequests`` owns both, out of ``CommandContext`` itself, so the policy
has its own file and tests (plugin tooling spec, 2026-09-11, design note on
the "session expired" primitive).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from graftpunk.exceptions import CommandError, SessionRejectedError, UnexpectedResponseError
from graftpunk.har.documents import is_login_document
from graftpunk.logging import get_logger

LOG = get_logger(__name__)

_REJECTED_STATUSES = frozenset({401, 403})
_ROLES_WARNED_ATTR = "_gp_roles_unavailable_warned"

__all__ = ["SiteRequests"]


def _path_of(url: str | None) -> str:
    return urlparse(url or "").path or "/"


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

        Raises:
            SessionRejectedError: 401/403, or a 2xx whose body is a login
                page (``har.documents.is_login_document``).
            UnexpectedResponseError: A 2xx whose body is neither JSON nor a
                login page.
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
        return response.json()

    def text(self, method: str, url: str, *, role: str = "navigation", **kwargs: Any) -> str:
        """Send *method* *url* with role headers; return any 2xx body as text.

        Rejection is detected by status only, so an HTML endpoint's real
        2xx body is never mistaken for an expired session.
        """
        response = self._send(method, url, role, **kwargs)
        self._raise_for_rejection(response)
        return response.text
