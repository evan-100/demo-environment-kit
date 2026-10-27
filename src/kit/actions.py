"""Everything the kit can do, as plain functions.

The CLI (cli.py) and the MCP server (mcp_server.py) both call these, so a
command behaves the same whether a person or an agent runs it. Each action:
- records a line in the run ledger (SPEC §3.6),
- returns a plain dict (easy to print, easy to send to an agent),
- raises KitError with a plain-language message when something goes wrong.

`say` is an optional function for progress messages (the CLI passes
typer.echo; the MCP server stays quiet, because stdout is its protocol channel).
"""

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from pydantic import ValidationError

from kit import ledger
from kit.check import PathResult, run_checks
from kit.fixtures import FIXTURES_DIR, load_fixtures, load_other_fixture_sets, save_fixtures
from kit.generate import GenerateError, generate_fixtures
from kit.hubspot import HubSpotClient, HubSpotError
from kit.report import REPORTS_DIR, latest_report, summary_line, write_reports
from kit.reset import reset as reset_records
from kit.schemas import FixtureSet, Story, load_story
from kit.seed import SeedError, apply_plan, break_hero_deal, build_plan, pick_owner
from kit.validate import validate_fixtures

STORIES_DIR = Path("stories")

# After `check --fix` reseeds, HubSpot search needs a few seconds to catch up.
SEARCH_SETTLE_SECONDS = 10
FIX_RECHECKS = 3

Say = Callable[[str], None]


def quiet(message: str) -> None:
    pass


class KitError(Exception):
    """A failure explained in plain language. `problems` lists individual issues, if any."""

    def __init__(self, message: str, problems: list[str] | None = None):
        super().__init__(message)
        self.problems = problems or []


def counts_of(fixtures: FixtureSet) -> dict:
    return {
        "companies": len(fixtures.companies),
        "contacts": len(fixtures.contacts),
        "deals": len(fixtures.deals),
        "activities": len(fixtures.activities),
    }


def load_story_file(story: str) -> Story:
    try:
        return load_story(STORIES_DIR / f"{story}.yaml")
    except FileNotFoundError:
        raise KitError(f"there's no story called '{story}' (expected stories/{story}.yaml)")
    except ValidationError as error:
        raise KitError(f"stories/{story}.yaml breaks a rule:\n{error}")


def load_valid_fixtures(story: str) -> tuple[Story, FixtureSet]:
    """Load a story and its fixtures, and refuse to continue if they don't pass validation."""
    story_file = load_story_file(story)
    try:
        fixtures = load_fixtures(story)
    except FileNotFoundError as error:
        raise KitError(str(error))
    except ValidationError as error:
        raise KitError(f"a fixture file for '{story}' breaks a rule:\n{error}")
    problems = validate_fixtures(fixtures, story_file, load_other_fixture_sets(story))
    if problems:
        raise KitError(f"fixtures for '{story}' did not pass validation", problems)
    return story_file, fixtures


# --- Stories and fixtures ------------------------------------------------------------------


def list_stories() -> list[dict]:
    stories = []
    for path in sorted(STORIES_DIR.glob("*.yaml")):
        story = load_story_file(path.stem)
        report = latest_report(story.id)
        stories.append({
            "story": story.id,
            "industry": story.industry,
            "hero_accounts": [a.name for a in story.accounts],
            "golden_paths": story.golden_paths,
            "has_fixtures": (FIXTURES_DIR / story.id / "companies.json").exists(),
            "last_check": {"checked_at": report["checked_at"], "summary": report["summary"]} if report else None,
        })
    return stories


