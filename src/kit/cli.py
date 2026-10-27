"""The `kit` command line. Subcommands are added milestone by milestone (see SPEC §7)."""

import time

import typer
from dotenv import load_dotenv
from pydantic import ValidationError

from kit import ledger
from kit.fixtures import load_fixtures, load_other_fixture_sets, save_fixtures
from kit.generate import GenerateError, generate_fixtures
from kit.hubspot import HubSpotClient, HubSpotError
from kit.schemas import FixtureSet, load_story
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
