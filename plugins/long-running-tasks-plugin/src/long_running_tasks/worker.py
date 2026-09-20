from __future__ import annotations

import os
import signal
import sys
import threading
from pathlib import Path

from .runner import RunManager
from .store import RunStore, StoreError


def main(argv: list[str] | None = None) -> int:
    args = list(argv or sys.argv[1:])
    if len(args) != 2:
        print("usage: python -m long_running_tasks.worker RUN_ID WORKER_TOKEN", file=sys.stderr)
        return 2
    run_id, token = args
    data_dir = os.environ.get("LONG_RUNNING_TASKS_DATA_DIR")
    manager = RunManager(store=RunStore(Path(data_dir) if data_dir else None), worker_backend="inline")
    cancel_event = threading.Event()
    manager._cancel_events[run_id] = cancel_event

    def request_cancel(_signum: int, _frame: object) -> None:
        cancel_event.set()
        try:
            run = manager.store.get(run_id)
            run["cancel_requested"] = True
            manager.store.write(run)
        except StoreError:
            pass

    signal.signal(signal.SIGTERM, request_cancel)
    signal.signal(signal.SIGINT, request_cancel)
    try:
        manager._worker_entry(run_id)
        return 0
    except StoreError as exc:
        manager.store.append_log(run_id, {"kind": "worker_exit", "message": str(exc)})
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
