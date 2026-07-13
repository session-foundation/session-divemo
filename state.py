"""Persistent per-platform state and the version-transition state machine.

State (the last-seen GitHub and store versions plus whether a warning is
currently outstanding) lives in a small SQLite database so the monitor only
raises a warning once, only sends an all-clear after a real warning, and can
survive restarts without re-alerting.

The decision logic in :func:`evaluate` is deliberately pure (no IO), so it can
be unit tested exhaustively.
"""

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# Event kinds emitted by evaluate():
WARNING = "warning"                # store is ahead of GitHub (tag users)
WARNING_UPDATE = "warning_update"  # already warning, store moved further ahead
ALL_CLEAR = "all_clear"            # GitHub caught up; warning resolved
INFO_GITHUB = "info_github"        # GitHub advanced (informational)
INFO_STORE = "info_store"          # store advanced (informational)


@dataclass(frozen=True)
class PlatformState:
    github_version: str
    store_version: str
    warning_active: bool
    initialized: bool


@dataclass
class Event:
    kind: str
    github_version: str
    store_version: str
    # True when raised while seeding a brand-new platform (store already ahead).
    initial: bool = False


@dataclass
class EvalResult:
    events: list = field(default_factory=list)
    warning_active: bool = False
    initialized: bool = True


def evaluate(prev, github_version, store_version, compare):
    """Decide which events to emit given the previous state and new versions.

    ``prev`` is a :class:`PlatformState` or ``None`` for a never-seen platform.
    ``compare(a, b)`` returns -1/0/1. Returns an :class:`EvalResult`.
    """
    store_ahead = compare(store_version, github_version) > 0

    # First observation of this platform: seed the baseline silently, but still
    # raise a warning if the store is *already* ahead of GitHub right now.
    if prev is None or not prev.initialized:
        events = []
        if store_ahead:
            events.append(Event(WARNING, github_version, store_version, initial=True))
        return EvalResult(events=events, warning_active=store_ahead, initialized=True)

    github_advanced = compare(github_version, prev.github_version) > 0
    store_advanced = compare(store_version, prev.store_version) > 0

    events = []
    warning_active = prev.warning_active

    if store_ahead and not warning_active:
        events.append(Event(WARNING, github_version, store_version))
        warning_active = True
    elif store_ahead and warning_active and store_advanced:
        # Still ahead, but the store shipped an even newer build: re-tag.
        events.append(Event(WARNING_UPDATE, github_version, store_version))
    elif not store_ahead and warning_active:
        events.append(Event(ALL_CLEAR, github_version, store_version))
        warning_active = False

    # Quiet movement (no warning transition this cycle) is reported as
    # informational status updates.
    if not events:
        if github_advanced:
            events.append(Event(INFO_GITHUB, github_version, store_version))
        if store_advanced:
            events.append(Event(INFO_STORE, github_version, store_version))

    return EvalResult(events=events, warning_active=warning_active, initialized=True)


class StateStore:
    """SQLite-backed store of the last-seen versions per platform."""

    def __init__(self, path):
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS platform_state (
                platform       TEXT PRIMARY KEY,
                github_version TEXT NOT NULL,
                store_version  TEXT NOT NULL,
                warning_active INTEGER NOT NULL DEFAULT 0,
                updated_at     TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def get(self, platform):
        row = self._conn.execute(
            "SELECT github_version, store_version, warning_active "
            "FROM platform_state WHERE platform = ?",
            (platform,),
        ).fetchone()
        if row is None:
            return None
        return PlatformState(
            github_version=row["github_version"],
            store_version=row["store_version"],
            warning_active=bool(row["warning_active"]),
            initialized=True,
        )

    def upsert(self, platform, github_version, store_version, warning_active):
        self._conn.execute(
            """
            INSERT INTO platform_state
                (platform, github_version, store_version, warning_active, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(platform) DO UPDATE SET
                github_version = excluded.github_version,
                store_version  = excluded.store_version,
                warning_active = excluded.warning_active,
                updated_at     = excluded.updated_at
            """,
            (
                platform,
                github_version,
                store_version,
                1 if warning_active else 0,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._conn.commit()

    def close(self):
        self._conn.close()
