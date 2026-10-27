"""The run ledger: one JSON line per command in runs/ledger.jsonl (SPEC §3.6).

The README's metrics are computed from this file, so every command records
what it did, how long it took and whether it worked.

Set KIT_BATCH (e.g. KIT_BATCH=readme-2026-10-27) to label a measured run, so
`kit metrics` can report on that batch alone, apart from day-to-day use.
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

LEDGER_PATH = Path("runs/ledger.jsonl")


def record(
    command: str,
    story: str | None,
    started: float,
    ok: bool,
    counts: dict | None = None,
    error: str | None = None,
) -> None:
    """Append one line. `started` is a time.monotonic() value from when the command began."""
    entry = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "command": command,
        "story": story,
        "duration_s": round(time.monotonic() - started, 2),
        "ok": ok,
        "counts": counts or {},
    }
    if error:
        entry["error"] = error
    if os.environ.get("KIT_BATCH"):
        entry["batch"] = os.environ["KIT_BATCH"]
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER_PATH.open("a") as ledger:
        ledger.write(json.dumps(entry) + "\n")


def read_entries(path: Path = LEDGER_PATH) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
