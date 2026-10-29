"""The `kit` command line. Each command is a thin layer over a function in actions.py."""

import json

import typer
from dotenv import load_dotenv

from kit import actions
from kit.actions import KitError
from kit.hubspot import HubSpotClient, HubSpotError
from kit.report import terminal_lines

app = typer.Typer(help="Demo environment kit: generate, seed, check and reset HubSpot demo data.")


@app.callback()
def main() -> None:
    # Read secrets (HUBSPOT_TOKEN, ANTHROPIC_API_KEY) from .env before any command runs.
    load_dotenv()


def fail(error: Exception) -> None:
    typer.echo(f"FAILED: {error}", err=True)
    for problem in getattr(error, "problems", []):
        typer.echo(f"  - {problem}", err=True)
    raise typer.Exit(code=1)


@app.command()
def ping() -> None:
    """Check the HubSpot token works and print the deal pipelines and stage IDs."""
    try:
        pipelines = HubSpotClient().deal_pipelines()
    except HubSpotError as error:
        fail(error)

    typer.echo("OK: connected to HubSpot.\n")
    for pipeline in pipelines:
        typer.echo(f"Pipeline '{pipeline['label']}' (id: {pipeline['id']})")
        for stage in sorted(pipeline["stages"], key=lambda s: s["displayOrder"]):
            typer.echo(f"  {stage['id']:<28} {stage['label']}")


@app.command()
def new(description: str = typer.Argument(..., help='What the demo is about, in quotes.')) -> None:
    """Draft a story file from a description. Review it, then run `kit generate`."""
    try:
        result = actions.new(description, say=typer.echo)
    except KitError as error:
        fail(error)
    typer.echo(f"OK: wrote {result['saved_to']}")
    typer.echo(f"  hero accounts: {', '.join(result['hero_accounts'])}")
    if result["background_from"]:
        typer.echo(f"  background accounts: reused from '{result['background_from']}' (only the heroes get generated)")
    else:
        typer.echo("  background accounts: new industry, so they'll be generated too")
    typer.echo(f"Next: {result['next']}")


@app.command()
def generate(story: str) -> None:
    """Generate fixtures for stories/<story>.yaml with the LLM, validate them, and save them."""
    try:
        result = actions.generate(story, say=typer.echo)
    except KitError as error:
        fail(error)
    typer.echo(f"OK: saved to {result['saved_to']}/ {result['counts']}")


@app.command()
def validate(story: str) -> None:
    """Check the saved fixtures for a story (no LLM, no HubSpot)."""
    try:
        result = actions.validate(story)
    except KitError as error:
        fail(error)
    typer.echo(f"OK: fixtures for '{story}' are valid {result['counts']}")


@app.command()
def seed(
    story: str,
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the plan without calling HubSpot."),
) -> None:
    """Load a story's fixtures into HubSpot. Safe to run again."""
    try:
        result = actions.seed(story, dry_run=dry_run, say=typer.echo)
    except KitError as error:
        fail(error)
    if dry_run:
        typer.echo(f"Dry run for '{story}', nothing is sent to HubSpot.\n")
        typer.echo(f"Would write: {result['would_write']}\n")
        for object_type, example in result["examples"].items():
            typer.echo(f"{object_type} example: {example['kit_id']}: {example['properties']}\n")
        typer.echo(result["note"])


@app.command()
def reset(
    story: str = typer.Argument(None, help="Story to remove. Leave out and use --all for every story."),
    all_stories: bool = typer.Option(False, "--all", help="Remove the records of every story."),
    reseed: bool = typer.Option(False, "--reseed", help="Seed the story again afterwards."),
    yes: bool = typer.Option(False, "--yes", help="Don't ask for confirmation."),
) -> None:
    """Delete the records the kit created (and only those)."""
    if all_stories == bool(story):
        fail(KitError("give a story name, or --all (not both)"))
    target = "every story" if all_stories else f"story '{story}'"
    if not yes:
        typer.confirm(f"Delete all kit records of {target} from HubSpot?", abort=True)
    try:
        actions.reset(None if all_stories else story, reseed=reseed, say=typer.echo)
    except KitError as error:
        fail(error)


@app.command()
def check(
    story: str,
    fix: bool = typer.Option(False, "--fix", help="Run the remediation for failing paths, then check again."),
) -> None:
    """Check every golden path of a story against HubSpot and write a report."""
    try:
        outcome = actions.check(story, fix=fix, say=typer.echo)
    except KitError as error:
        fail(error)
    if outcome.before_fix is not None:
        for line in terminal_lines(story, outcome.before_fix, "Before fix"):
            typer.echo(line)
        typer.echo("")
    heading = "After fix" if outcome.before_fix is not None else "Readiness"
    for line in terminal_lines(story, outcome.results, heading):
        typer.echo(line)
    typer.echo(f"\nReport: {outcome.report_markdown}")
    if not outcome.passed:
        raise typer.Exit(code=1)


@app.command(name="break")
def break_demo(story: str) -> None:
    """Deliberately break the first hero deal (wrong stage, overdue), to demo `kit check --fix`."""
    try:
        result = actions.break_story(story)
    except KitError as error:
        fail(error)
    typer.echo(f"Broke '{result['broke']}': {result['how']}.")
    typer.echo(f"Now run `kit check {story}` to see it caught, then `kit check {story} --fix`.")


@app.command()
def stories() -> None:
    """List the stories, whether they have fixtures, and their last readiness result."""
    typer.echo(json.dumps(actions.list_stories(), indent=2))


@app.command()
def metrics(batch: str = typer.Option(None, help="Batch label (KIT_BATCH). Default: the latest batch.")) -> None:
    """Print the README metrics table, computed from runs/ledger.jsonl."""
    from kit import metrics as kit_metrics
    from kit.ledger import read_entries

    entries = read_entries()
    batch = batch or kit_metrics.latest_batch(entries)
    if not batch:
        fail(KitError("no labelled runs in the ledger yet. Run scripts/measure.py first."))
    typer.echo(kit_metrics.markdown_table(batch, kit_metrics.compute(entries, batch)))


@app.command()
def mcp() -> None:
    """Run the MCP server (stdio), so an agent can drive the kit."""
    from kit.mcp_server import run

    run()


if __name__ == "__main__":
    app()
