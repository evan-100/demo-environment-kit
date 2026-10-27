"""`kit check`: prove a demo's golden paths work before a rep goes live.

Three parts:
1. **Golden paths** are YAML files in checks/. Each lists assertions about one
   thing a rep shows (the pipeline, a hero deal, a hero company, their own
   activity feed), plus a fallback and a remediation for when it fails.
2. **gather()** reads HubSpot once and builds a Snapshot: plain Python objects
   describing what the rep would see.
3. **evaluate()** runs each assertion against the snapshot. No API calls here,
   so the rules are easy to test and to read.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Literal

import yaml
from pydantic import BaseModel, ConfigDict

from kit.hubspot import KIT_ID
from kit.schemas import CLOSED_DEAL_STAGES, OPEN_DEAL_STAGES, FixtureSet
from kit.seed import ACTIVITY_OBJECT_TYPES, DEFAULT_PIPELINE

CHECKS_DIR = Path("checks")

# --- 1. Golden path files ----------------------------------------------------------------


class Assertion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    check: str
    min: int | None = None
    within_days: int | None = None
    days: int | None = None


class GoldenPath(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    title: str
    rep_shows: str
    scope: Literal["pipeline", "hero_deals", "hero_companies", "rep_feed"]
    assertions: list[Assertion]
    fallback: str  # "{story}" is replaced with the story id
    remediation: Literal["reseed"]


def load_golden_path(name: str, checks_dir: Path = CHECKS_DIR) -> GoldenPath:
    path = GoldenPath.model_validate(yaml.safe_load((checks_dir / f"{name}.yaml").read_text()))
    for assertion in path.assertions:
        if assertion.check not in ASSERTIONS[path.scope]:
            raise ValueError(f"{name}: '{assertion.check}' isn't a known check for scope '{path.scope}'")
    return path


# --- 2. What the rep sees ---------------------------------------------------------------------


@dataclass
class Activity:
    type: str  # note, call, email, meeting, task
    at: datetime  # hs_timestamp (for tasks: the due date)
    done: bool  # tasks: completed; meetings: not scheduled; others: always true


@dataclass
class DealView:
    kit_id: str
    found: bool
    name: str = ""
    stage: str = ""
    expected_stage: str = ""
    close_date: datetime | None = None
    contact_count: int = 0
    activities: list[Activity] = field(default_factory=list)


@dataclass
class CompanyView:
    kit_id: str
    found: bool
    name: str = ""
    contact_count: int = 0
    deal_count: int = 0
    activities: list[Activity] = field(default_factory=list)


@dataclass
class PipelineDeal:
    stage: str
    close_date: datetime | None
    # HubSpot's own "next activity date" (from upcoming tasks and meetings); empty shows as "No upcoming".
    next_activity: datetime | None = None


@dataclass
class Snapshot:
    now: datetime
    hero_deals: list[DealView]
    hero_companies: list[CompanyView]
    pipeline: list[PipelineDeal]
    rep_activities: list[Activity]


ACTIVITY_PROPERTIES = ["hs_timestamp", "hs_task_status", "hs_meeting_outcome"]


def parse_time(value: str | None) -> datetime | None:
    """HubSpot dates look like '2026-09-15T21:30:00Z' or '...21:30:00.123Z'."""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def to_activity(object_type: str, properties: dict) -> Activity | None:
    at = parse_time(properties.get("hs_timestamp"))
    if at is None:
        return None
    kind = next(k for k, v in ACTIVITY_OBJECT_TYPES.items() if v == object_type)
    if kind == "task":
        done = properties.get("hs_task_status") == "COMPLETED"
    elif kind == "meeting":
        done = (properties.get("hs_meeting_outcome") or "").upper() != "SCHEDULED"
    else:
        done = True
    return Activity(type=kind, at=at, done=done)


def linked_activities(hubspot, from_type: str, ids: list[str]) -> dict[str, list[Activity]]:
    """For each record id, the activities (of every type) linked to it."""
    result = {i: [] for i in ids}
    if not ids:
        return result
    for object_type in ACTIVITY_OBJECT_TYPES.values():
        links = hubspot.linked_ids(from_type, ids, object_type)
        activity_ids = sorted({a for linked in links.values() for a in linked})
        if not activity_ids:
            continue
        by_id = {}
        for record in hubspot.read_by_ids(object_type, activity_ids, ACTIVITY_PROPERTIES):
            if activity := to_activity(object_type, record["properties"]):
                by_id[record["id"]] = activity
        for record_id, linked in links.items():
            result[record_id] += [by_id[a] for a in linked if a in by_id]
    return result


def hubspot_ms(moment: datetime) -> str:
    return str(int(moment.timestamp() * 1000))


def gather(hubspot, fixtures: FixtureSet, owner_id: str, now: datetime) -> Snapshot:
    """Read everything the golden paths look at, in as few calls as practical."""
    hero_company_ids = [c.kit_id for c in fixtures.companies if c.is_hero]
    hero_deals = [d for d in fixtures.deals if d.company_id in hero_company_ids]

    # Hero deals: stage, close date, people, activities.
    found = {r["properties"][KIT_ID]: r for r in hubspot.read_by_ids(
        "deals", [d.kit_id for d in hero_deals], ["dealname", "dealstage", "closedate", KIT_ID], id_property=KIT_ID)}
    deal_ids = [r["id"] for r in found.values()]
    deal_contacts = hubspot.linked_ids("deals", deal_ids, "contacts") if deal_ids else {}
    deal_activities = linked_activities(hubspot, "deals", deal_ids)
    deal_views = []
    for deal in hero_deals:
        record = found.get(deal.kit_id)
        if record is None:
            deal_views.append(DealView(kit_id=deal.kit_id, found=False, expected_stage=deal.stage))
            continue
        p = record["properties"]
        deal_views.append(DealView(
            kit_id=deal.kit_id, found=True, name=p.get("dealname") or "", stage=p.get("dealstage") or "",
            expected_stage=deal.stage, close_date=parse_time(p.get("closedate")),
            contact_count=len(deal_contacts.get(record["id"], [])), activities=deal_activities[record["id"]],
        ))

    # Hero companies: people, deals, activities.
    found = {r["properties"][KIT_ID]: r for r in hubspot.read_by_ids(
        "companies", hero_company_ids, ["name", KIT_ID], id_property=KIT_ID)}
    company_ids = [r["id"] for r in found.values()]
    company_contacts = hubspot.linked_ids("companies", company_ids, "contacts") if company_ids else {}
    company_deals = hubspot.linked_ids("companies", company_ids, "deals") if company_ids else {}
    company_activities = linked_activities(hubspot, "companies", company_ids)
    company_views = []
    for kit_id in hero_company_ids:
        record = found.get(kit_id)
        if record is None:
            company_views.append(CompanyView(kit_id=kit_id, found=False))
            continue
        company_views.append(CompanyView(
            kit_id=kit_id, found=True, name=record["properties"].get("name") or "",
            contact_count=len(company_contacts.get(record["id"], [])),
            deal_count=len(company_deals.get(record["id"], [])),
            activities=company_activities[record["id"]],
        ))

    # The whole pipeline, as the rep's deals board shows it (not just kit records).
    pipeline = [
        PipelineDeal(stage=r["properties"].get("dealstage") or "",
                     close_date=parse_time(r["properties"].get("closedate")),
                     next_activity=parse_time(r["properties"].get("notes_next_activity_date")))
        for r in hubspot.search("deals", [{"propertyName": "pipeline", "operator": "EQ", "value": DEFAULT_PIPELINE}],
                                ["dealstage", "closedate", "notes_next_activity_date"])
    ]

    # The rep's own activities from the last 30 days and anything scheduled after that.
    rep_activities = []
    for object_type in ACTIVITY_OBJECT_TYPES.values():
        filters = [
            {"propertyName": "hubspot_owner_id", "operator": "EQ", "value": owner_id},
            {"propertyName": "hs_timestamp", "operator": "GTE", "value": hubspot_ms(now - timedelta(days=30))},
        ]
        for record in hubspot.search(object_type, filters, ACTIVITY_PROPERTIES):
            if activity := to_activity(object_type, record["properties"]):
                rep_activities.append(activity)

    return Snapshot(now=now, hero_deals=deal_views, hero_companies=company_views,
                    pipeline=pipeline, rep_activities=rep_activities)


# --- 3. Assertions -----------------------------------------------------------------------------
# Each takes (subject, assertion, now) and returns (passed, detail shown in the report).


def past_activities(activities: list[Activity], now: datetime, within_days: int | None = None) -> list[Activity]:
    oldest = now - timedelta(days=within_days) if within_days else None
    return [a for a in activities if a.at <= now and (oldest is None or a.at >= oldest)]


def stage_matches_story(deal: DealView, a: Assertion, now: datetime):
    return deal.stage == deal.expected_stage, f"stage is '{deal.stage}', the story says '{deal.expected_stage}'"


def close_date_in_future(deal: DealView, a: Assertion, now: datetime):
    if deal.stage in CLOSED_DEAL_STAGES:
        return True, "deal is closed"
    if deal.close_date is None:
        return False, "no close date"
    # Whole days, rounded toward zero (timedelta.days would turn "5 days ago" into -6).
    days = int((deal.close_date - now).total_seconds() / 86400)
    return deal.close_date > now, f"closes in {days} days" if days >= 0 else f"close date was {-days} days ago"


def min_contacts(subject, a: Assertion, now: datetime):
    return subject.contact_count >= a.min, f"{subject.contact_count} contacts linked"


def min_deals(company: CompanyView, a: Assertion, now: datetime):
    return company.deal_count >= a.min, f"{company.deal_count} deals linked"


def min_recent_activities(subject, a: Assertion, now: datetime):
    activities = subject if isinstance(subject, list) else subject.activities
    count = len(past_activities(activities, now, a.within_days))
    return count >= a.min, f"{count} activities in the last {a.within_days} days"


def newest_activity_within(subject, a: Assertion, now: datetime):
    past = past_activities(subject.activities, now)
    if not past:
        return False, "no past activities"
    age = (now - max(x.at for x in past)).days
    return age <= a.days, f"newest activity is {age} days old"


def min_upcoming(activities: list[Activity], a: Assertion, now: datetime):
    count = len([x for x in activities if x.at > now and not x.done and x.type in ("task", "meeting")])
    return count >= a.min, f"{count} upcoming tasks or meetings"


def min_open_deals(pipeline: list[PipelineDeal], a: Assertion, now: datetime):
    count = len([d for d in pipeline if d.stage in OPEN_DEAL_STAGES])
    return count >= a.min, f"{count} open deals"


def min_open_stages(pipeline: list[PipelineDeal], a: Assertion, now: datetime):
    stages = {d.stage for d in pipeline if d.stage in OPEN_DEAL_STAGES}
    return len(stages) >= a.min, f"open deals in {len(stages)} stages"


def no_overdue_open_deals(pipeline: list[PipelineDeal], a: Assertion, now: datetime):
    overdue = [d for d in pipeline if d.stage in OPEN_DEAL_STAGES and d.close_date and d.close_date < now]
    return not overdue, f"{len(overdue)} open deals past their close date"


def open_deals_have_next_step(pipeline: list[PipelineDeal], a: Assertion, now: datetime):
    missing = [d for d in pipeline if d.stage in OPEN_DEAL_STAGES and not (d.next_activity and d.next_activity > now)]
    return not missing, f"{len(missing)} open deals with no upcoming task or meeting"


ASSERTIONS: dict[str, dict[str, Callable]] = {
    "hero_deals": {f.__name__: f for f in [stage_matches_story, close_date_in_future, min_contacts,
                                            min_recent_activities, newest_activity_within]},
    "hero_companies": {f.__name__: f for f in [min_contacts, min_deals, newest_activity_within]},
    "pipeline": {f.__name__: f for f in [min_open_deals, min_open_stages, no_overdue_open_deals,
                                          open_deals_have_next_step]},
    "rep_feed": {f.__name__: f for f in [min_recent_activities, min_upcoming]},
}

# Plain-language names for the report.
DESCRIPTIONS = {
    "stage_matches_story": "stage matches the story",
    "close_date_in_future": "close date is in the future",
    "min_contacts": "at least {min} contacts",
    "min_deals": "at least {min} deals",
    "min_recent_activities": "at least {min} activities in the last {within_days} days",
    "newest_activity_within": "newest activity under {days} days old",
    "min_upcoming": "at least {min} upcoming tasks or meetings",
    "min_open_deals": "at least {min} open deals",
    "min_open_stages": "open deals in at least {min} stages",
    "no_overdue_open_deals": "no open deal past its close date",
    "open_deals_have_next_step": "every open deal has an upcoming task or meeting",
}


# --- Running a golden path --------------------------------------------------------------------


@dataclass
class AssertionResult:
    subject: str  # e.g. "hc-acme-deal-01" or "pipeline"
    description: str
    passed: bool
    detail: str


@dataclass
class PathResult:
    id: str
    title: str
    rep_shows: str
    passed: bool
    results: list[AssertionResult]
    fallback: str
    fallback_exists: bool
    remediation: str


def subjects_for(path: GoldenPath, snapshot: Snapshot) -> list[tuple[str, object, bool]]:
    """(label, subject, found) for each thing this path checks."""
    if path.scope == "hero_deals":
        return [(d.kit_id, d, d.found) for d in snapshot.hero_deals]
    if path.scope == "hero_companies":
        return [(c.kit_id, c, c.found) for c in snapshot.hero_companies]
    if path.scope == "pipeline":
        return [("pipeline", snapshot.pipeline, True)]
    return [("rep feed", snapshot.rep_activities, True)]


def evaluate(path: GoldenPath, snapshot: Snapshot, story: str) -> PathResult:
    results = []
    for label, subject, found in subjects_for(path, snapshot):
        for assertion in path.assertions:
            description = DESCRIPTIONS[assertion.check].format(**assertion.model_dump())
            if not found:
                results.append(AssertionResult(label, description, False, "record not found in HubSpot"))
                continue
            passed, detail = ASSERTIONS[path.scope][assertion.check](subject, assertion, snapshot.now)
            results.append(AssertionResult(label, description, passed, detail))

    fallback = path.fallback.format(story=story)
    return PathResult(
        id=path.id, title=path.title, rep_shows=path.rep_shows,
        passed=bool(results) and all(r.passed for r in results), results=results,
        fallback=fallback, fallback_exists=Path(fallback).exists(), remediation=path.remediation,
    )


def run_checks(hubspot, fixtures: FixtureSet, path_names: list[str], owner_id: str,
               now: datetime | None = None) -> list[PathResult]:
    now = now or datetime.now(timezone.utc)
    paths = [load_golden_path(name) for name in path_names]
    snapshot = gather(hubspot, fixtures, owner_id, now)
    return [evaluate(path, snapshot, fixtures.story_id) for path in paths]
