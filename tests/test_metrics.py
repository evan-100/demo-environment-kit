"""Tests for the README metrics computed from ledger entries."""

from kit.metrics import compute, latest_batch, percentile


def entry(command, story, duration, ok=True, batch="b1", counts=None):
    return {"command": command, "story": story, "duration_s": duration, "ok": ok, "batch": batch, "counts": counts or {}}


def test_percentile_nearest_rank():
    values = [float(v) for v in range(1, 11)]  # 1..10
    assert percentile(values, 50) == 5.0
    assert percentile(values, 95) == 10.0  # with 10 runs, p95 is the slowest
    assert percentile([], 50) is None


def test_compute_uses_only_the_batch_and_successful_runs():
    counts = {"companies": 27, "contacts": 42, "deals": 42, "notes": 2, "links": 187}
    entries = [
        entry("seed", "healthcare", 99.0, batch="old"),  # other batch: ignored
        entry("seed", "healthcare", 12.0, counts=counts),
        entry("seed", "healthcare", 14.0, counts=counts),
        entry("seed", "healthcare", 50.0, ok=False),  # failed seed: counted as a run, not timed
        entry("reset", "healthcare", 6.0),
        entry("check", "healthcare", 9.0, ok=True),
        entry("check", "healthcare", 9.0, ok=False),
    ]
    m = compute(entries, "b1")["healthcare"]
    assert (m["runs"], m["seed_ok"]) == (3, 2)
    assert (m["seed_p50_s"], m["seed_p95_s"], m["reset_p50_s"]) == (12.0, 14.0, 6.0)
    assert (m["checks"], m["checks_passed"]) == (2, 1)
    assert (m["records"], m["links"]) == (113, 187)
    assert latest_batch(entries) == "b1"
