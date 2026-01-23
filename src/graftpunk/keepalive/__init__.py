"""Keepalive daemon for maintaining browser sessions.

This package provides:
- KeepaliveHandler protocol for site-specific keepalive logic
- GenericHTTPHandler for simple HTTP endpoint keepalive
- KeepaliveState for daemon state management
- run_keepalive_daemon for the main daemon entry point
"""

from graftpunk.keepalive.handler import (
    GenericHTTPHandler,
    KeepaliveHandler,
    SessionStatus,
)
from graftpunk.keepalive.state import (
    DaemonStatus,
    KeepaliveState,
)

__all__ = [
    # Handler protocol
    "KeepaliveHandler",
    "SessionStatus",
    "GenericHTTPHandler",
    # State management
    "DaemonStatus",
    "KeepaliveState",
]
