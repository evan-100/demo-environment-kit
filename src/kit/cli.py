"""The `kit` command line. Subcommands are added milestone by milestone (see SPEC §7)."""

import time
from datetime import datetime, timezone

import typer
from dotenv import load_dotenv
from pydantic import ValidationError

from kit import ledger
from kit.fixtures import load_fixtures, load_other_fixture_sets, save_fixtures
from kit.generate import GenerateError, generate_fixtures
from kit.hubspot import HubSpotClient, HubSpotError
from kit.reset import reset as reset_records
from kit.schemas import FixtureSet, load_story
from kit.check import run_checks
from kit.report import terminal_lines, write_reports
from kit.seed import SeedError, apply_plan, break_hero_deal, build_plan, pick_owner
from kit.validate import validate_fixtures

app = typer.Typer(help="Demo readiness kit: generate, seed, check and reset HubSpot demo data.")


@app.callback()
def main() -> None:
    # Read secrets (HUBSPOT_TOKEN, ANTHROPIC_API_KEY) from .env before any command runs.
    load_dotenv()


def fail(message: str) -> None:
    typer.echo(f"FAILED: {message}", err=True)
    raise typer.Exit(code=1)


def counts_of(fixtures: FixtureSet) -> dict:
    return {
        "companies": len(fixtures.companies),
        "contacts": len(fixtures.contacts),
        "deals": len(fixtures.deals),
        "activities": len(fixtures.activities),
    }


def print_errors(errors: list[str]) -> None:
    typer.echo(f"{len(errors)} problem(s) found:", err=True)
    for error in errors:
        typer.echo(f"  - {error}", err=True)


@app.command()
def ping() -> None:
    """Check the HubSpot token works and print the deal pipelines and stage IDs."""
    try:
        pipelines = HubSpotClient().deal_pipelines()
    except HubSpotError as error:
        fail(str(error))

    typer.echo("OK: connected to HubSpot.\n")
    for pipeline in pipelines:
        typer.echo(f"Pipeline '{pipeline['label']}' (id: {pipeline['id']})")
        for stage in sorted(pipeline["stages"], key=lambda s: s["displayOrder"]):
            typer.echo(f"  {stage['id']:<28} {stage['label']}")


@app.command()
def generate(story: str) -> None:
    """Generate fixtures for stories/<story>.yaml with the LLM, validate them, and save them."""
    started = time.monotonic()
    try:
        story_file = load_story(f"stories/{story}.yaml")
        typer.echo(f"Generating '{story}' ({len(story_file.accounts)} hero accounts + filler)...")
        fixtures = generate_fixtures(story_file)
    except (FileNotFoundError, GenerateError) as error:
        ledger.record("generate", story, started, ok=False, error=str(error))
        fail(str(error))
    except ValidationError as error:
        # The story file is wrong, or the LLM wrote a record that breaks a rule.
        ledger.record("generate", story, started, ok=False, error="validation")
        fail(f"a record broke a rule, nothing was saved.\n{error}")

    errors = validate_fixtures(fixtures, story_file, load_other_fixture_sets(story))
    if errors:
        print_errors(errors)
        ledger.record("generate", story, started, ok=False, counts=counts_of(fixtures), error="validation")
        fail("fixtures did not pass validation, nothing was saved. Run generate again.")

    directory = save_fixtures(fixtures)
    ledger.record("generate", story, started, ok=True, counts=counts_of(fixtures))
    typer.echo(f"OK: saved to {directory}/ {counts_of(fixtures)}")


@app.command()
def validate(story: str) -> None:
    """Check the saved fixtures for a story (no LLM, no HubSpot)."""
    started = time.monotonic()
    try:
        story_file = load_story(f"stories/{story}.yaml")
        fixtures = load_fixtures(story)
    except FileNotFoundError as error:
        fail(str(error))
    except ValidationError as error:
        ledger.record("validate", story, started, ok=False, error="schema")
        fail(f"a fixture file breaks a rule:\n{error}")

    errors = validate_fixtures(fixtures, story_file, load_other_fixture_sets(story))
    ledger.record("validate", story, started, ok=not errors, counts=counts_of(fixtures))
    if errors:
        print_errors(errors)
        raise typer.Exit(code=1)
    typer.echo(f"OK: fixtures for '{story}' are valid {counts_of(fixtures)}")


def load_valid_fixtures(story: str) -> FixtureSet:
    """Load a story's fixtures and refuse to continue if they don't pass validation."""
    try:
        story_file = load_story(f"stories/{story}.yaml")
        fixtures = load_fixtures(story)
    except (FileNotFoundError, ValidationError) as error:
        fail(str(error))
    errors = validate_fixtures(fixtures, story_file, load_other_fixture_sets(story))
    if errors:
        print_errors(errors)
        fail("fixtures did not pass validation. Fix them or run `kit generate` again.")
    return fixtures


def run_seed(story: str, hubspot: HubSpotClient) -> None:
    started = time.monotonic()
    fixtures = load_valid_fixtures(story)
    try:
        plan = build_plan(fixtures, datetime.now(timezone.utc), owner_id=pick_owner(hubspot))
    except (HubSpotError, SeedError) as error:
        fail(f"couldn't pick a record owner: {error}")
    typer.echo(f"Seeding '{story}': {plan.counts()}")
    try:
        result = apply_plan(plan, hubspot)
    except HubSpotError as error:
        ledger.record("seed", story, started, ok=False, counts=plan.counts(), error=str(error)[:500])
        fail(f"{error}\nEverything written so far is tagged, so `kit seed` again or `kit reset` is safe.")

    ledger.record("seed", story, started, ok=not result.errors, counts=result.counts,
                  error=f"{len(result.errors)} partial failures" if result.errors else None)
    if result.errors:
        print_errors(result.errors)
        fail("some records or links failed (listed above). Rerunning `kit seed` is safe.")
    typer.echo(f"OK: seeded in {time.monotonic() - started:.1f}s")


