from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from typing import Optional

from rich.console import Console
from rich.control import Control, ControlType
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Column

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
        self._resume_delay_s: float = 0.05  # debounce to coalesce rapid IO bursts
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
                TextColumn(
                    "{task.description}",
                    style="grey50",
                    table_column=Column(ratio=1, no_wrap=True, overflow="ellipsis"),
                ),
                console=self.console,
                transient=False,
                expand=True,
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
            # Lazily start progress so REPL turns can recreate the spinner
            self.start()
        if self._paused:
            # Ensure rendering is active before adding a task
            self.resume()
        return int(self._progress.add_task(description, total=None))

    def complete_task(self, task_id: Optional[int], description: Optional[str] = None) -> None:
        if self._progress is None or task_id is None:
            return
        if description is not None:
            self._progress.update(task_id, description=description)
        self._progress.update(task_id, completed=True)
        try:
            self._progress.refresh()
        except Exception:
            pass

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

    def _erase_line(self):
        self.console.control(
            Control.move(y=-1),
            Control.move_to_column(0),
            Control((ControlType.ERASE_IN_LINE, 2)),
        )

    def start_debounced_task(self, description: str, delay: float = 0.1) -> int:
        """Begin a task and return a handle for later completion.

        Debounce creation: only materialize a visible progress task if the
        operation lasts longer than `delay`. This prevents flicker and avoids
        interfering with interactive approval prompts.
        """
        self._debounced_next += 1
        handle = self._debounced_next
        # Ensure Progress instance exists (but don't force resume)
        if self._progress is None:
            self.start()

        # If no IO guard is active, ensure we're rendering; if paused, resume now (safe)
        if self._progress is not None and self._io_depth == 0 and self._paused:
            self.resume()

        # If not paused and no IO guard is active, create immediately for quicker feedback
        if self._progress is not None and not self._paused and self._io_depth == 0:
            task_id = self.add_task(description)
            # Force a refresh so the spinner appears promptly
            try:
                self._progress.refresh()
            except Exception:
                pass
            self._debounced[handle] = {"timer": None, "task_id": task_id, "description": description}
        else:
            # Schedule delayed creation of the visible task; do not auto-resume rendering.
            timer = asyncio.create_task(self._delayed_create_task(handle, description, delay))
            self._debounced[handle] = {"timer": timer, "task_id": None, "description": description}
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

    async def _delayed_create_task(self, handle: int, description: str, delay: float) -> None:
        """Create a visible progress task after a delay if still pending.

        This avoids creating tasks for very short operations and reduces prompt garbling.
        """
        try:
            await asyncio.sleep(delay)
            entry = self._debounced.get(handle)
            if entry is None or entry.get("task_id") is not None:
                return
            # Ensure Progress exists but do not force resume while paused
            if self._progress is None:
                self.start()
            if self._progress is None:
                return
            task_id = int(self._progress.add_task(description, total=None))
            entry["task_id"] = task_id
            # Promptly refresh to render the spinner
            try:
                self._progress.refresh()
            except Exception:
                pass
            # If no IO guard is active and we are paused, resume immediately for faster render
            if self._io_depth == 0 and self._paused:
                self.resume()
        except asyncio.CancelledError:
            # Timer cancelled because task finished quickly
            pass

    def pause(self) -> None:
        # Temporarily stop live rendering to allow interactive prompts on stdout
        if self._progress is not None and not self._paused:
            try:
                # Hide the live progress without dropping the instance or tasks
                self._progress.stop()
                if self._transient == True:
                    self._erase_line()
            finally:
                self._paused = True
            try:
                self._progress.refresh()
            except Exception:
                pass

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
                    # Stop rendering and resume after
                    self.pause()
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
