"""Measured run for the README metrics: n rounds per story of reset, seed, check.

    KIT_BATCH=readme-2026-10-27 uv run python scripts/measure.py --runs 10 healthcare finserv manufacturing
    uv run kit metrics

Every command writes to runs/ledger.jsonl with the KIT_BATCH label, and
`kit metrics` turns that batch into the README table. Between seed and check
the script waits for HubSpot search to catch up; that wait is not part of
either measured time.
"""

import argparse
import os
import sys
import time

from dotenv import load_dotenv

from kit import actions

SEARCH_SETTLE_SECONDS = 15


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("stories", nargs="+")
    parser.add_argument("--runs", type=int, default=10)
    args = parser.parse_args()
    load_dotenv(".env")
    if not os.environ.get("KIT_BATCH"):
        sys.exit("Set KIT_BATCH to label this run, e.g. KIT_BATCH=readme-2026-10-27")

    for run in range(1, args.runs + 1):
        for story in args.stories:
            print(f"[run {run}/{args.runs}] {story}: reset", flush=True)
            actions.reset(story)
            print(f"[run {run}/{args.runs}] {story}: seed", flush=True)
            actions.seed(story)
            time.sleep(SEARCH_SETTLE_SECONDS)
            outcome = actions.check(story)
            print(f"[run {run}/{args.runs}] {story}: check {'PASS' if outcome.passed else 'FAIL'}"
                  f" ({sum(r.passed for r in outcome.results)}/{len(outcome.results)})", flush=True)


if __name__ == "__main__":
    main()