@app.command()
def seed(
    story: str,
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the plan without calling HubSpot."),
) -> None:
    """Load stories/<story> fixtures into HubSpot. Safe to run again."""
    if dry_run:
        plan = build_plan(load_valid_fixtures(story), datetime.now(timezone.utc))
        typer.echo(f"Dry run for '{story}', nothing is sent to HubSpot.\n")
        for object_type, records in plan.records.items():
            if records:
                typer.echo(f"{object_type}: {len(records)} to create or update")
                for record in records[:3]:
                    typer.echo(f"    {record['kit_id']}: {record['properties']}")
                if len(records) > 3:
                    typer.echo(f"    ... and {len(records) - 3} more")
        typer.echo(f"links: {len(plan.links)}")
        typer.echo("(Every record also gets an owner, the HubSpot user picked at seed time.)")
        return
    run_seed(story, HubSpotClient())


@app.command()
def reset(
    story: str = typer.Argument(None, help="Story to remove. Leave out and use --all for every story."),
    all_stories: bool = typer.Option(False, "--all", help="Remove the records of every story."),
    reseed: bool = typer.Option(False, "--reseed", help="Seed the story again afterwards."),
    yes: bool = typer.Option(False, "--yes", help="Don't ask for confirmation."),
) -> None:
    """Delete the records the kit created (and only those)."""
    if all_stories == bool(story):
        fail("give a story name, or --all (not both)")
    if reseed and all_stories:
        fail("--reseed needs a single story")

    target = "every story" if all_stories else f"story '{story}'"
    if not yes:
        typer.confirm(f"Delete all kit records of {target} from HubSpot?", abort=True)

    started = time.monotonic()
    hubspot = HubSpotClient()
    fixtures = None
    if story:
        try:
            fixtures = load_fixtures(story)
        except (FileNotFoundError, ValidationError):
            pass  # search alone still finds the records
    try:
        result = reset_records(hubspot, story, fixtures)
    except HubSpotError as error:
        ledger.record("reset", story, started, ok=False, error=str(error)[:500])
        fail(str(error))

    ledger.record("reset", story, started, ok=True, counts=result.deleted)
    typer.echo(f"OK: deleted {result.deleted or 'nothing'} in {time.monotonic() - started:.1f}s")
    if result.skipped:
        typer.echo(f"Left alone {result.skipped} record(s) that weren't tagged for {target}.")
    if reseed:
        run_seed(story, hubspot)


# How long to wait for HubSpot search to catch up after `--fix` reseeds, and how often to retry.
SEARCH_SETTLE_SECONDS = 10
FIX_RECHECKS = 3


@app.command()
def check(
    story: str,
    fix: bool = typer.Option(False, "--fix", help="Run the remediation for failing paths, then check again."),
) -> None:
    """Check every golden path of a story against HubSpot and write a report."""
    started = time.monotonic()
    story_file = load_story(f"stories/{story}.yaml")
    fixtures = load_valid_fixtures(story)
    hubspot = HubSpotClient()
    try:
        owner = pick_owner(hubspot)
        results = run_checks(hubspot, fixtures, story_file.golden_paths, owner)
    except (HubSpotError, SeedError) as error:
        ledger.record("check", story, started, ok=False, error=str(error)[:500])
        fail(str(error))
    for line in terminal_lines(story, results, "Readiness"):
        typer.echo(line)

    before_fix = None
    failing = [r for r in results if not r.passed]
    if fix and failing:
        before_fix = results
        remediations = sorted({r.remediation for r in failing})
        typer.echo(f"\nFixing: {', '.join(remediations)}")
        if "reseed" in remediations:
            run_seed(story, hubspot)
        # Search (used by the pipeline and rep-feed checks) lags behind writes, so allow a few tries.
        for attempt in range(1, FIX_RECHECKS + 1):
            typer.echo(f"Waiting {SEARCH_SETTLE_SECONDS}s for HubSpot search to catch up (try {attempt}/{FIX_RECHECKS})...")
            time.sleep(SEARCH_SETTLE_SECONDS)
            results = run_checks(hubspot, fixtures, story_file.golden_paths, owner)
            if all(r.passed for r in results):
                break
        typer.echo("")
        for line in terminal_lines(story, results, "After fix"):
            typer.echo(line)

    passed = all(r.passed for r in results)
    json_path, md_path = write_reports(story, datetime.now(timezone.utc), results, before_fix)
    ledger.record("check", story, started, ok=passed, counts={
        "paths": len(results), "passed": sum(r.passed for r in results), "fixed": before_fix is not None,
    })
    typer.echo(f"\nReport: {md_path} (and {json_path.name})")
    if not passed:
        raise typer.Exit(code=1)


@app.command(name="break")
def break_demo(story: str) -> None:
    """Deliberately break the first hero deal (wrong stage, overdue), to demo `kit check --fix`."""
    started = time.monotonic()
    fixtures = load_valid_fixtures(story)
    kit_id = break_hero_deal(fixtures, HubSpotClient(), datetime.now(timezone.utc))
    ledger.record("break", story, started, ok=True, counts={"deals": 1})
    typer.echo(f"Broke '{kit_id}': stage set to appointmentscheduled, close date 5 days ago.")
    typer.echo(f"Now run `kit check {story}` to see it caught, then `kit check {story} --fix`.")
