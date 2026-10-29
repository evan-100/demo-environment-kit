"""Readiness reports: a terminal table, plus JSON and Markdown files in reports/.

The JSON is for tools (the MCP server reads the latest one). The Markdown is
for people, e.g. pasted into Slack before a call.
"""

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from kit.check import PathResult

REPORTS_DIR = Path("reports")


def summary_line(results: list[PathResult]) -> str:
    passed = sum(r.passed for r in results)
    return f"{passed}/{len(results)} golden paths pass"


def terminal_lines(story: str, results: list[PathResult], heading: str) -> list[str]:
    lines = [f"{heading}: '{story}'  ({summary_line(results)})", ""]
    for path in results:
        ok = sum(r.passed for r in path.results)
        lines.append(f"  {'PASS' if path.passed else 'FAIL'}  {path.title:<20} {ok}/{len(path.results)} checks")
        for result in path.results:
            if not result.passed:
                lines.append(f"          x {result.subject}: {result.description} ({result.detail})")
        if not path.passed:
            missing = "" if path.fallback_exists else "  (file missing!)"
            lines.append(f"          fallback: {path.fallback}{missing}")
            lines.append(f"          fix:      {path.remediation}  (run `uv run kit check {story} --fix`)")
    return lines


def markdown(story: str, at: datetime, results: list[PathResult], fixed_from: list[PathResult] | None) -> str:
    lines = [f"# Demo readiness: {story}", "", f"Checked {at:%Y-%m-%d %H:%M} UTC. **{summary_line(results)}.**", ""]
    if fixed_from is not None:
        lines += [f"Before `--fix`: {summary_line(fixed_from)}. The remediation ran and everything was checked again.", ""]
    lines += ["| Golden path | Result | Checks passed |", "|---|---|---|"]
    for path in results:
        ok = sum(r.passed for r in path.results)
        lines.append(f"| {path.title} | {'PASS' if path.passed else '**FAIL**'} | {ok}/{len(path.results)} |")
    for path in results:
        lines += ["", f"## {path.title}", "", f"What the rep shows: {path.rep_shows}", ""]
        for result in path.results:
            mark = "pass" if result.passed else "**FAIL**"
            lines.append(f"- {mark}: `{result.subject}` {result.description} ({result.detail})")
        if not path.passed:
            lines += ["", f"**Fallback:** {path.fallback}", f"**Remediation:** {path.remediation}"]
    return "\n".join(lines) + "\n"


def write_reports(story: str, at: datetime, results: list[PathResult],
                  fixed_from: list[PathResult] | None = None) -> tuple[Path, Path]:
    REPORTS_DIR.mkdir(exist_ok=True)
    stem = REPORTS_DIR / f"{story}-{at:%Y%m%d-%H%M%S}"
    data = {
        "story": story,
        "checked_at": at.isoformat(timespec="seconds"),
        "passed": all(r.passed for r in results),
        "summary": summary_line(results),
        "paths": [asdict(r) for r in results],
        "before_fix": [asdict(r) for r in fixed_from] if fixed_from is not None else None,
    }
    json_path, md_path = stem.with_suffix(".json"), stem.with_suffix(".md")
    json_path.write_text(json.dumps(data, indent=2) + "\n")
    md_path.write_text(markdown(story, at, results, fixed_from))
    return json_path, md_path


def latest_report(story: str) -> dict | None:
    """The most recent JSON report for a story, or None."""
    reports = sorted(REPORTS_DIR.glob(f"{story}-*.json"))
    return json.loads(reports[-1].read_text()) if reports else None
