from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from typing import Optional

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from gptsh.interfaces import ProgressReporter


class NoOpProgressReporter(ProgressReporter):
    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self) -> "NoOpProgressReporter":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def start(self, transient: Optional[bool] = False) -> None:
        return

    def stop(self) -> None:
        return

    def add_task(self, description: str) -> Optional[int]:
        return None

    def complete_task(self, task_id: Optional[int], description: Optional[str] = None) -> None:
        return

    def update_task(self, task_id: Optional[int], description: Optional[str] = None) -> None:
        return

    def remove_task(self, task_id: Optional[int]) -> None:
        return

    def pause(self) -> None:
        return

    def resume(self) -> None:
        return

    @contextmanager
    def io(self):
        yield

    @asynccontextmanager
    async def aio_io(self):
        yield


class RichProgressReporter(ProgressReporter):
    def __init__(self, console: Optional[Console] = None, transient: bool = True):
        self._progress: Optional[Progress] = None
        self._paused: bool = False
        self._transient: bool = transient or False
        self.console: Console = console or Console(stderr=True, soft_wrap=True)
        self._io_lock: asyncio.Lock = asyncio.Lock()
        self._io_depth: int = 0
        self._resume_task: Optional[asyncio.Task] = None
        self._resume_delay_s: float = 0.1  # debounce to coalesce rapid IO bursts
        # Debounced per-task helpers
        self._debounced_next: int = 0
        # handle -> {"timer": asyncio.Task, "task_id": Optional[int], "description": str}
        self._debounced: dict[int, dict[str, object]] = {}

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
        # Cancel any pending resume to avoid resurrecting after stop
        try:
            if self._resume_task is not None:
                self._resume_task.cancel()
        except Exception:
            pass
        finally:
            self._resume_task = None
        # Cancel all debounced timers and clear mapping
        try:
            for entry in list(self._debounced.values()):
                timer = entry.get("timer")  # type: ignore[assignment]
                if isinstance(timer, asyncio.Task):
                    timer.cancel()
        except Exception:
            pass
        finally:
            self._debounced.clear()

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

    def update_task(self, task_id: Optional[int], description: Optional[str] = None) -> None:
        """Update an existing task's description without completing it."""
        if self._progress is None or task_id is None:
            return
        if description is not None:
            self._progress.update(task_id, description=description)

    def remove_task(self, task_id: Optional[int]) -> None:
        """Remove a task from the live progress display."""
        if self._progress is None or task_id is None:
            return
        try:
            self._progress.remove_task(task_id)
            # Force a refresh so no blank line remains after removal
            try:
                self._progress.refresh()
            except Exception:
                pass
        except Exception:
            # Be tolerant if task_id was already removed
            pass

    def start_debounced_task(self, description: str, delay: float = 0.15) -> int:
        """Begin a task and return a handle for later completion.

        For simplicity and visibility, create the progress task immediately.
        """
        # Ensure progress is visible if it was paused due to IO debounce
        if self._progress is not None and self._paused:
            try:
                if self._resume_task is not None:
                    self._resume_task.cancel()
            except Exception:
                pass
            finally:
                self._resume_task = None
            try:
                self._progress.start()
            finally:
                self._paused = False
        self._debounced_next += 1
        handle = self._debounced_next
        task_id = self.add_task(description)
        self._debounced[handle] = {"timer": None, "task_id": task_id, "description": description}
        return handle

    def complete_debounced_task(self, handle: int, final_description: Optional[str] = None) -> None:
        """Complete a debounced task created by start_debounced_task.

        Cancels the timer if pending. If a visible task was created, completes it.
        """
        entry = self._debounced.pop(handle, None)
        if not entry:
            return
        # Cancel timer if still active
        timer = entry.get("timer")
        if isinstance(timer, asyncio.Task) and not timer.done():
            try:
                timer.cancel()
            except Exception:
                pass
        # Complete live task if it was created
        task_id = entry.get("task_id")
        if isinstance(task_id, int):
            self.complete_task(task_id, final_description)
            self.remove_task(task_id)

    def pause(self) -> None:
        # Temporarily stop live rendering to allow interactive prompts on stdout
        if self._progress is not None and not self._paused:
            try:
                # Hide the live progress without dropping the instance or tasks
                self._progress.stop()
            finally:
                self._paused = True

    def resume(self) -> None:
        # Resume live rendering if previously paused
        if self._progress is not None and self._paused:
            try:
                # Restart live rendering on the existing progress instance
                self._progress.start()
            finally:
                self._paused = False

    @contextmanager
    def io(self):
        """Synchronous IO guard: pause progress before output and resume after."""
        try:
            self.pause()
            yield
        finally:
            self.resume()

    @asynccontextmanager
    async def aio_io(self):
        """Async IO guard: serialize output and pause progress while printing."""
        async with self._io_lock:
            outermost = (self._io_depth == 0)
            self._io_depth += 1
            try:
                if outermost:
                    # For transient UX, fully stop progress so it disappears during IO
                    self.pause()  # pause() mapped to stop() per current policy
                yield
            finally:
                self._io_depth = max(0, self._io_depth - 1)
                if self._io_depth == 0:
                    # Debounce resume to coalesce adjacent IO sections
                    try:
                        if self._resume_task is not None:
                            self._resume_task.cancel()
                    except Exception:
                        pass
                    self._resume_task = asyncio.create_task(self._delayed_resume())

    async def _delayed_resume(self) -> None:
        try:
            await asyncio.sleep(self._resume_delay_s)
            # Only resume if still idle and progress exists and is paused
            if self._io_depth == 0 and self._progress is not None and self._paused:
                self.resume()
        except asyncio.CancelledError:
            pass
        finally:
            self._resume_task = None
