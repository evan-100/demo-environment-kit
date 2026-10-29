"""Reading and writing fixture files (fixtures/<story>/*.json).

Each record type gets its own file so a change to, say, one contact shows up
as a small, readable diff in git.
"""

import json
from pathlib import Path

from kit.schemas import FixtureSet

# Paths are relative to the repo root, where `kit` is run from.
FIXTURES_DIR = Path("fixtures")
RECORD_TYPES = ["companies", "contacts", "deals", "activities"]


def save_fixtures(fixtures: FixtureSet, base_dir: Path = FIXTURES_DIR) -> Path:
    directory = base_dir / fixtures.story_id
    directory.mkdir(parents=True, exist_ok=True)
    for record_type in RECORD_TYPES:
        records = [r.model_dump(exclude_none=True) for r in getattr(fixtures, record_type)]
        (directory / f"{record_type}.json").write_text(json.dumps(records, indent=2) + "\n")
    return directory


def load_fixtures(story_id: str, base_dir: Path = FIXTURES_DIR) -> FixtureSet:
    """Load and schema-check one story's fixtures. Raises pydantic.ValidationError if they're wrong."""
    directory = base_dir / story_id
    if not directory.is_dir():
        raise FileNotFoundError(f"no fixtures for '{story_id}' yet. Run `uv run kit generate {story_id}` first.")
    data = {"story_id": story_id}
    for record_type in RECORD_TYPES:
        data[record_type] = json.loads((directory / f"{record_type}.json").read_text())
    return FixtureSet.model_validate(data)


def load_other_fixture_sets(story_id: str, base_dir: Path = FIXTURES_DIR) -> list[FixtureSet]:
    """Fixtures of every *other* story, used to stop two stories reusing a company name."""
    others = []
    for directory in sorted(base_dir.glob("*/")):
        if directory.name != story_id and (directory / "companies.json").exists():
            others.append(load_fixtures(directory.name, base_dir))
    return others