def generate(story: str, say: Say = quiet) -> dict:
    started = time.monotonic()
    story_file = load_story_file(story)
    say(f"Generating '{story}' ({len(story_file.accounts)} hero accounts + filler)...")
    try:
        fixtures = generate_fixtures(story_file)
    except GenerateError as error:
        ledger.record("generate", story, started, ok=False, error=str(error))
        raise KitError(str(error))
    except ValidationError as error:
        ledger.record("generate", story, started, ok=False, error="validation")
        raise KitError(f"the generated data broke a rule, nothing was saved:\n{error}")

    problems = validate_fixtures(fixtures, story_file, load_other_fixture_sets(story))
    if problems:
        ledger.record("generate", story, started, ok=False, counts=counts_of(fixtures), error="validation")
        raise KitError("generated fixtures did not pass validation, nothing was saved. Run generate again.", problems)

    directory = save_fixtures(fixtures)
    ledger.record("generate", story, started, ok=True, counts=counts_of(fixtures))
    return {"story": story, "saved_to": str(directory), "counts": counts_of(fixtures)}


def validate(story: str) -> dict:
    started = time.monotonic()
    try:
        _, fixtures = load_valid_fixtures(story)
    except KitError as error:
        ledger.record("validate", story, started, ok=False, error=str(error)[:500])
        raise
    ledger.record("validate", story, started, ok=True, counts=counts_of(fixtures))
    return {"story": story, "valid": True, "counts": counts_of(fixtures)}


# --- HubSpot ---------------------------------------------------------------------------------


def seed(story: str, dry_run: bool = False, say: Say = quiet, hubspot=None) -> dict:
    _, fixtures = load_valid_fixtures(story)
    now = datetime.now(timezone.utc)

    if dry_run:
        plan = build_plan(fixtures, now)
        return {
            "story": story,
            "dry_run": True,
            "would_write": plan.counts(),
            "examples": {t: records[0] for t, records in plan.records.items() if records},
            "note": "Nothing was sent to HubSpot. Every record also gets an owner, picked at seed time.",
        }

    started = time.monotonic()
    hubspot = hubspot or HubSpotClient()
    try:
        plan = build_plan(fixtures, now, owner_id=pick_owner(hubspot))
        say(f"Seeding '{story}': {plan.counts()}")
        result = apply_plan(plan, hubspot)
    except (HubSpotError, SeedError) as error:
        ledger.record("seed", story, started, ok=False, error=str(error)[:500])
        raise KitError(f"{error}\nEverything written so far is tagged, so seeding again or resetting is safe.")

    seconds = round(time.monotonic() - started, 1)
    ledger.record("seed", story, started, ok=not result.errors, counts=result.counts,
                  error=f"{len(result.errors)} partial failures" if result.errors else None)
    if result.errors:
        raise KitError("some records or links failed. Seeding again is safe.", result.errors)
    say(f"OK: seeded in {seconds}s")
    return {"story": story, "seeded": result.counts, "seconds": seconds}


def reset(story: str | None, reseed: bool = False, say: Say = quiet, hubspot=None) -> dict:
    """Delete the kit's records for one story, or for every story when `story` is None."""
    if reseed and story is None:
        raise KitError("reseed needs a single story")
    started = time.monotonic()
    hubspot = hubspot or HubSpotClient()
    fixtures = None
    if story:
        load_story_file(story)  # fail early on a typo
        try:
            fixtures = load_fixtures(story)
        except (FileNotFoundError, ValidationError):
            pass  # search alone still finds the records
    try:
        result = reset_records(hubspot, story, fixtures)
    except HubSpotError as error:
        ledger.record("reset", story, started, ok=False, error=str(error)[:500])
        raise KitError(str(error))

    seconds = round(time.monotonic() - started, 1)
    ledger.record("reset", story, started, ok=True, counts=result.deleted)
    say(f"OK: deleted {result.deleted or 'nothing'} in {seconds}s")
    if result.skipped:
        say(f"Left alone {result.skipped} record(s) that weren't tagged for this story.")
    outcome = {"story": story or "all", "deleted": result.deleted, "left_alone": result.skipped, "seconds": seconds}
    if reseed:
        outcome["reseed"] = seed(story, say=say, hubspot=hubspot)
    return outcome


