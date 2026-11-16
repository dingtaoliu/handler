"""The reader pool: document, email and image parsing in child processes.

Parsing is CPU-heavy, blocking library code that can briefly use hundreds of
MB, and a Python thread can't be stopped. A child process can: a read that
runs too long is killed, a child that dies takes only its own call with it,
and the pool starts a replacement. Every other call, of any tenant, carries on.

- Children come from the `forkserver` start method (`spawn` where there is
  none). The fork server preloads the reader libraries once, so children
  share those pages and start in milliseconds.
- A call sends the function by reference plus small arguments (file paths,
  options), never file bytes, and gets the parsed result back. A result over
  `max_result_mb` is refused in the child.
- On Linux each child's address space is capped with RLIMIT_AS (macOS doesn't
  enforce it, so there the cap is skipped). Hitting it raises MemoryError in
  the child, or kills it; either way the call fails and the child is replaced.
- A child is recycled after `max_tasks` calls and stopped after
  `idle_seconds` unused, so a heap a large parse left behind is given back.
- Fairness: the slots are shared by tenants. A free slot goes to the waiting
  tenant holding the fewest slots (oldest call first). A tenant may use every
  slot while no one else waits; when another tenant is waiting with less than
  its share (slots / contending tenants), the newest call of a tenant over its
  share is stopped and queued again, so one tenant's hung PDFs cannot hold
  every slot. A stopped call loses only its progress; reads are idempotent.

Callers use `limiter.capped_read`, which adds the logging.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import os
import pickle
import shutil
import signal
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from multiprocessing import spawn as mp_spawn
from pathlib import Path
from typing import Any, Callable

from ..observability.memory import process_memory
from .types import ReadError

logger = logging.getLogger("handler.runtime.documents")

PROCESS_NAME = "handler-reader"
# Imported once by the fork server; children share these pages.
PRELOAD = [
    "handler.runtime.documents.pool", "handler.runtime.documents.router",
    "handler.runtime.documents.conversion", "handler.runtime.images",
    "handler.runtime.mail.parse", "fitz", "PIL.Image", "openpyxl", "xlrd", "markitdown",
]
_CLEAR_REFS = Path("/proc/self/clear_refs")


class ReaderFailed(ReadError):
    """The reader process was stopped or died; the message is meant for the agent."""

    def __init__(self, message: str, outcome: str):
        super().__init__(message)
        self.outcome = outcome


@dataclass(frozen=True)
class PoolSettings:
    size: int = 2
    timeout_seconds: float = 120.0
    memory_mb: int = 1536  # RLIMIT_AS per child, Linux only; 0 turns it off
    max_tasks: int = 50
    max_result_mb: int = 64
    idle_seconds: float = 60.0


@dataclass
class Outcome:
    """What one call did. `value()` returns the result or raises its error."""

    status: str  # ok | error | timeout | crashed | memory | too_large | cancelled
    result: Any = None
    memory: dict[str, Any] = field(default_factory=dict)
    queued_ms: int = 0
    duration_ms: int = 0
    active: int = 0
    exit_code: int | None = None

    def value(self, name: str, timeout: float) -> Any:
        if self.status == "ok":
            return self.result
        if self.status == "error":
            raise self.result
        message = {
            "timeout": f"reading {name} took longer than {timeout:g}s and was stopped",
            "memory": f"reading {name} needed more memory than a reader may use, and was "
                      "stopped",
            "too_large": f"the result of reading {name} is too large to return",
            "crashed": f"the reader stopped unexpectedly while reading {name}"
                       + (f" (signal {-self.exit_code})" if self.exit_code
                          and self.exit_code < 0 else "")
                       + "; the file may be damaged or too large",
            "closed": "the file reader is shutting down; try again",
        }.get(self.status, f"reading {name} failed ({self.status})")
        raise ReaderFailed(message, self.status)


# ── the child ──────────────────────────────────────────────────────────────


def _limit_memory(memory_mb: int) -> None:
    """RLIMIT_AS on Linux. macOS accepts some values but doesn't enforce them."""
    if not memory_mb or sys.platform != "linux":
        return
    import resource

    try:
        _, hard = resource.getrlimit(resource.RLIMIT_AS)
        limit = memory_mb * 1024 * 1024
        if hard != resource.RLIM_INFINITY:
            limit = min(limit, hard)
        resource.setrlimit(resource.RLIMIT_AS, (limit, hard))
    except (ValueError, OSError):
        logger.warning("could not limit reader memory", exc_info=True)


