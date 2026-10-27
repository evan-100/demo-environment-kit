# demo-readiness-kit

Keeps demo environments realistic, current and verified before every call.

Work in progress. See `SPEC.md` for the plan and `PROGRESS.md` for status.

## Quickstart (so far)

```sh
uv sync
cp .env.example .env    # paste your HubSpot service key
uv run kit ping         # checks the token and lists deal pipeline stages
uv run pytest
```
