"""Single-instance enforcement for the desktop app.

Two mechanisms are combined:

1. A **TCP lock** on ``127.0.0.1:<port>``.  Binding a listening socket
   is atomic across processes on every OS we care about, and it doubles
   as an IPC channel: the second launch connects and sends ``SHOW``,
   which makes the first instance raise its window.
2. A **PID file** in the data directory.  This is advisory only — it
   lets ``--status`` style tooling report who owns the lock — but it is
   cleaned up on release so stale files do not accumulate.

Usage::

    lock = SingleInstance(data_dir)
    try:
        lock.acquire(on_activate=window.show)
    except AlreadyRunning:
        lock.signal_existing()   # ask the running app to come forward
        raise SystemExit(0)
    ...
    lock.release()
"""

from __future__ import annotations

import os
import sys
import socket
import threading
from pathlib import Path
from typing import Callable

from ..core.errors import AssistantError
from ..core.logging_setup import get_logger


logger = get_logger("desktop.lock")


#: Default port for the single-instance lock.  Deliberately distinct from
#: the Bridge (7823) and the web UI (7824) ports.
DEFAULT_LOCK_PORT = 7825

#: Message the second instance sends to wake the first one.
ACTIVATE_MESSAGE = b"SHOW\n"


class AlreadyRunning(AssistantError):
    """Another instance of the desktop app already holds the lock."""


class SingleInstance:
    """A cross-platform single-instance lock with an activation channel."""

    def __init__(self, data_dir: Path | str, *, port: int | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.port = int(port or os.environ.get("LOCAL_AGENT_LOCK_PORT", DEFAULT_LOCK_PORT))
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._on_activate: Callable[[], None] | None = None

    # ------------------------------------------------------------ paths

    @property
    def pid_path(self) -> Path:
        return self.data_dir / "desktop.pid"

    # ---------------------------------------------------------- lifecycle

    def acquire(self, on_activate: Callable[[], None] | None = None) -> None:
        """Take the lock, or raise :class:`AlreadyRunning`.

        ``on_activate`` is invoked (on a background thread) whenever a
        second launch asks this instance to come to the foreground.
        """
        if self._socket is not None:
            raise AssistantError("single-instance lock already acquired")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            # On Windows SO_REUSEADDR would let a *second* process steal the
            # port, defeating the whole point; SO_EXCLUSIVEADDRUSE is the
            # correct flag there.  On POSIX, SO_REUSEADDR only skips the
            # TIME_WAIT delay — a live listener still wins the bind — so it
            # makes restarts instant without weakening the lock.
            if sys.platform == "win32":
                exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
                if exclusive is not None:
                    sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
            else:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", self.port))
            sock.listen(4)
            # A short accept timeout lets the server thread notice ``release``
            # promptly.  Without it the blocking accept() would keep the file
            # descriptor (and therefore the port) alive after close().
            sock.settimeout(0.25)
        except OSError as exc:
            sock.close()
            raise AlreadyRunning(
                f"another instance is already running (port {self.port} is taken)"
            ) from exc

        self._socket = sock
        self._on_activate = on_activate
        self._write_pid()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._serve, name="desktop-single-instance", daemon=True
        )
        self._thread.start()
        logger.info("single-instance lock acquired on port %s", self.port)

    def release(self) -> None:
        """Release the lock and clean up the PID file."""
        self._stop.set()
        sock, self._socket = self._socket, None
        # Join first so the accept loop stops touching the descriptor, then
        # close it: closing a socket another thread is blocked in accept() on
        # does not actually free the port on Linux.
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        try:
            if self.pid_path.is_file():
                self.pid_path.unlink()
        except OSError:
            pass

    @property
    def held(self) -> bool:
        return self._socket is not None

    # -------------------------------------------------------- activation

    def signal_existing(self, *, timeout: float = 2.0) -> bool:
        """Ask the already-running instance to show its window.

        Returns ``True`` when the message was delivered.
        """
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=timeout) as client:
                client.sendall(ACTIVATE_MESSAGE)
            logger.info("asked the running instance to show its window")
            return True
        except OSError as exc:
            logger.warning("could not signal the running instance: %s", exc)
            return False

    # ------------------------------------------------------------ server

    def _serve(self) -> None:
        sock = self._socket
        assert sock is not None
        while not self._stop.is_set():
            try:
                conn, _ = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                try:
                    conn.settimeout(1.0)
                    data = conn.recv(64)
                except OSError:
                    data = b""
            if data.strip() == ACTIVATE_MESSAGE.strip() and self._on_activate is not None:
                try:
                    self._on_activate()
                except Exception:  # noqa: BLE001
                    logger.exception("activation callback failed")

    # --------------------------------------------------------------- pid

    def _write_pid(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.pid_path.write_text(str(os.getpid()), encoding="utf-8")
        except OSError as exc:
            logger.warning("could not write pid file: %s", exc)

    def read_pid(self) -> int | None:
        """Return the PID recorded in the lock file, if any."""
        try:
            return int(self.pid_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    # ------------------------------------------------------ context mgr

    def __enter__(self) -> "SingleInstance":
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()