def break_story(story: str) -> dict:
    started = time.monotonic()
    _, fixtures = load_valid_fixtures(story)
    kit_id = break_hero_deal(fixtures, HubSpotClient(), datetime.now(timezone.utc))
    ledger.record("break", story, started, ok=True, counts={"deals": 1})
    return {"story": story, "broke": kit_id, "how": "stage set to appointmentscheduled, close date 5 days ago"}


# --- Readiness checks ------------------------------------------------------------------------


@dataclass
class CheckOutcome:
    story: str
    hero_accounts: list[str]
    results: list[PathResult]
    before_fix: list[PathResult] | None
    report_markdown: str
    report_json: str

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    def to_dict(self) -> dict:
        """A compact summary for agents: every path, and the details only for failures."""
        def describe(results: list[PathResult]) -> list[dict]:
            return [{
                "path": r.id,
                "title": r.title,
                "passed": r.passed,
                "failures": [f"{x.subject}: {x.description} ({x.detail})" for x in r.results if not x.passed],
                **({} if r.passed else {"fallback": r.fallback, "remediation": r.remediation}),
            } for r in results]

        return {
            "story": self.story,
            "hero_accounts": self.hero_accounts,
            "passed": self.passed,
            "summary": summary_line(self.results),
            "paths": describe(self.results),
            "before_fix": {"summary": summary_line(self.before_fix), "paths": describe(self.before_fix)}
            if self.before_fix is not None else None,
            "report": self.report_markdown,
        }


def check(story: str, fix: bool = False, say: Say = quiet, hubspot=None) -> CheckOutcome:
    started = time.monotonic()
    story_file, fixtures = load_valid_fixtures(story)
    hubspot = hubspot or HubSpotClient()
    try:
        owner = pick_owner(hubspot)
        results = run_checks(hubspot, fixtures, story_file.golden_paths, owner)
        before_fix = None
        failing = [r for r in results if not r.passed]
        if fix and failing:
            before_fix = results
            remediations = sorted({r.remediation for r in failing})
            say(f"{summary_line(results)}. Fixing: {', '.join(remediations)}")
            if "reseed" in remediations:
                seed(story, say=say, hubspot=hubspot)
            # Search (used by the pipeline and rep-feed checks) lags behind writes, so allow a few tries.
            for attempt in range(1, FIX_RECHECKS + 1):
                say(f"Waiting {SEARCH_SETTLE_SECONDS}s for HubSpot search to catch up (try {attempt}/{FIX_RECHECKS})...")
                time.sleep(SEARCH_SETTLE_SECONDS)
                results = run_checks(hubspot, fixtures, story_file.golden_paths, owner)
                if all(r.passed for r in results):
                    break
    except (HubSpotError, SeedError) as error:
        ledger.record("check", story, started, ok=False, error=str(error)[:500])
        raise KitError(str(error))

    json_path, md_path = write_reports(story, datetime.now(timezone.utc), results, before_fix)
    hero_accounts = [a.name for a in story_file.accounts]
    outcome = CheckOutcome(story, hero_accounts, results, before_fix, str(md_path), str(json_path))
    ledger.record("check", story, started, ok=outcome.passed, counts={
        "paths": len(results), "passed": sum(r.passed for r in results), "fixed": before_fix is not None,
    })
    return outcome


def last_report(story: str) -> dict:
    """The latest readiness report: when, the headline, and the full Markdown version."""
    load_story_file(story)
    report = latest_report(story)
    if report is None:
        raise KitError(f"no readiness report for '{story}' yet. Run a check first.")
    markdown_files = sorted(REPORTS_DIR.glob(f"{story}-*.md"))
    return {
        "story": story,
        "checked_at": report["checked_at"],
        "passed": report["passed"],
        "summary": report["summary"],
        "markdown": markdown_files[-1].read_text() if markdown_files else None,
    }
