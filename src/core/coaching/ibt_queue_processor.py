"""Non-blocking IBT import queue processor.

A single background thread runs discovery + import sequentially.
Concurrent triggers are silently ignored (the lock is not acquired a second time).
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable

_LOG = logging.getLogger(__name__)

# Prevents concurrent import runs.
_import_lock = threading.Lock()


def run_discovery_and_import(
    *,
    on_progress: Callable[[int, int], None] | None = None,
    on_import_done: Callable[[Path], None] | None = None,
    on_all_done: Callable[[], None] | None = None,
) -> bool:
    """Start discovery + pending-queue import in a background thread.

    Returns ``True`` if the thread was started.  Returns ``False`` and logs a
    message if an import is already in progress (the caller should ignore the
    duplicate trigger).

    All callbacks are invoked from the background thread.  Callers that need to
    update a Tkinter widget must schedule via ``widget.after(0, fn)``.

    Parameters
    ----------
    on_progress:
        Called before each import with ``(current_index, total_count)``
        where *current_index* is 1-based.
    on_import_done:
        Called after each successful import with the ``session_dir`` Path.
    on_all_done:
        Called once when all pending entries have been processed (whether they
        succeeded or failed).
    """
    if not _import_lock.acquire(blocking=False):
        _LOG.info("[ibt_queue_processor] already running, ignoring trigger")
        return False

    threading.Thread(
        target=_thread_entry,
        kwargs={
            "on_progress": on_progress,
            "on_import_done": on_import_done,
            "on_all_done": on_all_done,
        },
        name="ibt-queue-processor",
        daemon=True,
    ).start()
    return True


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _thread_entry(
    *,
    on_progress: Callable[[int, int], None] | None,
    on_import_done: Callable[[Path], None] | None,
    on_all_done: Callable[[], None] | None,
) -> None:
    try:
        _process(
            on_progress=on_progress,
            on_import_done=on_import_done,
            on_all_done=on_all_done,
        )
    finally:
        _import_lock.release()


def _process(
    *,
    on_progress: Callable[[int, int], None] | None,
    on_import_done: Callable[[Path], None] | None,
    on_all_done: Callable[[], None] | None,
) -> None:
    from core.coaching.ibt_import_queue import (
        discover_and_queue_new_ibt_files,
        get_pending_queue_entries,
        update_queue_entry_state,
    )
    from core.coaching.ibt_importer import import_ibt_queue_entry

    # Step 1: Discovery (new .ibt files → queue)
    try:
        discover_and_queue_new_ibt_files()
    except Exception as exc:
        _LOG.warning("[ibt_queue_processor] discovery failed: %s", exc)

    # Step 2: Collect all pending entries
    try:
        pending = get_pending_queue_entries()
    except Exception as exc:
        _LOG.warning("[ibt_queue_processor] get_pending_queue_entries failed: %s", exc)
        pending = []

    total = len(pending)
    _LOG.info("[ibt_queue_processor] pending=%d", total)

    for idx, entry in enumerate(pending):
        fingerprint = str(entry.get("fingerprint") or "")
        source_name = str(entry.get("source_name") or fingerprint[:16] or "?")

        _safe_call(on_progress, idx + 1, total)

        try:
            update_queue_entry_state(fingerprint, "importing")
            session_dir = import_ibt_queue_entry(entry)
            update_queue_entry_state(
                fingerprint, "done",
                extra_fields={"session_dir": str(session_dir)},
            )
            _LOG.info(
                "[ibt_queue_processor] imported %s → %s", source_name, session_dir.name
            )
            _safe_call(on_import_done, session_dir)
        except Exception as exc:
            _LOG.warning(
                "[ibt_queue_processor] import failed for %s: %s", source_name, exc
            )
            try:
                update_queue_entry_state(
                    fingerprint, "failed",
                    extra_fields={"error": str(exc)},
                )
            except Exception:
                pass

    _safe_call(on_all_done)


def _safe_call(fn: Callable | None, *args: object) -> None:
    if not callable(fn):
        return
    try:
        fn(*args)
    except Exception as exc:
        _LOG.debug("[ibt_queue_processor] callback error: %s", exc)
