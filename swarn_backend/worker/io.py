"""The worker's side of the pipe: a thread-safe event writer on the real
stdout and a reader thread for commands on stdin."""

from __future__ import annotations

import json
import sys
import threading
import time
from collections.abc import Callable
from typing import IO, Any


class Emitter:
    def __init__(self, out: IO[str]):
        self._out = out
        self._lock = threading.Lock()

    def __call__(self, t: str, **payload: Any) -> None:
        self.emit(t, **payload)

    def emit(self, t: str, **payload: Any) -> None:
        line = json.dumps({"t": t, "ts": time.time(), **payload}, default=str, ensure_ascii=False)
        with self._lock:
            try:
                self._out.write(line + "\n")
                self._out.flush()
            except (BrokenPipeError, OSError):
                # the API went away; nothing left to report to
                pass

    def log(self, level: str, message: str) -> None:
        self.emit("log", level=level, message=str(message)[:4000])


class CommandReader(threading.Thread):
    """Reads stdin lines; dispatches approve/cancel; EOF counts as cancel."""

    def __init__(self, stream: IO[str], on_approve: Callable[[str, str], None],
                 on_cancel: Callable[[], None]):
        super().__init__(name="commands", daemon=True)
        self._stream = stream
        self._on_approve = on_approve
        self._on_cancel = on_cancel

    def run(self) -> None:
        try:
            for raw in self._stream:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    cmd = json.loads(raw)
                except ValueError:
                    continue
                kind = cmd.get("c")
                if kind == "approve":
                    self._on_approve(str(cmd.get("request_id") or ""), str(cmd.get("answer") or ""))
                elif kind == "cancel":
                    self._on_cancel()
        except (OSError, ValueError):
            pass
        # EOF: the supervisor is gone or closed our stdin — stop cooperatively
        self._on_cancel()


def take_protocol_stdout() -> IO[str]:
    """Duplicate the real stdout for the protocol and point sys.stdout at
    stderr so stray prints from the engines cannot corrupt the stream."""
    import os
    fd = os.dup(sys.stdout.fileno())
    proto = os.fdopen(fd, "w", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr
    return proto
