"""`kit seed`: load a story's fixtures into HubSpot.

Two steps:
1. build_plan() turns fixtures into the exact records and links to write.
   It doesn't touch HubSpot, so `--dry-run` just prints the plan.
2. apply_plan() writes them: make sure the demo_kit_* properties exist,
   upsert records (create or update, matched on demo_kit_id), then link them.

Why running it twice is safe: records are matched on demo_kit_id,
so a second run updates the same records instead of creating copies. And
dates are worked out from the start of today, so two runs on the same day
write identical values.

Why a failure halfway never leaves unknown state behind: every record is
tagged the moment it's written, so `kit reset` can always find it, and
rerunning `kit seed` simply finishes the job.
"""

import html
import os
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from kit.hubspot import KIT_ID, KIT_STORY
from kit.schemas import CLOSED_DEAL_STAGES, ActivityFixture, DealFixture, FixtureSet, RelativeDate

# Write order matters: companies before the contacts and deals that link to them.
RECORD_OBJECT_TYPES = ["companies", "contacts", "deals"]
ACTIVITY_OBJECT_TYPES = {"note": "notes", "call": "calls", "email": "emails", "meeting": "meetings", "task": "tasks"}
OBJECT_TYPES = RECORD_OBJECT_TYPES + list(ACTIVITY_OBJECT_TYPES.values())

# Built-in property group to put our demo_kit_* properties in, per object type.
PROPERTY_GROUPS = {
    "companies": "companyinformation",
    "contacts": "contactinformation",
    "deals": "dealinformation",
    "notes": "noteinformation",
    "calls": "callinformation",
    "emails": "emailinformation",
    "meetings": "meetingeventinformation",
    "tasks": "taskinformation",
}

# HubSpot association type IDs (docs/hubspot-notes.md §7). Direction matters.
CONTACT_TO_PRIMARY_COMPANY = 1
DEAL_TO_CONTACT = 3
DEAL_TO_PRIMARY_COMPANY = 5
ACTIVITY_LINK_TYPES = {  # activity object -> {target object: type id}
    "notes": {"companies": 190, "contacts": 202, "deals": 214},
    "calls": {"companies": 182, "contacts": 194, "deals": 206},
    "emails": {"companies": 186, "contacts": 198, "deals": 210},
    "meetings": {"companies": 188, "contacts": 200, "deals": 212},
    "tasks": {"companies": 192, "contacts": 204, "deals": 216},
}

DEFAULT_PIPELINE = "default"

# Roughly how many days ago a deal at each open stage was created, so the board
# doesn't show every deal as "created today". HubSpot lets us set this on deals
# only; for companies and contacts `createdate` is read-only.
MIN_DEAL_AGE_DAYS = {
    "appointmentscheduled": 3,
    "qualifiedtobuy": 10,
    "presentationscheduled": 20,
    "decisionmakerboughtin": 30,
    "contractsent": 40,
}


@dataclass
class Link:
    from_type: str
    from_kit_id: str
    to_type: str
    to_kit_id: str
    type_id: int


@dataclass
class SeedPlan:
    story: str
    # object type -> [{"kit_id": ..., "properties": {...}}]
    records: dict[str, list[dict]] = field(default_factory=dict)
    links: list[Link] = field(default_factory=list)

    def counts(self) -> dict:
        counts = {object_type: len(records) for object_type, records in self.records.items() if records}
        counts["links"] = len(self.links)
        return counts


@dataclass
class SeedResult:
    counts: dict
    errors: list[str]


class SeedError(Exception):
    pass


# --- Dates ------------------------------------------------------------------------------


