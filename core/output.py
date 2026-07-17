"""
Durable, high-throughput output writers.

OutputWriter keeps one file handle open per module and appends NDJSON in large
batches, instead of re-opening the file on every flush (the original behavior).
DlqWriter does the same for the dead-letter queue, so a stream full of
no-match lines no longer triggers a file open() per event.

Each worker process owns its own writers with a unique filename suffix
(e.g. ".w0"), so multiple workers never write to the same file and never
corrupt each other's output.
"""

import os
import re
import time
import logging
from utils import fastjson

logger = logging.getLogger("soc-engine")

# 1 MiB userspace buffer per handle keeps write() syscalls infrequent.
_BUFFER = 1024 * 1024


class OutputWriter:
    _SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")

    def __init__(self, output_dir, suffix="", rotate_mb=0, fsync=False):
        self.output_dir = output_dir
        self.suffix = suffix
        self.rotate_bytes = int(rotate_mb) * 1024 * 1024 if rotate_mb else 0
        self.fsync = bool(fsync)
        self.handles = {}   # module -> binary file object
        self.sizes = {}     # module -> current file size in bytes
        self.last_error = None
        os.makedirs(output_dir, exist_ok=True)

    def _fname(self, module):
        # event.module is normally the source program set by the engine, but a
        # rule can override it — it must never be able to escape output_dir
        # (path separators) or kill the writer (a non-string dict key crashed
        # the whole worker before the engine made event.module replace-only).
        if isinstance(module, list):
            module = module[0] if module else "unknown"
        name = self._SAFE_NAME.sub("_", str(module or "unknown"))
        name = name.lstrip(".")[:80]
        return name or "unknown"

    def _path(self, module):
        return os.path.join(self.output_dir, f"{module}{self.suffix}.json")

    def _handle(self, module):
        h = self.handles.get(module)
        if h is None:
            path = self._path(module)
            h = open(path, "ab", buffering=_BUFFER)
            self.handles[module] = h
            try:
                self.sizes[module] = os.path.getsize(path)
            except OSError:
                self.sizes[module] = 0
        return h

    def write_batch(self, batch):
        if not batch:
            return
        grouped = {}
        for event in batch:
            ev = event.get("event") or {}
            module = self._fname(ev.get("module", "unknown"))
            grouped.setdefault(module, []).append(fastjson.dumps_bytes(event))

        for module, blobs in grouped.items():
            data = b"\n".join(blobs) + b"\n"
            self._handle(module).write(data)
            self.sizes[module] = self.sizes.get(module, 0) + len(data)
            if self.rotate_bytes and self.sizes[module] >= self.rotate_bytes:
                self._rotate(module)

    def _rotate(self, module):
        h = self.handles.get(module)
        if h is not None:
            try:
                h.flush()
                os.fsync(h.fileno())
                h.close()
            except OSError:
                pass
        path = self._path(module)
        try:
            # Keep a single prior generation; downstream agents track by inode.
            os.replace(path, path + ".1")
        except OSError:
            pass
        self.handles.pop(module, None)
        self.sizes[module] = 0

    def flush(self):
        """Flush all handles. Returns True only if EVERY flush succeeded.

        A False return means some events may still sit in a userspace buffer
        (disk full / IO error). The caller must NOT commit Kafka offsets past
        them — losing data silently here is exactly what at-least-once exists
        to prevent.
        """
        ok = True
        for module, h in self.handles.items():
            try:
                h.flush()
                if self.fsync:
                    os.fsync(h.fileno())
            except OSError as e:
                ok = False
                self.last_error = f"{module}: {e}"
        return ok

    def close(self):
        for h in self.handles.values():
            try:
                h.flush()
                h.close()
            except OSError:
                pass
        self.handles.clear()
        self.sizes.clear()


