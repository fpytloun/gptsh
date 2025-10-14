from __future__ import annotations

from typing import Optional

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from gptsh.interfaces import ProgressReporter


class RichProgressReporter(ProgressReporter):
    def __init__(self, console: Optional[Console] = None, transient: bool = True):
        self._progress: Optional[Progress] = None
        self._paused: bool = False
        self._transient: bool = transient or False
        self.console: Console = console or Console(stderr=True, soft_wrap=True)

    # Context manager support to ensure progress lifecycle is managed safely
    def __enter__(self) -> "RichProgressReporter":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        # Always stop the progress on exit; do not suppress exceptions
        self.stop()
        return False

    def start(self, transient: Optional[bool] = False) -> None:
        if self._progress is None:
            # Render progress to stderr. Spinner green; text gray for subtlety.
            self._progress = Progress(
                SpinnerColumn(style="green"),
                TextColumn("{task.description}", style="grey50"),
                console=self.console,
                transient=self._transient,
            )
            self._progress.start()

    def stop(self) -> None:
        if self._progress is not None:
            self._progress.stop()
            self._progress = None
        self._paused = False

    def add_task(self, description: str) -> Optional[int]:
        if self._progress is None:
            return None
        return int(self._progress.add_task(description, total=None))

    def complete_task(self, task_id: Optional[int], description: Optional[str] = None) -> None:
        if self._progress is None or task_id is None:
            return
        if description is not None:
            self._progress.update(task_id, description=description)
        self._progress.update(task_id, completed=True)

    def pause(self) -> None:
        # Temporarily stop live rendering to allow interactive prompts on stdout
        if self._progress is not None and not self._paused:
            try:
                self._progress.stop()
            finally:
                self._paused = True

    def resume(self) -> None:
        # Resume live rendering if previously paused
        if self._progress is not None and self._paused:
            try:
                self._progress.start()
            finally:
                self._paused = False
