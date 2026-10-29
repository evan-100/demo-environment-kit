"""Tests for the MCP server over the real protocol: start `kit mcp` and talk to it
with the official MCP client, exactly as an agent would. No HubSpot calls.
"""

import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

EXPECTED_TOOLS = {"list_stories", "create_story", "generate_story", "seed_story", "check_story", "reset_story", "get_last_report"}

# Run the server with the same Python as the tests, from any folder (it finds the repo itself).
SERVER = StdioServerParameters(command=sys.executable, args=["-m", "kit.cli", "mcp"], cwd="/")


def with_session(steps):
    """Start the server, run `steps(session)`, and return what it returns."""
    async def main():
        async with stdio_client(SERVER) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await steps(session)
    return asyncio.run(main())


def text(result) -> str:
    return "\n".join(c.text for c in result.content)


def test_exposes_the_spec_tools_with_hints():
    tools = with_session(lambda s: s.list_tools()).tools
    by_name = {t.name: t for t in tools}
    assert set(by_name) == EXPECTED_TOOLS
    assert by_name["reset_story"].annotations.destructive_hint is True
    assert by_name["list_stories"].annotations.read_only_hint is True


def test_list_stories_and_dry_run_seed():
    async def steps(session):
        return (await session.call_tool("list_stories", {}),
                await session.call_tool("seed_story", {"story": "healthcare", "dry_run": True}))

    stories, dry_run = with_session(steps)
    assert not stories.is_error and "Acme Regional Health" in text(stories)
    plan = json.loads(text(dry_run))
    assert plan["dry_run"] is True and plan["would_write"]["companies"] == 27


def test_reset_without_confirm_is_refused_with_a_reason():
    result = with_session(lambda s: s.call_tool("reset_story", {"story": "healthcare"}))
    assert result.is_error
    assert "confirm=true" in text(result)  # the agent sees why, not just "Error executing tool"


def test_unknown_story_explains_itself():
    result = with_session(lambda s: s.call_tool("seed_story", {"story": "nosuchstory", "dry_run": True}))
    assert result.is_error and "no story called 'nosuchstory'" in text(result)
