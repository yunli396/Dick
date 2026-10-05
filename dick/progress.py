import sys
import threading


class Progress:
    """A small stderr progress renderer with native-manager-inspired styles."""

    def __init__(self, family, total, stream=None, enabled=True):
        self.family = family
        self.total = max(0, total)
        self.stream = stream or sys.stderr
        self.enabled = enabled and getattr(self.stream, "isatty", lambda: False)()
        self.completed = 0
        self.lock = threading.Lock()

    def start(self):
        if not self.enabled:
            return
        with self.lock:
            if self.family == "arch":
                self.stream.write(":: Synchronizing package databases...\n")
            elif self.family == "debian":
                self.stream.write("Reading package lists...\n")
            elif self.family == "fedora":
                self.stream.write("Refreshing repository metadata...\n")
            else:
                self.stream.write("Refreshing package indexes...\n")
            self.stream.flush()

    def update(self, label, success=True, detail=""):
        with self.lock:
            self.completed += 1
            if not self.enabled:
                return
            ratio = self.completed / self.total if self.total else 1
            width = 28
            filled = min(width, int(width * ratio))
            bar = "#" * filled + "-" * (width - filled)
            status = "done" if success else "failed"
            suffix = f" ({detail})" if detail else ""
            if self.family == "arch":
                line = f" {label:<20} [{bar}] {ratio * 100:3.0f}% {status}{suffix}"
            elif self.family == "debian":
                line = f"Get: {label:<20} [{bar}] {ratio * 100:3.0f}%{suffix}"
            elif self.family == "fedora":
                line = f" {label:<20} {bar} {ratio * 100:3.0f}%{suffix}"
            else:
                line = f" {label:<20} [{bar}] {ratio * 100:3.0f}%{suffix}"
            self.stream.write("\r\033[2K" + line)
            if self.completed >= self.total:
                self.stream.write("\n")
            self.stream.flush()

    def finish(self):
        if self.enabled and self.completed < self.total:
            with self.lock:
                self.stream.write("\n")
                self.stream.flush()
