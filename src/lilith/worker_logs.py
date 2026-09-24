"""Continuously bounded worker output, drained without blocking the worker."""
import threading

from lilith.redaction import redact_text

LIMIT = 1048576
LINE_LIMIT = 16384


class WorkerLog:
    def __init__(self, path, stream):
        self.path, self.stream = path, stream
        self.lock = threading.Lock()
        self.error = None
        self.size = 0
        self.truncated = False
        self.path.write_bytes(b"")
        self.thread = threading.Thread(target=self._drain, name="worker-log-reader", daemon=True)
        self.thread.start()

    def append(self, text, *, final=False):
        with self.lock:
            if self.truncated and not final:
                return
            data = redact_text(text).encode("utf-8", errors="replace")
            cap = LIMIT if final else LIMIT - 4096
            remaining = max(0, cap - self.size)
            if len(data) > remaining:
                marker = b"\n[worker output limit reached; further output discarded]\n"
                data = (data[:max(0, remaining - len(marker))] + marker)[:remaining]
                self.truncated = True
            with self.path.open("ab") as target:
                target.write(data)
            self.size += len(data)

    def _drain(self):
        pending = bytearray()
        dropping = False
        try:
            while chunk := self.stream.read(8192):
                for index, part in enumerate(chunk.split(b"\n")):
                    if index:
                        self.append("[oversized worker line omitted]\n" if dropping else
                                    pending.decode("utf-8", errors="replace") + "\n")
                        pending.clear()
                        dropping = False
                    if not dropping:
                        if len(pending) + len(part) > LINE_LIMIT:
                            pending.clear()
                            dropping = True
                        else:
                            pending.extend(part)
            if dropping:
                self.append("[oversized worker line omitted]\n")
            elif pending:
                self.append(pending.decode("utf-8", errors="replace"))
        except (OSError, ValueError) as error:
            self.error = type(error).__name__
        finally:
            self.stream.close()

    def finish(self, summary):
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            # An inherited pipe must not hold up runtime shutdown indefinitely.
            self.stream.close()
            self.thread.join(timeout=1)
        self.append(summary, final=True)