def _reset_peak() -> None:
    """Restart VmHWM, so each call reports its own peak (Linux 4.0+)."""
    try:
        _CLEAR_REFS.write_text("5")
    except OSError:
        pass


def _out_of_memory(exc: Exception) -> bool:
    """MuPDF reports a refused allocation as its own error, not MemoryError."""
    text = str(exc)
    return "malloc" in text and "failed" in text


def _dump(reply: tuple, max_bytes: int) -> bytes:
    try:
        data = pickle.dumps(reply, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception as exc:  # noqa: BLE001 - e.g. an exception that won't pickle
        status, result, memory = reply
        failure = result if isinstance(result, BaseException) else exc
        data = pickle.dumps(("error", RuntimeError(f"{type(failure).__name__}: {failure}"),
                             memory))
    if len(data) > max_bytes:
        data = pickle.dumps(("too_large", len(data), reply[2]))
    return data


def _child_main(conn: Any, memory_mb: int, max_result_bytes: int, cache_dir: str | None) -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # the parent decides when we stop
    _limit_memory(memory_mb)
    from ..observability.logging import configure_json_logging
    from . import cache

    configure_json_logging("WARNING")
    if cache_dir:
        cache.configure_dir(Path(cache_dir))
    while True:
        try:
            request = conn.recv_bytes()
        except (EOFError, OSError):
            return
        _reset_peak()
        before = process_memory()
        stop = False
        try:
            function, args, kwargs = pickle.loads(request)
            status, result = "ok", function(*args, **kwargs)
        except MemoryError:
            status, result, stop = "memory", None, True
        except Exception as exc:  # noqa: BLE001 - every error goes back to the caller
            if _out_of_memory(exc):
                status, result, stop = "memory", None, True
            else:
                if not isinstance(exc, ReadError):
                    logger.warning("reader call failed", exc_info=True)
                status, result = "error", exc
        after = process_memory()
        memory = {"rss_mb_before": before["rss_mb"], "rss_mb_after": after["rss_mb"],
                  "peak_rss_mb": after["peak_rss_mb"], "swap_mb": after["swap_mb"],
                  "reader_pid": os.getpid()}
        reply = _dump((status, result, memory), max_result_bytes)
        result = None
        try:
            conn.send_bytes(reply)
        except (OSError, ValueError):
            return
        if stop:
            return  # a MemoryError can leave a library half-initialised


# ── the parent ─────────────────────────────────────────────────────────────

_prep_installed = False


def _install_prep_filter() -> None:
    """Keep children from re-running the parent's __main__ script.

    A child's preparation data names the parent's main module, which the child
    then imports: for `handler-rt` that is the whole server. Readers need none
    of it, so for processes named PROCESS_NAME the entries are dropped.
    """
    global _prep_installed
    if _prep_installed:
        return
    original = mp_spawn.get_preparation_data

    def preparation_data(name: str) -> dict:
        data = original(name)
        if name.startswith(PROCESS_NAME):
            data.pop("init_main_from_path", None)
            data.pop("init_main_from_name", None)
        return data

    mp_spawn.get_preparation_data = preparation_data
    _prep_installed = True


def _context() -> Any:
    _install_prep_filter()
    if "forkserver" in multiprocessing.get_all_start_methods():
        context = multiprocessing.get_context("forkserver")
        context.set_forkserver_preload(PRELOAD)
        return context
    return multiprocessing.get_context("spawn")


class _Stopped(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _Worker:
    def __init__(self, context: Any, settings: PoolSettings, cache_dir: str | None):
        parent, child = context.Pipe(duplex=True)
        self.process = context.Process(
            target=_child_main, name=PROCESS_NAME, daemon=True,
            args=(child, settings.memory_mb, settings.max_result_mb * 1024 * 1024, cache_dir))
        self.process.start()
        child.close()
        self.conn = parent
        self.tasks = 0
        self.idle_since = time.monotonic()
        self.stopped_for: str | None = None

    def call(self, request: bytes, timeout: float) -> tuple:
        try:
            self.conn.send_bytes(request)
            if not self.conn.poll(timeout):
                self.kill("timeout")
                raise _Stopped("timeout")
            data = self.conn.recv_bytes()
        except (EOFError, OSError):
            raise _Stopped(self.stopped_for or "crashed") from None
        try:
            return pickle.loads(data)
        except Exception as exc:  # noqa: BLE001 - e.g. an exception class that won't rebuild
            return ("error", RuntimeError(f"{type(exc).__name__}: {exc}"), {})

    def kill(self, reason: str) -> None:
        if self.stopped_for is None:
            self.stopped_for = reason
        try:
            self.process.kill()
        except (OSError, ValueError, AttributeError):
            pass

    def close(self, timeout: float = 2.0) -> int | None:
        """Stop the child (an EOF ends its loop) and reap it; returns its exit code."""
        try:
            self.conn.close()
        except OSError:
            pass
        self.process.join(timeout)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(timeout)
        code = self.process.exitcode
        try:
            self.process.close()
        except ValueError:
            pass
        return code


@dataclass(eq=False)
class ReaderCall:
    """One queued or running call; `running` is True while it holds a slot."""

    tenant: str
    queued_at: float = field(default_factory=time.monotonic)
    running: bool = False
    loop: Any = None
    granted: Any = None
    worker: _Worker | None = None
    started_at: float = 0.0
    preempted: bool = False
    cancelled: bool = False


class ReaderPool:
    def __init__(self, settings: PoolSettings | None = None):
        self.settings = settings or PoolSettings()
        self._lock = threading.Lock()
        self._idle: list[_Worker] = []
        self._running: list[ReaderCall] = []
        self._waiting: list[ReaderCall] = []
        self._threads: ThreadPoolExecutor | None = None
        self._context: Any = None
        self._root: Path | None = None
        self._closed = False
        self._reaper: threading.Thread | None = None
        self._stop_reaper = threading.Event()
        self.started = 0  # children started, for tests and logs

    # ── state ──

    @property
    def active(self) -> int:
        with self._lock:
            return len(self._running)

    def holding(self, tenant: str) -> int:
        with self._lock:
            return self._holding(tenant)

    def _holding(self, tenant: str) -> int:
        return sum(1 for c in self._running if c.tenant == tenant and not c.preempted)

    def _share(self) -> int:
        tenants = {c.tenant for c in self._running if not c.preempted}
        tenants |= {c.tenant for c in self._waiting}
        return max(1, self.settings.size // max(1, len(tenants)))

    def scratch_dir(self) -> Path:
        """A private directory for files handed to readers (removed on close)."""
        return self._ensure_root() / "scratch"

    def _ensure_root(self) -> Path:
        with self._lock:
            if self._root is None:
                self._root = Path(tempfile.mkdtemp(prefix="handler-readers-"))
                (self._root / "cache").mkdir()
                (self._root / "scratch").mkdir()
            return self._root

    # ── scheduling (all under self._lock) ──

    def _grant(self) -> None:
        while self._waiting and len(self._running) < self.settings.size:
            order = {id(c): i for i, c in enumerate(self._waiting)}
            call = min(self._waiting, key=lambda c: (self._holding(c.tenant), c.queued_at,
                                                     order[id(c)]))
            self._waiting.remove(call)
            call.running, call.started_at = True, time.monotonic()
            call.worker = self._idle.pop() if self._idle else None
            self._running.append(call)
            try:
                call.loop.call_soon_threadsafe(_resolve, call.granted)
            except RuntimeError:  # the caller's loop is gone: give the slot back
                self._running.remove(call)
                call.running = False
                if call.worker is not None:
                    self._idle.append(call.worker)
                    call.worker = None

    def _preempt(self) -> None:
        if len(self._running) < self.settings.size:
            return
        share = self._share()
        starving = sum(1 for c in self._waiting if self._holding(c.tenant) < share)
        stopping = sum(1 for c in self._running if c.preempted)
        while starving > stopping:
            over = [c for c in self._running
                    if not c.preempted and self._holding(c.tenant) > share]
            if not over:
                return
            victim = max(over, key=lambda c: c.started_at)
            victim.preempted = True
            stopping += 1
            logger.warning("document read stopped for another tenant", extra={
                "tenant": victim.tenant, "share": share, "limit": self.settings.size})
            if victim.worker is not None:
                victim.worker.kill("preempted")

    # ── calls ──

    async def run(self, call: ReaderCall, function: Callable[..., Any], args: tuple,
                  kwargs: dict, *, timeout: float | None = None) -> Outcome:
        request = pickle.dumps((function, args, kwargs), protocol=pickle.HIGHEST_PROTOCOL)
        limit = self.settings.timeout_seconds if timeout is None else timeout
        loop = asyncio.get_running_loop()
        while True:
            if not await self._acquire(call, loop):
                return Outcome("closed")
            work = loop.run_in_executor(self._executor(), self._dispatch, call, request, limit)
            try:
                outcome = await asyncio.shield(work)
            except asyncio.CancelledError:
                with self._lock:
                    call.cancelled = True
                    if call.worker is not None:
                        call.worker.kill("cancelled")
                raise
            if outcome.status != "preempted":
                outcome.queued_ms = round((call.started_at - call.queued_at) * 1000)
                return outcome
            call.preempted = False  # queued again with its original age

    async def _acquire(self, call: ReaderCall, loop: Any) -> bool:
        call.loop, call.granted = loop, loop.create_future()
        with self._lock:
            if self._closed:
                return False
            self._waiting.append(call)
            self._grant()
            self._preempt()
        try:
            return await call.granted
        except asyncio.CancelledError:
            with self._lock:
                if call in self._waiting:
                    self._waiting.remove(call)
                elif call in self._running:  # granted as we were cancelled
                    self._running.remove(call)
                    call.running = False
                    if call.worker is not None:
                        self._idle.append(call.worker)
                    self._grant()
            raise

    def _executor(self) -> ThreadPoolExecutor:
        with self._lock:
            if self._threads is None:
                # One thread per slot waits on its child; queued calls hold none.
                self._threads = ThreadPoolExecutor(self.settings.size,
                                                   thread_name_prefix="document-read")
            return self._threads

    def _spawn(self) -> _Worker:
        root = self._ensure_root()
        with self._lock:
            if self._context is None:
                self._context = _context()
            context = self._context
        worker = _Worker(context, self.settings, str(root / "cache"))
        with self._lock:
            self.started += 1
            self._start_reaper()
        return worker

    def _dispatch(self, call: ReaderCall, request: bytes, timeout: float) -> Outcome:
        """In a pool thread: run the call on a child; always frees the slot."""
        started = time.monotonic()
        worker = call.worker
        keep = False
        outcome = Outcome("crashed")
        try:
            if worker is not None and not worker.process.is_alive():
                worker.close(0)  # died while idle (e.g. the OOM killer)
                worker = None
            if worker is None:
                worker = self._spawn()
            with self._lock:
                call.worker = worker
                if call.preempted or call.cancelled or self._closed:
                    worker.kill("preempted" if call.preempted
                                else "cancelled" if call.cancelled else "closed")
            with self._lock:
                outcome.active = len(self._running)
            status, result, memory = worker.call(request, timeout)
            worker.tasks += 1
            outcome.status, outcome.result, outcome.memory = status, result, memory
            keep = status != "memory" and worker.tasks < self.settings.max_tasks
        except _Stopped as stopped:
            outcome.status = stopped.reason
        except Exception as exc:  # noqa: BLE001 - e.g. the child could not start
            logger.warning("reader process failed", exc_info=True)
            outcome.status, outcome.result = "error", ReadError(
                f"the file reader is unavailable ({type(exc).__name__})")
        finally:
            outcome.duration_ms = round((time.monotonic() - started) * 1000)
            with self._lock:
                self._running.remove(call)
                call.running = False
                if keep and not self._closed:
                    worker.idle_since = time.monotonic()
                    self._idle.append(worker)
                if call.preempted and not call.cancelled:
                    outcome.status = "preempted"
                self._grant()
            if worker is not None and not (keep and not self._closed):
                outcome.exit_code = worker.close()
        return outcome

    # ── lifecycle ──

    def _start_reaper(self) -> None:
        if self._reaper is None or not self._reaper.is_alive():
            self._stop_reaper.clear()
            self._reaper = threading.Thread(target=self._reap, name="reader-reaper",
                                            daemon=True)
            self._reaper.start()

    def _reap(self) -> None:
        every = max(0.05, min(5.0, self.settings.idle_seconds / 2))
        while not self._stop_reaper.wait(every):
            now = time.monotonic()
            with self._lock:
                expired = [w for w in self._idle
                           if now - w.idle_since >= self.settings.idle_seconds]
                for worker in expired:
                    self._idle.remove(worker)
            for worker in expired:
                worker.close()

    def close(self) -> None:
        """Stop every child, fail queued calls and remove the scratch files."""
        with self._lock:
            self._closed = True
            idle, self._idle = self._idle, []
            waiting, self._waiting = self._waiting, []
            for call in self._running:
                if call.worker is not None:
                    call.worker.kill("closed")
            threads, self._threads = self._threads, None
            root, self._root = self._root, None
        for call in waiting:
            try:
                call.loop.call_soon_threadsafe(_resolve, call.granted, False)
            except RuntimeError:
                pass
        for worker in idle:
            worker.close()
        if threads is not None:
            threads.shutdown(wait=True)
        self._stop_reaper.set()
        if self._reaper is not None:
            self._reaper.join(5)
        if root is not None:
            shutil.rmtree(root, ignore_errors=True)


def _resolve(future: Any, value: bool = True) -> None:
    if not future.done():
        future.set_result(value)
