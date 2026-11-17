"""The reader pool: document, email and image parsing in child processes.

Parsing is CPU-heavy, blocking library code that can briefly use hundreds of
MB, and a Python thread can't be stopped. A child process can: a read that
runs too long or grows too large is killed, a child that dies takes only its
own call with it, and the pool starts a replacement. Every other call, of any
tenant, carries on.

- Children come from the `forkserver` start method (`spawn` where there is
  none). The fork server preloads only the light reader modules; a child
  imports PyMuPDF, MarkItDown, openpyxl or Pillow when its first call needs
  them. The fork server starts with the first read and stops with the last
  idle child, so a host with no reads for `idle_seconds` holds no reader
  processes at all.
- A call sends the function by reference plus small arguments (file paths,
  options), never file bytes, and gets the parsed result back. A result over
  `max_result_mb` is refused in the child.
- Memory: the thread waiting on a child samples its RSS every 200 ms and
  kills it over `max_rss_mb` ("too large to read"); the child is replaced.
  RLIMIT_AS (`memory_mb`, Linux only; macOS doesn't enforce it) is off by
  default: a reader that has loaded PyMuPDF and MarkItDown reserves well over
  1 GB of address space, so a limit tight enough to matter crashes ordinary
  reads. Set it only as a loose backstop.
- A child is recycled after `max_tasks` calls and stopped after
  `idle_seconds` unused, so a heap a large parse left behind is given back. It
  is also recycled after a call that leaves its RSS above RECYCLE_RSS_FRACTION
  of `max_rss_mb`, so the next call doesn't start near the cap.
- Library families (families.py): a child that has loaded PyMuPDF and then
  MarkItDown, or the reverse, is over a 220 MB cap on the imports alone. So a
  call that needs one family never gets an idle child holding the other: that
  child is stopped first and the call gets a fresh one (the pool never runs
  more than `size` children). Same family or a light call (spreadsheets,
  images, email) reuses a child as before. Children report what they loaded.
- Fairness is applied only at admission; a running read is never stopped for
  another tenant. A free slot goes to the waiting tenant holding the fewest
  slots, oldest call first. While another tenant is waiting, a tenant holding
  its share (ceil(slots / active tenants)) or more can't start a new read. A
  tenant alone may use every slot.

Callers use `limiter.capped_read`, which adds the logging.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import math
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
# Imported once by the fork server: the reader modules, not the libraries they
# load lazily (those would hold ~165 MB in the server while no one reads).
PRELOAD = [
    "handler.runtime.documents.pool", "handler.runtime.documents.router",
    "handler.runtime.mail.parse",
]
RSS_SAMPLE_SECONDS = 0.2
# Recycle a child whose RSS after a call is above this share of max_rss_mb. Not
# lower: a child that has just read a PDF holds about 160 MB (PyMuPDF), one that
# read a docx about 150 MB (MarkItDown), and those should be reused.
RECYCLE_RSS_FRACTION = 0.75
_CLEAR_REFS = Path("/proc/self/clear_refs")


class ReaderFailed(ReadError):
    """The reader process was stopped or died; the message is meant for the agent."""

    def __init__(self, message: str, outcome: str):
        super().__init__(message)
        self.outcome = outcome


@dataclass(frozen=True)
class PoolSettings:
    size: int = 1
    timeout_seconds: float = 120.0
    max_rss_mb: int = 220  # a child over this RSS is killed; 0 turns it off
    memory_mb: int = 0  # RLIMIT_AS per child (Linux only), off: see multi-instance.md
    max_tasks: int = 50
    max_result_mb: int = 64
    idle_seconds: float = 60.0


@dataclass
class Outcome:
    """What one call did. `value()` returns the result or raises its error."""

    status: str  # ok | error | timeout | crashed | memory | rss | too_large | cancelled
    result: Any = None
    memory: dict[str, Any] = field(default_factory=dict)
    queued_ms: int = 0
    duration_ms: int = 0
    active: int = 0
    exit_code: int | None = None
    replaced: str | None = None  # the idle child stopped before this call: "family"
    recycled: str | None = None  # why this call's child was then stopped: tasks | rss

    def value(self, name: str, timeout: float, max_rss_mb: int = 0) -> Any:
        if self.status == "ok":
            return self.result
        if self.status == "error":
            raise self.result
        message = {
            "timeout": f"reading {name} took longer than {timeout:g}s and was stopped",
            "memory": f"reading {name} needed more memory than a reader may use, and was "
                      "stopped",
            "rss": f"{name} is too large to read: the reader used more than "
                   f"{max_rss_mb} MB of memory and was stopped",
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
    from . import cache, families

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
                  "reader_pid": os.getpid(), "families": families.loaded()}
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


def _rss_mb(pid: int) -> float | None:
    """A process's resident set in MB: /proc on Linux, libproc on macOS."""
    try:
        with open(f"/proc/{pid}/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1048576
    except (OSError, ValueError, IndexError):
        pass
    if sys.platform == "darwin":
        return _darwin_rss_mb(pid)
    return None


_libproc: Any = None


def _darwin_rss_mb(pid: int) -> float | None:
    global _libproc
    try:
        if _libproc is None:
            _libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        info = ctypes.create_string_buffer(96)  # struct proc_taskinfo
        size = _libproc.proc_pidinfo(pid, 4, ctypes.c_uint64(0), info, 96)  # PROC_PIDTASKINFO
        if size < 16:
            return None
        return int.from_bytes(info.raw[8:16], "little") / 1048576  # pti_resident_size
    except OSError:
        return None


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
        self.peak_rss_mb: float | None = None
        self.families: set[str] = set()  # heavy libraries it has loaded (families.py)

    def suits(self, family: str | None) -> bool:
        """Running a call that needs `family` won't add a second heavy library."""
        return family is None or self.families <= {family}

    def call(self, request: bytes, timeout: float, max_rss_mb: int = 0) -> tuple:
        """Send one call and wait, sampling RSS; kill on timeout or over max_rss_mb."""
        deadline = time.monotonic() + timeout
        try:
            self.conn.send_bytes(request)
            while True:
                left = deadline - time.monotonic()
                if self.conn.poll(max(0.0, min(RSS_SAMPLE_SECONDS, left))):
                    break
                if max_rss_mb and (rss := _rss_mb(self.process.pid)) is not None \
                        and rss > max_rss_mb:
                    self.peak_rss_mb = rss
                    self.kill("rss")
                    raise _Stopped("rss")
                if left <= 0:
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
    cancelled: bool = False
    family: str | None = None  # the heavy library this call needs (families.py)
    retire: _Worker | None = None  # an idle child holding another family, stopped first


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
        self._server_lock = threading.Lock()
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
        return sum(1 for c in self._running if c.tenant == tenant)

    def _share(self) -> int:
        """ceil(slots / tenants reading or waiting)."""
        tenants = {c.tenant for c in self._running} | {c.tenant for c in self._waiting}
        return max(1, math.ceil(self.settings.size / max(1, len(tenants))))

    def _next(self) -> ReaderCall:
        """The waiting call to start: fewest slots held, oldest first; a tenant at
        its share waits while another tenant does. Never leaves a slot idle."""
        order = {id(c): i for i, c in enumerate(self._waiting)}
        ranked = sorted(self._waiting, key=lambda c: (self._holding(c.tenant), c.queued_at,
                                                      order[id(c)]))
        share = self._share()
        tenants = {c.tenant for c in self._waiting}
        for call in ranked:
            if len(tenants) == 1 or self._holding(call.tenant) < share:
                return call
        return ranked[0]

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
            call = self._next()
            self._waiting.remove(call)
            call.running, call.started_at = True, time.monotonic()
            self._assign_idle(call)
            self._running.append(call)
            try:
                call.loop.call_soon_threadsafe(_resolve, call.granted)
            except RuntimeError:  # the caller's loop is gone: give the slot back
                self._running.remove(call)
                call.running = False
                self._return_idle(call)

    def _assign_idle(self, call: ReaderCall) -> None:
        """An idle child that suits the call's family (one already holding it
        first), else none; with no suitable one, an idle child holding another
        family is taken to be stopped, so the fresh one doesn't add a process."""
        call.worker = call.retire = None
        if not self._idle:
            return
        suitable = [w for w in self._idle if w.suits(call.family)]
        if suitable:
            worker = max(suitable, key=lambda w: (call.family in w.families, w.idle_since))
            self._idle.remove(worker)
            call.worker = worker
        else:
            call.retire = self._idle.pop()

    def _return_idle(self, call: ReaderCall) -> None:
        for worker in (call.worker, call.retire):
            if worker is not None:
                self._idle.append(worker)
        call.worker = call.retire = None

    # ── calls ──

    async def run(self, call: ReaderCall, function: Callable[..., Any], args: tuple,
                  kwargs: dict, *, timeout: float | None = None,
                  family: str | None = None) -> Outcome:
        """`family`: the heavy library the call will load (families.needed)."""
        request = pickle.dumps((function, args, kwargs), protocol=pickle.HIGHEST_PROTOCOL)
        call.family = family
        limit = self.settings.timeout_seconds if timeout is None else timeout
        loop = asyncio.get_running_loop()
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
        outcome.queued_ms = round((call.started_at - call.queued_at) * 1000)
        return outcome

    async def _acquire(self, call: ReaderCall, loop: Any) -> bool:
        call.loop, call.granted = loop, loop.create_future()
        with self._lock:
            if self._closed:
                return False
            self._waiting.append(call)
            self._grant()
        try:
            return await call.granted
        except asyncio.CancelledError:
            with self._lock:
                if call in self._waiting:
                    self._waiting.remove(call)
                elif call in self._running:  # granted as we were cancelled
                    self._running.remove(call)
                    call.running = False
                    self._return_idle(call)
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
        with self._server_lock:  # not while the reaper stops the fork server
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
            with self._lock:
                retire, call.retire = call.retire, None
            if retire is not None:
                retire.close()  # it holds the other library family: stop it first
                outcome.replaced = "family"
            if worker is not None and not worker.process.is_alive():
                worker.close(0)  # died while idle (e.g. the OOM killer)
                worker = None
            if worker is None:
                worker = self._spawn()
            with self._lock:
                call.worker = worker
                if call.cancelled or self._closed:
                    worker.kill("cancelled" if call.cancelled else "closed")
            with self._lock:
                outcome.active = len(self._running)
            status, result, memory = worker.call(request, timeout, self.settings.max_rss_mb)
            worker.tasks += 1
            worker.families.update(memory.pop("families", ()) if isinstance(memory, dict) else ())
            outcome.status, outcome.result, outcome.memory = status, result, memory
            keep = status != "memory"
            if keep and worker.tasks >= self.settings.max_tasks:
                keep, outcome.recycled = False, "tasks"
            after = _rss_mb(worker.process.pid)  # also on macOS, where the child can't tell
            cap = self.settings.max_rss_mb
            if keep and cap and after is not None and after > cap * RECYCLE_RSS_FRACTION:
                keep, outcome.recycled = False, "rss"
        except _Stopped as stopped:
            outcome.status = stopped.reason
            if worker.peak_rss_mb is not None:
                outcome.memory = {"peak_rss_mb": round(worker.peak_rss_mb, 1),
                                  "reader_pid": worker.process.pid}
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
            self._stop_server_if_unused()

    def _stop_server_if_unused(self) -> None:
        """Stop the fork server (and resource tracker) with the last child."""
        with self._server_lock:
            with self._lock:
                if self._idle or self._running or self._waiting:
                    return
            _stop_server()

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
        with self._server_lock:
            _stop_server()
        self._stop_reaper.set()
        if self._reaper is not None:
            self._reaper.join(5)
        if root is not None:
            shutil.rmtree(root, ignore_errors=True)


def _stop_server() -> None:
    """Stop multiprocessing's fork server and resource tracker if they run.

    Both start again on the next read. They use private stdlib methods
    (present in 3.12 and 3.13); if those are missing, they keep running.
    """
    from multiprocessing import forkserver, resource_tracker

    for owner, name in ((getattr(forkserver, "_forkserver", None), "_stop"),
                        (getattr(resource_tracker, "_resource_tracker", None), "_stop")):
        stop = getattr(owner, name, None)
        if stop is None:
            continue
        try:
            stop()
        except Exception:  # noqa: BLE001 - best effort; it just stays up
            logger.warning("could not stop a reader helper process", exc_info=True)


def _resolve(future: Any, value: bool = True) -> None:
    if not future.done():
        future.set_result(value)
