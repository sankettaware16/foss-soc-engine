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
from utils import fastjson

# 1 MiB userspace buffer per handle keeps write() syscalls infrequent.
_BUFFER = 1024 * 1024


class OutputWriter:
    def __init__(self, output_dir, suffix="", rotate_mb=0, fsync=False):
        self.output_dir = output_dir
        self.suffix = suffix
        self.rotate_bytes = int(rotate_mb) * 1024 * 1024 if rotate_mb else 0
        self.fsync = bool(fsync)
        self.handles = {}   # module -> binary file object
        self.sizes = {}     # module -> current file size in bytes
        os.makedirs(output_dir, exist_ok=True)

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
            module = ev.get("module", "unknown")
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
        for h in self.handles.values():
            try:
                h.flush()
                if self.fsync:
                    os.fsync(h.fileno())
            except OSError:
                pass

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
    def __init__(self, log_dir, suffix=""):
        os.makedirs(log_dir, exist_ok=True)
        self.path = os.path.join(log_dir, f"dlq{suffix}.json")
        self.handle = open(self.path, "ab", buffering=_BUFFER)

    def write(self, entry):
        try:
            self.handle.write(fastjson.dumps_bytes(entry) + b"\n")
        except Exception:
            pass

    def flush(self):
        try:
            self.handle.flush()
        except OSError:
            pass

    def close(self):
        try:
            self.handle.flush()
            self.handle.close()
        except OSError:
            pass
