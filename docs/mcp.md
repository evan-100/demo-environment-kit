# Using the kit from an agent (MCP)

`kit mcp` runs an MCP server over stdio. Any MCP-capable agent or desktop app can use it.

## Connect a client

Most MCP clients take a server entry like this (use your own path to the repo):

```json
{
  "mcpServers": {
    "demo-environment-kit": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/demo-environment-kit", "kit", "mcp"]
    }
  }
}
```

The server finds the repo and its `.env` by itself, so it works whatever folder the client starts it from.

## Tools

| Tool | What it does | Notes |
|---|---|---|
| `list_stories` | Every story, whether it's generated and loaded in HubSpot, last readiness result | read-only |
| `create_story(description)` | Drafts `stories/<id>.yaml` from a description of the demo | one LLM call; review before generating |
| `generate_story(story)` | New fixtures from the story file via the LLM | ~3 min, or ~15 s when reusing a background; costs API credits |
| `seed_story(story, dry_run)` | Load fixtures into HubSpot | safe to repeat |
| `check_story(story, fix)` | Run the golden paths, write a report; `fix=true` repairs and rechecks | |
| `reset_story(story, reseed, confirm)` | Delete the story's kit records (only those) | destructive: needs `confirm=true` |
| `get_last_report(story)` | Latest report, including the Markdown | read-only |

The server also sends the workflow to the agent: **check first**, fix if anything fails, and seed only
when the records aren't there. Seeding first would quietly repair a broken demo before anyone saw it.

## The demo scenario

```sh
uv run kit break healthcare              # optional: give the agent something to find
uv run python examples/get_acme_ready.py
```

The script starts the server, hands its tools to Claude, and asks: *"Get a healthcare demo ready for Acme
and tell me if anything's broken."* It prints each tool call and the agent's summary.
