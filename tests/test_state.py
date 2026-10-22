from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from goldwatch.state import FileLock, LockError, StateError, StateStore


class StateTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_missing_state_defaults(self) -> None:
        store = StateStore(self.root / "state.json")
        self.assertEqual(store.load(), {"version": 1, "rules": {}})

    def test_round_trip(self) -> None:
        store = StateStore(self.root / "nested" / "state.json")
        data = {"version": 1, "rules": {"x": {"triggered": True}}}
        store.save(data)
        self.assertEqual(store.load(), data)
        self.assertEqual((self.root / "nested" / "state.json").stat().st_mode & 0o777, 0o600)

    def test_corrupt_state_raises(self) -> None:
        path = self.root / "state.json"
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(StateError):
            StateStore(path).load()

    def test_lock_is_exclusive(self) -> None:
        path = self.root / "state.json.lock"
        with FileLock(path):
            with self.assertRaises(LockError):
                with FileLock(path):
                    pass

    def test_lock_released_after_exit(self) -> None:
        path = self.root / "state.json.lock"
        with FileLock(path):
            pass
        with FileLock(path):
            pass


if __name__ == "__main__":
    unittest.main()
