"""The `kit` command line. Subcommands are added milestone by milestone (see SPEC §7)."""

import typer

from kit.hubspot import HubSpotClient, HubSpotError

app = typer.Typer(help="Demo readiness kit: generate, seed, check and reset HubSpot demo data.")


@app.callback()
def main() -> None:
    # An empty callback keeps `kit ping` a named subcommand even while it's the only one.
    pass


@app.command()
def ping() -> None:
    """Check the HubSpot token works and print the deal pipelines and stage IDs."""
    try:
        pipelines = HubSpotClient().deal_pipelines()
    except HubSpotError as error:
        typer.echo(f"FAILED: {error}", err=True)
        raise typer.Exit(code=1)

    typer.echo("OK: connected to HubSpot.\n")
    for pipeline in pipelines:
        typer.echo(f"Pipeline '{pipeline['label']}' (id: {pipeline['id']})")
        for stage in sorted(pipeline["stages"], key=lambda s: s["displayOrder"]):
            typer.echo(f"  {stage['id']:<28} {stage['label']}")
