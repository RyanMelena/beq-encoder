"""Every child process of one job: started, tracked, stopped and reaped here.

`popen` is the single way a process starts; helpers that decode or probe take it
as an argument so Cancel reaches their children too (MediaInfo runs in-process: no
child, nothing to stop). `run`, `capture` and `feed` are the three ways a job talks
to a tool, all built on `popen`.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path


class Cancelled(Exception):
    pass


class JobFailed(Exception):
    pass


KILL_GRACE_S = 10.0  # a child that has not stopped this long after SIGTERM gets SIGKILL
REAP_TIMEOUT_S = 30.0


@contextmanager
def every(seconds: float, work: Callable[[], None]) -> Iterator[None]:
    """Run `work` every `seconds` in the background while the body runs."""
    stop = threading.Event()

    def loop() -> None:
        while not stop.wait(seconds):
            work()

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join(timeout=5)


class Processes:
    def __init__(self, log: Callable[[str], None]):
        self._log = log
        self._cancel = threading.Event()
        self.live: list[subprocess.Popen] = []

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def check(self) -> None:
        if self._cancel.is_set():
            raise Cancelled()

    def cancel(self) -> None:
        self._cancel.set()
        self._signal_all(signal.SIGTERM)
        timer = threading.Timer(KILL_GRACE_S, self._signal_all, [signal.SIGKILL])
        timer.daemon = True
        timer.start()

    def _signal_all(self, sig: int) -> None:
        for p in list(self.live):
            self._signal(p, sig)

    @staticmethod
    def _signal(p: subprocess.Popen, sig: int) -> None:
        if p.poll() is None:
            try:
                os.killpg(p.pid, sig)
            except ProcessLookupError:
                pass

    def popen(self, cmd: list[str], env: dict | None = None, **kw) -> subprocess.Popen:
        self.check()
        self._log("$ " + " ".join(_quote(c) for c in cmd))
        p = subprocess.Popen(cmd, env={**os.environ, **(env or {})}, start_new_session=True, **kw)
        self.live.append(p)
        if self._cancel.is_set():  # cancel() walked `live` before this process joined it
            self._signal(p, signal.SIGTERM)
            raise Cancelled()
        return p

    def reap(self) -> None:
        """The job is over: nothing it started outlives it or stays a zombie."""
        for p in self.live:
            self._signal(p, signal.SIGKILL)
            try:
                p.wait(timeout=REAP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                self._log(f"WARNING: process {p.pid} did not exit after SIGKILL")

    def run(self, cmd: list[str], on_line: Callable[[str], None] | None = None,
            env: dict | None = None, ok_codes: tuple[int, ...] = (0,),
            keep: list[str] | None = None, filt: Callable[[str], str] | None = None) -> None:
        """Run a tool to its end, logging its output and handing each line to `on_line`."""
        p = self.popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       stdin=subprocess.DEVNULL, text=True, errors="replace")
        self._pump(p.stdout, on_line, keep, filt)
        rc = p.wait()
        self.check()
        if rc not in ok_codes:
            raise JobFailed(f"{Path(cmd[0]).name} exited with code {rc} (see log)")

    def _pump(self, stream, on_line: Callable[[str], None] | None, keep: list[str] | None,
              filt: Callable[[str], str] | None) -> None:
        """Read a text stream split on CR or LF, logging and dispatching each line."""
        buf = ""
        while True:
            ch = stream.read(1)
            if not ch or ch in "\r\n":
                if buf.strip():
                    shown = filt(buf) if filt else buf
                    if shown:
                        self._log(shown)
                        if keep is not None:
                            keep.append(shown)
                            del keep[:-400]
                    if on_line:
                        on_line(buf)
                buf = ""
                if not ch:
                    break
            else:
                buf += ch

    def capture(self, cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
        """Run a short command to its end and return its output."""
        p = self.popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, text=True, errors="replace")
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._signal(p, signal.SIGKILL)
            p.communicate()
            raise JobFailed(f"{Path(cmd[0]).name} timed out after {timeout:.0f} s") from None
        self.check()
        return subprocess.CompletedProcess(cmd, p.returncode, out, err)

    @contextmanager
    def feed(self, cmd: list[str], what: str) -> Iterator[Callable[[bytes], object]]:
        """Run a tool that reads its stdin and yield the function that writes to it. When
        the tool dies part-way its stderr says why, so the failure carries that (read on a
        thread: a tool that fills the pipe while the job is writing would stall both)."""
        p = self.popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        tail: list[bytes] = []

        def drain() -> None:
            for line in p.stderr:
                tail.append(line)
                del tail[:-40]

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        broken = False
        try:
            yield p.stdin.write
        except BrokenPipeError:
            broken = True
        finally:
            try:
                p.stdin.close()
            except OSError:  # its last flush found the pipe gone
                broken = True
            rc = p.wait()
            reader.join(timeout=5)
        self.check()
        if broken or rc != 0:
            err = b"".join(tail).decode(errors="replace")
            raise JobFailed(f"{what} failed: {err.strip()[-300:]}")


def _quote(s: str) -> str:
    return s if re.fullmatch(r"[\w./:=,+@%-]+", s) else "'" + s.replace("'", "'\\''") + "'"
