"""`kit reset`: delete the records the kit created, and nothing else.

How records are found:
- a search for demo_kit_story = <story> (or "has any demo_kit_story" for --all), plus
- a direct lookup of every demo_kit_id in the story's fixtures, because search
  can lag a few seconds behind writes and would miss just-seeded records.

The safety rule: before anything is deleted, each record's own
demo_kit_story is checked again. A record without the tag, or with another
story's tag, is never deleted, even if a lookup returned it by mistake.
"""

from dataclasses import dataclass

from kit.schemas import FixtureSet
from kit.seed import OBJECT_TYPES, kit_ids_by_object_type

# Delete activities first, then deals and contacts, then companies (the reverse of seeding).
DELETE_ORDER = list(reversed(OBJECT_TYPES))


@dataclass
class ResetResult:
    deleted: dict[str, int]
    # Records a lookup returned that failed the safety check and were left alone.
    skipped: int


def is_safe_to_delete(record: dict, story: str | None) -> bool:
    if not record.get("story"):
        return False  # untagged: not ours
    return story is None or record["story"] == story


def find_kit_records(hubspot, object_type: str, story: str | None, kit_ids: list[str]) -> list[dict]:
    found = {r["id"]: r for r in hubspot.find_by_story(object_type, story)}
    if kit_ids:
        found |= {r["id"]: r for r in hubspot.read_by_kit_ids(object_type, kit_ids)}
    return list(found.values())


def reset(hubspot, story: str | None, fixtures: FixtureSet | None = None) -> ResetResult:
    """Delete one story's records (story="healthcare") or every story's (story=None)."""
    kit_ids = kit_ids_by_object_type(fixtures) if fixtures else {}
    deleted, skipped = {}, 0

    for object_type in DELETE_ORDER:
        records = find_kit_records(hubspot, object_type, story, kit_ids.get(object_type, []))
        safe_ids = [r["id"] for r in records if is_safe_to_delete(r, story)]
        skipped += len(records) - len(safe_ids)
        if safe_ids:
            hubspot.archive(object_type, safe_ids)
            deleted[object_type] = len(safe_ids)

    return ResetResult(deleted=deleted, skipped=skipped)