def start_of_today(now: datetime) -> datetime:
    return now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def working_hours_time(kit_id: str) -> timedelta:
    """A stable time of day for a record, between 13:00 and 21:45 UTC (US working hours).

    Derived from the kit ID, so the same record always lands at the same time
    and activities don't all sit at midnight.
    """
    number = zlib.crc32(kit_id.encode())
    return timedelta(hours=13 + number % 9, minutes=15 * (number // 9 % 4))


def resolve(when: RelativeDate, kit_id: str, today: datetime) -> datetime:
    return when.resolve(today) + working_hours_time(kit_id)


def hubspot_time(moment: datetime) -> str:
    """HubSpot accepts UTC timestamps like 2026-10-26T15:30:00.000Z."""
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def deal_age_days(deal: DealFixture, activities: list[ActivityFixture]) -> int:
    """How many days ago the deal was created: fits its stage, and comes before its first activity.

    A small spread derived from the kit ID keeps deals from all sharing one date,
    while keeping the value identical on every seed.
    """
    spread = zlib.crc32(deal.kit_id.encode()) % 15
    if deal.stage in CLOSED_DEAL_STAGES:
        age = (deal.close_date.days_ago or 0) + 30 + spread  # about a month before it closed
    else:
        age = MIN_DEAL_AGE_DAYS[deal.stage] + spread
    oldest_activity = max((a.when.days_ago or 0 for a in activities if a.deal_id == deal.kit_id), default=0)
    return max(age, oldest_activity + 1 + spread % 5)


# --- Building the plan --------------------------------------------------------------------


def tags(kit_id: str, story: str) -> dict:
    return {KIT_ID: kit_id, KIT_STORY: story}


def activity_properties(activity: ActivityFixture, today: datetime, now: datetime) -> dict:
    """The HubSpot fields for one activity. Each type has its own field names."""
    moment = resolve(activity.when, activity.kit_id, today)
    in_past = moment <= now
    properties = {"hs_timestamp": hubspot_time(moment)}

    if activity.type == "note":
        # Notes have no title field, so the title goes in bold at the top of the body.
        properties["hs_note_body"] = (
            f"<p><strong>{html.escape(activity.title)}</strong></p><p>{html.escape(activity.body)}</p>"
        )
    elif activity.type == "call":
        properties |= {
            "hs_call_title": activity.title,
            "hs_call_body": activity.body,
            "hs_call_direction": (activity.direction or "outbound").upper(),
            "hs_call_duration": str((activity.duration_minutes or 15) * 60_000),  # milliseconds
        }
        if in_past:
            properties["hs_call_status"] = "COMPLETED"
    elif activity.type == "email":
        properties |= {
            "hs_email_subject": activity.title,
            "hs_email_text": activity.body,
            "hs_email_direction": "INCOMING_EMAIL" if activity.direction == "inbound" else "EMAIL",
            "hs_email_status": "SENT",
        }
    elif activity.type == "meeting":
        end = moment + timedelta(minutes=activity.duration_minutes or 30)
        properties |= {
            "hs_meeting_title": activity.title,
            "hs_meeting_body": activity.body,
            "hs_meeting_start_time": hubspot_time(moment),
            "hs_meeting_end_time": hubspot_time(end),
            "hs_meeting_outcome": "COMPLETED" if in_past else "SCHEDULED",
        }
    elif activity.type == "task":
        properties |= {
            "hs_task_subject": activity.title,
            "hs_task_body": activity.body,
            "hs_task_status": (activity.task_status or "not_started").upper(),
        }
    return properties


def build_plan(fixtures: FixtureSet, now: datetime, owner_id: str | None = None) -> SeedPlan:
    """Everything to write. `owner_id` is the HubSpot user who owns every record (None in a dry run)."""
    story = fixtures.story_id
    today = start_of_today(now)
    plan = SeedPlan(story=story, records={object_type: [] for object_type in OBJECT_TYPES})
    owner = {"hubspot_owner_id": owner_id} if owner_id else {}

    for company in fixtures.companies:
        plan.records["companies"].append({"kit_id": company.kit_id, "properties": {
            "name": company.name,
            "domain": company.domain,
            "city": company.city,
            "state": company.state,
            "numberofemployees": str(company.employees),
            "description": company.description,
        } | owner | tags(company.kit_id, story)})

    for contact in fixtures.contacts:
        plan.records["contacts"].append({"kit_id": contact.kit_id, "properties": {
            "firstname": contact.first_name,
            "lastname": contact.last_name,
            "email": contact.email,
            "jobtitle": contact.job_title,
            "phone": contact.phone,
        } | owner | tags(contact.kit_id, story)})
        plan.links.append(Link("contacts", contact.kit_id, "companies", contact.company_id, CONTACT_TO_PRIMARY_COMPANY))

    for deal in fixtures.deals:
        plan.records["deals"].append({"kit_id": deal.kit_id, "properties": {
            "dealname": deal.name,
            "pipeline": DEFAULT_PIPELINE,
            "dealstage": deal.stage,
            "amount": str(deal.amount),
            "closedate": hubspot_time(resolve(deal.close_date, deal.kit_id, today)),
            "createdate": hubspot_time(resolve(
                RelativeDate(days_ago=deal_age_days(deal, fixtures.activities)), deal.kit_id, today)),
        } | owner | tags(deal.kit_id, story)})
        plan.links.append(Link("deals", deal.kit_id, "companies", deal.company_id, DEAL_TO_PRIMARY_COMPANY))
        for contact_id in deal.contact_ids:
            plan.links.append(Link("deals", deal.kit_id, "contacts", contact_id, DEAL_TO_CONTACT))

    for activity in fixtures.activities:
        object_type = ACTIVITY_OBJECT_TYPES[activity.type]
        plan.records[object_type].append({
            "kit_id": activity.kit_id,
            "properties": activity_properties(activity, today, now) | owner | tags(activity.kit_id, story),
        })
        link_types = ACTIVITY_LINK_TYPES[object_type]
        targets = [("contacts", contact_id) for contact_id in activity.contact_ids]
        if activity.company_id:
            targets.append(("companies", activity.company_id))
        if activity.deal_id:
            targets.append(("deals", activity.deal_id))
        for target_type, target_kit_id in targets:
            plan.links.append(Link(object_type, activity.kit_id, target_type, target_kit_id, link_types[target_type]))

    return plan


# --- Applying the plan -----------------------------------------------------------------------


def pick_owner(hubspot) -> str:
    """The rep who owns the demo records: KIT_OWNER_ID from .env, or else the account's first user.

    HubSpot requires an owner on meetings, and a rep's demo looks wrong with unowned deals.
    """
    if os.environ.get("KIT_OWNER_ID"):
        return os.environ["KIT_OWNER_ID"]
    people = [o for o in hubspot.owners() if not o.get("archived")]
    if not people:
        raise SeedError("the HubSpot account has no users to own the records")
    return people[0]["id"]


def ensure_properties(hubspot) -> None:
    """Create demo_kit_id (unique) and demo_kit_story on every object type, if missing."""
    for object_type in OBJECT_TYPES:
        for name, label, unique in [(KIT_ID, "Demo kit ID", True), (KIT_STORY, "Demo kit story", False)]:
            hubspot.create_property_if_missing(object_type, {
                "name": name,
                "label": label,
                "type": "string",
                "fieldType": "text",
                "groupName": PROPERTY_GROUPS[object_type],
                "hasUniqueValue": unique,
            })


def apply_plan(plan: SeedPlan, hubspot) -> SeedResult:
    errors: list[str] = []
    ensure_properties(hubspot)

    # 1. Create or update every record, matched on demo_kit_id.
    for object_type in OBJECT_TYPES:
        if plan.records[object_type]:
            errors += [f"{object_type}: {e}" for e in hubspot.batch_upsert(object_type, plan.records[object_type])]

    # 2. Look up HubSpot's own IDs for our records, which links need.
    hubspot_id = {}  # (object type, kit_id) -> HubSpot id
    for object_type in OBJECT_TYPES:
        kit_ids = [r["kit_id"] for r in plan.records[object_type]]
        if kit_ids:
            for record in hubspot.read_by_kit_ids(object_type, kit_ids):
                hubspot_id[(object_type, record["kit_id"])] = record["id"]

    # 3. Link them, grouped by pair of object types (one API endpoint per pair).
    grouped: dict[tuple[str, str], list[tuple[str, str, int]]] = {}
    for link in plan.links:
        from_id = hubspot_id.get((link.from_type, link.from_kit_id))
        to_id = hubspot_id.get((link.to_type, link.to_kit_id))
        if from_id is None or to_id is None:
            errors.append(f"can't link {link.from_kit_id} -> {link.to_kit_id}: one of them wasn't written")
            continue
        grouped.setdefault((link.from_type, link.to_type), []).append((from_id, to_id, link.type_id))
    for (from_type, to_type), links in grouped.items():
        errors += [f"{from_type}->{to_type}: {e}" for e in hubspot.associate(from_type, to_type, links)]

    return SeedResult(counts=plan.counts(), errors=errors)


def kit_ids_by_object_type(fixtures: FixtureSet) -> dict[str, list[str]]:
    """Every kit ID in the fixtures, grouped by HubSpot object type. Used by reset."""
    plan = build_plan(fixtures, datetime.now(timezone.utc))
    return {object_type: [r["kit_id"] for r in records] for object_type, records in plan.records.items()}


def break_hero_deal(fixtures: FixtureSet, hubspot, now: datetime) -> str:
    """Deliberately damage the first hero deal: wrong stage, close date in the past.

    Used to show (and test) that `kit check` catches a broken demo and
    `kit check --fix` repairs it. Returns the deal's kit ID.
    """
    hero_company = next(c.kit_id for c in fixtures.companies if c.is_hero)
    deal = next(d for d in fixtures.deals if d.company_id == hero_company)
    hubspot.batch_upsert("deals", [{"kit_id": deal.kit_id, "properties": {
        "dealstage": "appointmentscheduled",
        "closedate": hubspot_time(now - timedelta(days=5)),
    }}])
    return deal.kit_id