class DlqWriter:
    """Per-source dead-letter files under <log_dir>/dlq/.

    Each source program gets its own file (dlq/nginx.w0.json,
    dlq/postfix.w0.json, ...) so operators can inspect one broken source
    without grepping a mixed stream. Files are size-capped like OutputWriter
    (one .1 prior generation kept) so a misconfigured source can never fill
    the disk, and a rate-limited "DLQ storm" warning fires when one source
    dead-letters heavily — that's the signal a rule or program_mapping broke.
    """

    # A storm = this many dead-letters from ONE source within one window.
    STORM_THRESHOLD = 5000
    STORM_WINDOW_SEC = 60

    _SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")

    def __init__(self, log_dir, suffix="", rotate_mb=200):
        self.dir = os.path.join(log_dir, "dlq")
        os.makedirs(self.dir, exist_ok=True)
        self.suffix = suffix
        self.rotate_bytes = int(rotate_mb) * 1024 * 1024 if rotate_mb else 0
        self.handles = {}   # program -> binary file object
        self.sizes = {}     # program -> current size in bytes
        self.last_error = None
        # Entries whose write itself failed (open()/write() error). They are
        # retried on every flush(); while any remain, flush() returns False
        # so the worker never commits offsets past a lost dead-letter.
        self._retry = []
        self._win_start = time.time()
        self._win_counts = {}

    def _fname(self, program):
        # program comes from log metadata — sanitize it before it becomes a
        # filename, and cap the length. Leading dots are stripped: dotfiles
        # are invisible to glob-based tooling (incl. the Web UI DLQ view).
        name = self._SAFE_NAME.sub("_", str(program or "unknown"))
        name = name.lstrip(".")[:80]
        return name or "unknown"

    def _path(self, program):
        return os.path.join(self.dir, f"{self._fname(program)}{self.suffix}.json")

    def _handle(self, program):
        key = self._fname(program)
        h = self.handles.get(key)
        if h is None:
            path = self._path(program)
            h = open(path, "ab", buffering=_BUFFER)
            self.handles[key] = h
            try:
                self.sizes[key] = os.path.getsize(path)
            except OSError:
                self.sizes[key] = 0
        return h, key

    def write(self, entry):
        program = entry.get("program") or "unknown"
        if not self._write_now(entry, program):
            # A dead-letter is part of the durability contract (a DLQ'd
            # message counts as 'handled'): keep the entry and let flush()
            # retry it — never drop it silently (audit A1#6 / A2#10).
            self._retry.append(entry)
        self._storm_check(program)

    def _write_now(self, entry, program):
        try:
            data = fastjson.dumps_bytes(entry) + b"\n"
            h, key = self._handle(program)
            h.write(data)
            self.sizes[key] = self.sizes.get(key, 0) + len(data)
            if self.rotate_bytes and self.sizes[key] >= self.rotate_bytes:
                self._rotate(program, key)
            return True
        except Exception as e:
            self.last_error = f"{program}: {e}"
            return False

    def _rotate(self, program, key):
        h = self.handles.get(key)
        if h is not None:
            try:
                h.flush()
                h.close()
            except OSError:
                pass
        path = self._path(program)
        try:
            # Keep one prior generation, same policy as OutputWriter.
            os.replace(path, path + ".1")
        except OSError:
            pass
        self.handles.pop(key, None)
        self.sizes[key] = 0

    def _storm_check(self, program):
        now = time.time()
        if now - self._win_start >= self.STORM_WINDOW_SEC:
            self._win_start = now
            self._win_counts = {}
        key = self._fname(program)
        c = self._win_counts.get(key, 0) + 1
        self._win_counts[key] = c
        if c == self.STORM_THRESHOLD:  # fires once per window per source
            logger.warning(
                "DLQ STORM: %d '%s' logs dead-lettered in the last %ds - "
                "its rule or program_mapping is likely broken (see %s)",
                c, key, self.STORM_WINDOW_SEC, self._path(program),
            )

    def flush(self):
        """Returns True on success. DLQ entries are part of the durability
        contract too (a DLQ'd message counts as 'handled'), so a failed DLQ
        WRITE or flush must also block the offset commit."""
        # First, retry entries whose write itself failed (e.g. open() error:
        # no handle exists, so handle-flushing alone would "succeed" and the
        # entry would be lost silently).
        if self._retry:
            still = []
            for entry in self._retry:
                if not self._write_now(entry, entry.get("program") or "unknown"):
                    still.append(entry)
            self._retry = still
        ok = not self._retry
        for key, h in self.handles.items():
            try:
                h.flush()
            except OSError as e:
                ok = False
                self.last_error = f"{key}: {e}"
        return ok

    def close(self):
        # best effort on remaining retry entries before handles go away
        for entry in self._retry:
            self._write_now(entry, entry.get("program") or "unknown")
        self._retry = []
        for h in self.handles.values():
            try:
                h.flush()
                h.close()
            except OSError:
                pass
        self.handles.clear()
        self.sizes.clear()
