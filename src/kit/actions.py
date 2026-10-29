"""Everything the kit can do, as plain functions.

The CLI (cli.py) and the MCP server (mcp_server.py) both call these, so a
command behaves the same whether a person or an agent runs it. Each action:
- records a line in the run ledger,
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
from kit.new import new_story
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


def stories_sharing_background(story_file: Story) -> set[str]:
    """The other stories built on the same background accounts as this one.

    A story with `background: healthcare` shares with healthcare, and with every other
    story whose background is healthcare.
    """
    root = story_file.background or story_file.id
    shared = set()
    for path in STORIES_DIR.glob("*.yaml"):
        try:
            other = load_story(path)
        except (ValidationError, OSError):
            continue  # a broken story file is reported when that story is used
        if other.id != story_file.id and (other.background or other.id) == root:
            shared.add(other.id)
    return shared


def load_background(story_file: Story) -> FixtureSet | None:
    """The fixtures this story borrows its background accounts from, if it borrows them."""
    if story_file.background is None:
        return None
    base = load_story_file(story_file.background)
    if base.background is not None:
        raise KitError(f"background '{base.id}' borrows its own background; point at '{base.background}' instead")
    try:
        return load_fixtures(base.id)
    except FileNotFoundError:
        raise KitError(f"story '{base.id}' has no fixtures yet. Run `uv run kit generate {base.id}` first.")


def load_valid_fixtures(story: str) -> tuple[Story, FixtureSet]:
    """Load a story and its fixtures, and refuse to continue if they don't pass validation."""
    story_file = load_story_file(story)
    try:
        fixtures = load_fixtures(story)
    except FileNotFoundError as error:
        raise KitError(str(error))
    except ValidationError as error:
        raise KitError(f"a fixture file for '{story}' breaks a rule:\n{error}")
    problems = validate_fixtures(fixtures, story_file, load_other_fixture_sets(story),
                                 stories_sharing_background(story_file))
    if problems:
        raise KitError(f"fixtures for '{story}' did not pass validation", problems)
    return story_file, fixtures


# --- Stories and fixtures ------------------------------------------------------------------


def is_loaded(story: str, hubspot) -> bool | None:
    """True if the story's records are in HubSpot now; None if fixtures or HubSpot aren't available.

    Reads the story's companies by kit ID, which (unlike search) is up to date the moment
    records are written or deleted.
    """
    try:
        companies = load_fixtures(story).companies
        return bool(hubspot.read_by_kit_ids("companies", [c.kit_id for c in companies]))
    except (FileNotFoundError, ValidationError, HubSpotError):
        return None


def list_stories(hubspot=None) -> list[dict]:
    """Every story: where it came from, whether it has fixtures, whether it's loaded in HubSpot, last check."""
    try:
        hubspot = hubspot or HubSpotClient()
    except HubSpotError:
        hubspot = None  # no token: list the stories anyway, just without the HubSpot column
    stories = []
    for path in sorted(STORIES_DIR.glob("*.yaml")):
        story = load_story_file(path.stem)
        report = latest_report(story.id)
        stories.append({
            "story": story.id,
            "made_by": "kit new" if path.read_text().startswith("# Written by `kit new`") else "bundled",
            "industry": story.industry,
            "background_from": story.background,
            "hero_accounts": [a.name for a in story.accounts],
            "golden_paths": story.golden_paths,
            "has_fixtures": (FIXTURES_DIR / story.id / "companies.json").exists(),
            "in_hubspot": is_loaded(story.id, hubspot) if hubspot else None,
            "last_check": {"checked_at": report["checked_at"], "summary": report["summary"]} if report else None,
            "background_group": story.background or story.id,
        })
    # A story can't be seeded while another story on the same background is loaded (see seed).
    for row in stories:
        row["blocked_by"] = [other["story"] for other in stories
                             if other["in_hubspot"] and other is not row
                             and other["background_group"] == row["background_group"]]
    for row in stories:
        del row["background_group"]
    return stories


def generate(story: str, say: Say = quiet) -> dict:
    started = time.monotonic()
    story_file = load_story_file(story)
    background = load_background(story_file)
    heroes = f"{len(story_file.accounts)} hero account{'s' if len(story_file.accounts) != 1 else ''}"
    if background is None:
        say(f"Generating '{story}' ({heroes} + filler)...")
    else:
        say(f"Generating '{story}' ({heroes}; background accounts reused from '{story_file.background}')...")
    try:
        fixtures = generate_fixtures(story_file, background=background)
    except GenerateError as error:
        ledger.record("generate", story, started, ok=False, error=str(error))
        raise KitError(str(error))
    except ValidationError as error:
        ledger.record("generate", story, started, ok=False, error="validation")
        raise KitError(f"the generated data broke a rule, nothing was saved:\n{error}")

    problems = validate_fixtures(fixtures, story_file, load_other_fixture_sets(story),
                                 stories_sharing_background(story_file))
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


def new(description: str, say: Say = quiet) -> dict:
    """Draft stories/<id>.yaml from a one-line description. Nothing is generated or seeded."""
    started = time.monotonic()
    existing = [load_story_file(path.stem) for path in sorted(STORIES_DIR.glob("*.yaml"))]
    say("Drafting a story file from your description...")
    try:
        path, data = new_story(description, existing, STORIES_DIR)
    except GenerateError as error:
        ledger.record("new", None, started, ok=False, error=str(error))
        raise KitError(str(error))
    except ValidationError as error:
        ledger.record("new", None, started, ok=False, error="validation")
        raise KitError(f"the drafted story broke a rule, nothing was saved. Try again or reword it:\n{error}")
    ledger.record("new", data["id"], started, ok=True)
    return {
        "story": data["id"],
        "saved_to": str(path),
        "hero_accounts": [a["name"] for a in data["accounts"]],
        "background_from": data.get("background"),
        "next": f"Review {path}, then run `uv run kit generate {data['id']}`.",
    }


# --- HubSpot ---------------------------------------------------------------------------------


def loaded_neighbours(story_file: Story, hubspot) -> list[str]:
    """Stories sharing this one's background accounts that are in HubSpot right now.

    They share background contacts (same email addresses; HubSpot allows one contact per email)
    and may feature the same hero account, so only one of these stories can be loaded at a time. Reads by kit ID, which
    (unlike search) sees records the moment they're written or deleted.
    """
    loaded = []
    for other in sorted(stories_sharing_background(story_file)):
        try:
            other_fixtures = load_fixtures(other)
        except (FileNotFoundError, ValidationError):
            continue  # never generated, so never seeded
        if hubspot.read_by_kit_ids("companies", [c.kit_id for c in other_fixtures.companies]):
            loaded.append(other)
    return loaded


def seed(story: str, dry_run: bool = False, say: Say = quiet, hubspot=None) -> dict:
    story_file, fixtures = load_valid_fixtures(story)
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
        if loaded := loaded_neighbours(story_file, hubspot):
            names = ", ".join(loaded)
            raise KitError(
                f"'{names}' is loaded in HubSpot and shares background accounts with '{story}'. "
                f"Only one of them can be loaded at a time. Run `uv run kit reset {loaded[0]} --yes` first."
            )
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
