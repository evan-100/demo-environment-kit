"""Demo: an agent gets a demo ready through the kit's MCP server (SPEC §4.6).

    uv run python examples/get_acme_ready.py
    uv run python examples/get_acme_ready.py "Is the healthcare demo ready?"

It starts `kit mcp`, gives Claude the server's tools, and asks:
"Get a healthcare demo ready for Acme and tell me if anything's broken."
Each tool call is printed as it happens, then the agent's summary.
To have something to fix, run `uv run kit break healthcare` first.
"""

import asyncio
import json
import os
import sys

from anthropic import AsyncAnthropic
from anthropic.lib.tools.mcp import async_mcp_tool
from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

DEFAULT_REQUEST = "Get a healthcare demo ready for Acme and tell me if anything's broken."
MODEL = os.environ.get("KIT_LLM_MODEL", "claude-opus-5-5")

SYSTEM_PROMPT = """\
You are a demo operations assistant for a sales team. You prepare HubSpot demo
environments with the demo-readiness-kit tools and report back to the rep.
Follow the server's instructions for the order of steps. Your final answer is
short and plain: ready or not, what was broken and how it was fixed, and for
anything still failing, the fallback the rep should use on the call.
"""


async def main(request: str) -> None:
    load_dotenv(".env")
    server = StdioServerParameters(command=sys.executable, args=["-m", "kit.cli", "mcp"])
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            started = await session.initialize()
            tools = (await session.list_tools()).tools
            print(f"Connected to MCP server '{started.server_info.name}' with {len(tools)} tools.")
            print(f"\nRep: {request}\n")

            runner = AsyncAnthropic().beta.messages.tool_runner(
                model=MODEL,
                max_tokens=16000,
                output_config={"effort": "medium"},
                # If the model declines a turn, the server retries on a fallback model.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                # The server's own instructions explain the workflow; pass them on to the model.
                system=SYSTEM_PROMPT + "\nServer instructions:\n" + (started.instructions or ""),
                tools=[async_mcp_tool(t, session) for t in tools],
                messages=[{"role": "user", "content": request}],
            )
            async for message in runner:
                for block in message.content:
                    if block.type == "tool_use":
                        print(f"  -> {block.name}({json.dumps(block.input)})")
                    elif block.type == "text" and message.stop_reason == "end_turn":
                        print(f"\nAgent:\n{block.text}")
                if message.stop_reason == "refusal":
                    print("\nThe model declined this request.")


if __name__ == "__main__":
    asyncio.run(main(" ".join(sys.argv[1:]) or DEFAULT_REQUEST))
