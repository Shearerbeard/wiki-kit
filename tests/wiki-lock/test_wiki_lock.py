#!/usr/bin/env python3
"""Tests for the wiki_lock writer lock (Rank 1.6)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.wiki_lock import (  # noqa: E402
    DEFAULT_LOCK_TIMEOUT_SECONDS,
    LOCK_FILE_NAME,
    LOCK_STALE_TTL_SECONDS,
    LOCK_TIMEOUT_ENV_VAR,
    EventWriteLock,
    lock_timeout_seconds,
    parse_lock_pid,
    pid_alive,
)


class EventWriteLockTest(unittest.TestCase):
    def lock_path(self, events_dir: Path) -> Path:
        return events_dir / LOCK_FILE_NAME

    def backdate(self, path: Path, seconds: float) -> None:
        past = time.time() - seconds
        os.utime(path, (past, past))

    def test_acquire_and_release(self) -> None:
        with TemporaryDirectory() as tmp:
            events_dir = Path(tmp) / "events"
            with EventWriteLock(events_dir, timeout_seconds=0):
                self.assertTrue(self.lock_path(events_dir).is_file())
                text = self.lock_path(events_dir).read_text()
                self.assertEqual(parse_lock_pid(text), os.getpid())
            self.assertFalse(self.lock_path(events_dir).exists())

    def test_held_by_live_pid_times_out(self) -> None:
        with TemporaryDirectory() as tmp:
            events_dir = Path(tmp) / "events"
            events_dir.mkdir()
            self.lock_path(events_dir).write_text(f"pid={os.getpid()}\n")
            with self.assertRaises(RuntimeError) as ctx:
                EventWriteLock(events_dir, timeout_seconds=0).__enter__()
            # The timeout error must name the holder and the override knob.
            self.assertIn(f"pid={os.getpid()}", str(ctx.exception))
            self.assertIn(LOCK_TIMEOUT_ENV_VAR, str(ctx.exception))

    def test_dead_pid_lock_is_cleared(self) -> None:
        with TemporaryDirectory() as tmp:
            events_dir = Path(tmp) / "events"
            events_dir.mkdir()
            # A pid known to be dead: a reaped child can't be recycled
            # within the lifetime of this test.
            proc = subprocess.Popen(["true"])
            proc.wait()
            self.lock_path(events_dir).write_text(f"pid={proc.pid}\n")
            with EventWriteLock(events_dir, timeout_seconds=0):
                self.assertTrue(self.lock_path(events_dir).is_file())

    def test_unparseable_fresh_lock_fails_closed(self) -> None:
        with TemporaryDirectory() as tmp:
            events_dir = Path(tmp) / "events"
            events_dir.mkdir()
            self.lock_path(events_dir).write_text("not a pid file\n")
            with self.assertRaises(RuntimeError):
                EventWriteLock(events_dir, timeout_seconds=0).__enter__()

    def test_unparseable_lock_older_than_ttl_is_cleared(self) -> None:
        with TemporaryDirectory() as tmp:
            events_dir = Path(tmp) / "events"
            events_dir.mkdir()
            stale = self.lock_path(events_dir)
            stale.write_text("not a pid file\n")
            self.backdate(stale, LOCK_STALE_TTL_SECONDS + 60)
            with EventWriteLock(events_dir, timeout_seconds=0):
                # Acquisition succeeded (would raise without the TTL branch)
                # and the lock file is now ours, freshly written.
                self.assertEqual(parse_lock_pid(stale.read_text()), os.getpid())

    def test_live_pid_lock_older_than_ttl_is_cleared(self) -> None:
        # The pid-recycling case: holder pid looks alive but the lock has
        # outlived any legitimate hold.
        with TemporaryDirectory() as tmp:
            events_dir = Path(tmp) / "events"
            events_dir.mkdir()
            stale = self.lock_path(events_dir)
            stale.write_text("pid=99999999\n")
            self.backdate(stale, LOCK_STALE_TTL_SECONDS + 60)
            with (
                mock.patch("scripts.wiki_lock.pid_alive", return_value=True),
                EventWriteLock(events_dir, timeout_seconds=0),
            ):
                self.assertEqual(parse_lock_pid(stale.read_text()), os.getpid())

    def test_exit_does_not_unlink_when_never_locked(self) -> None:
        with TemporaryDirectory() as tmp:
            events_dir = Path(tmp) / "events"
            events_dir.mkdir()
            self.lock_path(events_dir).write_text(f"pid={os.getpid()}\n")
            lock = EventWriteLock(events_dir, timeout_seconds=0)
            with self.assertRaises(RuntimeError):
                lock.__enter__()
            lock.__exit__(None, None, None)
            # The competing holder's lock file must survive our failed attempt.
            self.assertTrue(self.lock_path(events_dir).is_file())


class TimeoutConfigTest(unittest.TestCase):
    def test_default_without_env(self) -> None:
        with mock.patch.dict(os.environ, clear=False):
            os.environ.pop(LOCK_TIMEOUT_ENV_VAR, None)
            self.assertEqual(lock_timeout_seconds(), DEFAULT_LOCK_TIMEOUT_SECONDS)

    def test_env_override(self) -> None:
        with mock.patch.dict(os.environ, {LOCK_TIMEOUT_ENV_VAR: "2.5"}):
            self.assertEqual(lock_timeout_seconds(), 2.5)

    def test_garbage_env_fails_loud(self) -> None:
        with (
            mock.patch.dict(os.environ, {LOCK_TIMEOUT_ENV_VAR: "soon"}),
            self.assertRaises(ValueError),
        ):
            lock_timeout_seconds()

    def test_constructor_resolves_env_when_unset(self) -> None:
        with (
            mock.patch.dict(os.environ, {LOCK_TIMEOUT_ENV_VAR: "0"}),
            TemporaryDirectory() as tmp,
        ):
            lock = EventWriteLock(Path(tmp) / "events")
            self.assertEqual(lock.timeout_seconds, 0.0)


class PidHelpersTest(unittest.TestCase):
    def test_parse_lock_pid(self) -> None:
        self.assertEqual(parse_lock_pid("pid=42\ntimestamp_utc=x\n"), 42)
        self.assertIsNone(parse_lock_pid("pid=not-a-number\n"))
        self.assertIsNone(parse_lock_pid(""))
        self.assertIsNone(parse_lock_pid("timestamp_utc=x\n"))

    def test_pid_alive_self(self) -> None:
        self.assertTrue(pid_alive(os.getpid()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
