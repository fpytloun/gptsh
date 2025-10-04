from __future__ import annotations

from typing import Optional

from rich.progress import Progress, SpinnerColumn, TextColumn

from gptsh.interfaces import ProgressReporter


class RichProgressReporter(ProgressReporter):
    def __init__(self):
        self._progress: Optional[Progress] = None

    def start(self) -> None:
        if self._progress is None:
            self._progress = Progress(SpinnerColumn(), TextColumn("{task.description}"))
            self._progress.start()

    def stop(self) -> None:
        if self._progress is not None:
            self._progress.stop()
            self._progress = None

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
        # Rich Progress auto-handles redraws; nothing required here for now
        pass

    def resume(self) -> None:
        # Rich Progress auto-handles redraws; nothing required here for now
        pass

