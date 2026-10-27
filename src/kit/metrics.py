"""`kit metrics`: the README numbers, computed from the run ledger.

Reports one batch of runs (see ledger.py, KIT_BATCH) per story:
- seed time p50 / p95 and reset time p50 (successful runs only),
- check pass rate: checks where every golden path passed,
- records per story (what one seed writes).
Numbers are reported as measured, never rounded up.
"""

import math
from collections import defaultdict

from kit.ledger import read_entries


def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile: the smallest value with at least p% of values at or below it.

    With 10 runs, p95 is the slowest run.
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


def latest_batch(entries: list[dict]) -> str | None:
    batches = [e["batch"] for e in entries if e.get("batch")]
    return batches[-1] if batches else None


def compute(entries: list[dict], batch: str) -> dict:
    """Per-story metrics for one batch."""
    by_story = defaultdict(lambda: defaultdict(list))
    for entry in entries:
        if entry.get("batch") == batch:
            by_story[entry["story"]][entry["command"]].append(entry)

    stories = {}
    for story, commands in sorted(by_story.items()):
        seeds = [e for e in commands["seed"] if e["ok"]]
        resets = [e for e in commands["reset"] if e["ok"]]
        checks = commands["check"]
        records = {k: v for k, v in seeds[-1]["counts"].items() if k != "links"} if seeds else {}
        stories[story] = {
            "runs": len(commands["seed"]),
            "seed_ok": len(seeds),
            "seed_p50_s": percentile([e["duration_s"] for e in seeds], 50),
            "seed_p95_s": percentile([e["duration_s"] for e in seeds], 95),
            "reset_p50_s": percentile([e["duration_s"] for e in resets], 50),
            "checks": len(checks),
            "checks_passed": sum(e["ok"] for e in checks),
            "records": sum(records.values()),
            "links": seeds[-1]["counts"].get("links") if seeds else None,
        }
    return stories


def markdown_table(batch: str, stories: dict) -> str:
    lines = [
        f"Batch `{batch}`:",
        "",
        "| Story | Runs | Records (+links) | Seed p50 | Seed p95 | Reset p50 | Checks passing on fresh seed |",
        "|---|---|---|---|---|---|---|",
    ]
    for story, m in stories.items():
        rate = f"{m['checks_passed']}/{m['checks']}" + (f" ({100 * m['checks_passed'] / m['checks']:.0f}%)" if m["checks"] else "")
        lines.append(
            f"| {story} | {m['runs']} | {m['records']} (+{m['links']}) | {m['seed_p50_s']} s | {m['seed_p95_s']} s "
            f"| {m['reset_p50_s']} s | {rate} |"
        )
    return "\n".join(lines)
