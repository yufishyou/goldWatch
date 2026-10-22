"""状态持久化与单实例文件锁。"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

try:  # macOS / Linux
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]


class StateError(RuntimeError):
    """状态文件损坏或无法读写。"""


class LockError(RuntimeError):
    """已有另一个检查进程在运行。"""


def default_state() -> dict[str, Any]:
    return {"version": 1, "rules": {}}


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return default_state()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StateError(
                f"状态文件 {self.path} 不是合法 JSON：{exc}。"
                f"可以备份后删除它，让工具重新开始。"
            ) from exc
        except OSError as exc:
            raise StateError(f"无法读取状态文件 {self.path}：{exc}") from exc
        if not isinstance(data, dict):
            raise StateError(f"状态文件 {self.path} 的顶层必须是对象")
        data.setdefault("version", 1)
        if not isinstance(data.get("rules"), dict):
            data["rules"] = {}
        return data

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        fd, temporary = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise


class FileLock:
    """非阻塞独占锁；拿不到锁说明另一个检查还在跑。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+", encoding="utf-8")
        if fcntl is not None:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                self._handle.close()
                self._handle = None
                raise LockError(f"另一个检查正在运行（锁文件 {self.path}）") from exc
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._handle is None:
            return
        if fcntl is not None:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        self._handle.close()
        self._handle = None
