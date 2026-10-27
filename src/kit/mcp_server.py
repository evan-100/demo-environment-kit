"""MCP server: lets any MCP-capable agent drive the kit.

Each tool calls the same function in actions.py that the CLI uses. Tools
return plain dicts. Expected failures are raised as ToolError, so the agent
reads the plain-language message (any other exception would reach it only
as "Error executing tool").

Run it with `kit mcp` (stdio). Nothing may be printed to stdout here,
because stdout carries the MCP protocol.
"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from kit import actions
from kit.actions import KitError

# The repo root, where stories/, fixtures/, checks/ and reports/ live.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

INSTRUCTIONS = """\
Demo readiness kit: keeps HubSpot demo environments realistic, current and
verified before a sales call. Each "story" (e.g. healthcare) is a demo scenario
with hero accounts and golden paths (the screens a rep will show).

To get a demo ready: run check_story first, to see the demo as the rep would.
If any golden path fails, call check_story with fix=true, which runs the
remediation and checks again. If the story's records aren't in HubSpot at all
(e.g. "record not found"), seed_story first. Seeding before checking would
hide what was broken, so check first.
Then summarise for the rep: what passed, what was broken and fixed, and for
anything still failing, the fallback to use on the call.
reset_story deletes the story's demo records and needs confirm=true.
"""

# Warnings only: the agent's terminal shouldn't fill up with a log line per HubSpot request.
server = MCPServer(name="demo-readiness-kit", instructions=INSTRUCTIONS, log_level="WARNING")


def explain(error: KitError) -> ToolError:
    """Turn a KitError into the error message the agent sees."""
    lines = [str(error)] + [f"- {p}" for p in error.problems]
    return ToolError("\n".join(lines))


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_stories() -> list[dict]:
    """List the demo stories, whether each has fixtures, and its last readiness result."""
    try:
        return actions.list_stories()
    except KitError as error:
        raise explain(error)


@server.tool()
def generate_story(story: str) -> dict:
    """Generate fresh fixtures for a story with the LLM (takes about 2 minutes, costs API credits).

    Only needed when the story file changed. Seeding uses the saved fixtures.
    """
    try:
        return actions.generate(story)
    except KitError as error:
        raise explain(error)


@server.tool(annotations=ToolAnnotations(idempotentHint=True))
def seed_story(story: str, dry_run: bool = False) -> dict:
    """Load a story's demo data into HubSpot. Safe to run again: it updates the same records.

    With dry_run=true, returns what would be written without calling HubSpot.
    """
    try:
        return actions.seed(story, dry_run=dry_run)
    except KitError as error:
        raise explain(error)


@server.tool()
def check_story(story: str, fix: bool = False) -> dict:
    """Check every golden path of a story against HubSpot and write a report.

    With fix=true, failing paths are repaired (by reseeding) and checked again;
    the result then includes `before_fix`. Failing paths list a fallback the rep can use.
    """
    try:
        return actions.check(story, fix=fix).to_dict()
    except KitError as error:
        raise explain(error)


@server.tool(annotations=ToolAnnotations(destructiveHint=True))
def reset_story(story: str, reseed: bool = False, confirm: bool = False) -> dict:
    """Delete every HubSpot record the kit created for a story (never anything else).

    Destructive: you must pass confirm=true. With reseed=true, the story is seeded again afterwards.
    """
    if not confirm:
        raise ToolError(
            f"reset_story deletes all demo records of '{story}' from HubSpot. "
            "Ask the user, then call again with confirm=true."
        )
    try:
        return actions.reset(story, reseed=reseed)
    except KitError as error:
        raise explain(error)


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_last_report(story: str) -> dict:
    """The latest readiness report for a story: when it ran, the headline and the full Markdown."""
    try:
        return actions.last_report(story)
    except KitError as error:
        raise explain(error)


def run() -> None:
    # Agents may start the server from any folder; the kit's paths are relative to the repo root.
    os.chdir(PROJECT_ROOT)
    load_dotenv(PROJECT_ROOT / ".env")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    server.run("stdio")
