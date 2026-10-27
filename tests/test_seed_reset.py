"""Tests for seeding and reset against an in-memory fake HubSpot.

FakeHubSpot has the same methods as HubSpotClient (the ones seed.py and
reset.py use) and keeps records in dictionaries, so these tests show what
the kit does to an account without calling the real API.
"""

import copy
import os
from datetime import datetime, timedelta, timezone

import pytest
from dotenv import load_dotenv

from kit.fixtures import load_fixtures
from kit.hubspot import KIT_ID, KIT_STORY
from kit.reset import reset
from kit.seed import OBJECT_TYPES, apply_plan, build_plan, pick_owner

load_dotenv()

NOW = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)


class FakeHubSpot:
    def __init__(self):
        self.records = {object_type: {} for object_type in OBJECT_TYPES}  # id -> properties
        self.links = set()  # (from type, from id, to type, to id, type id)
        self.properties = set()  # (object type, property name)
        self.next_id = 1000
        self.calls = 0
        # Simulate a broken search that returns every record, tagged or not.
        self.search_returns_everything = False

    # Helpers for tests.
    def add(self, object_type: str, properties: dict) -> str:
        self.next_id += 1
        self.records[object_type][str(self.next_id)] = dict(properties)
        return str(self.next_id)

    def snapshot(self):
        return copy.deepcopy((self.records, self.links))

    def with_kit_id(self, object_type: str, kit_id: str):
        return next((i for i, p in self.records[object_type].items() if p.get(KIT_ID) == kit_id), None)

    def simplified(self, object_type: str, record_id: str) -> dict:
        p = self.records[object_type][record_id]
        return {"id": record_id, "kit_id": p.get(KIT_ID), "story": p.get(KIT_STORY)}

    # The same methods HubSpotClient has.
    def owners(self):
        self.calls += 1
        return [{"id": "77", "archived": False}]

    def create_property_if_missing(self, object_type, definition):
        self.calls += 1
        self.properties.add((object_type, definition["name"]))

    def batch_upsert(self, object_type, records):
        self.calls += 1
        for record in records:
            existing = self.with_kit_id(object_type, record["kit_id"])
            if existing:
                self.records[object_type][existing].update(record["properties"])
            else:
                self.add(object_type, record["properties"] | {KIT_ID: record["kit_id"]})
        return []

    def read_by_kit_ids(self, object_type, kit_ids):
        self.calls += 1
        ids = [self.with_kit_id(object_type, k) for k in kit_ids]
        return [self.simplified(object_type, i) for i in ids if i]

    def find_by_story(self, object_type, story):
        self.calls += 1
        return [
            self.simplified(object_type, i)
            for i, p in self.records[object_type].items()
            if self.search_returns_everything
            or (p.get(KIT_STORY) and (story is None or p[KIT_STORY] == story))
        ]

    def associate(self, from_type, to_type, links):
        self.calls += 1
        for from_id, to_id, type_id in links:
            self.links.add((from_type, from_id, to_type, to_id, type_id))
        return []

    def archive(self, object_type, ids):
        self.calls += 1
        for record_id in ids:
            del self.records[object_type][record_id]
        self.links = {l for l in self.links if not ({(l[0], l[1]), (l[2], l[3])} & {(object_type, i) for i in ids})}


@pytest.fixture
def fixtures():
    return load_fixtures("healthcare")


@pytest.fixture
def hubspot():
    return FakeHubSpot()


def seed(fixtures, hubspot, now=NOW):
    result = apply_plan(build_plan(fixtures, now, owner_id=pick_owner(hubspot)), hubspot)
    assert result.errors == []
    return result


def add_real_customer_data(hubspot: FakeHubSpot) -> list[tuple[str, str]]:
    """Records the kit did NOT create: a real customer and another story's demo."""
    return [
        ("companies", hubspot.add("companies", {"name": "A Real Customer Inc"})),
        ("contacts", hubspot.add("contacts", {"email": "buyer@realcustomer.example.com"})),
        ("deals", hubspot.add("deals", {"dealname": "Real renewal", KIT_ID: "looks-like-ours"})),
        ("companies", hubspot.add("companies", {"name": "Other Demo", KIT_ID: "fs-x", KIT_STORY: "finserv"})),
    ]


# --- Seeding -------------------------------------------------------------------------


def test_seed_writes_every_record_tagged(fixtures, hubspot):
    seed(fixtures, hubspot)
    assert len(hubspot.records["companies"]) == len(fixtures.companies)
    assert len(hubspot.records["deals"]) == len(fixtures.deals)
    activity_count = sum(len(hubspot.records[t]) for t in ["notes", "calls", "emails", "meetings", "tasks"])
    assert activity_count == len(fixtures.activities)
    for records in hubspot.records.values():
        for properties in records.values():
            assert properties[KIT_STORY] == "healthcare" and properties[KIT_ID].startswith("hc-")
            assert properties["hubspot_owner_id"] == "77"


def test_seeding_twice_changes_nothing(fixtures, hubspot):
    # Seeding twice in a row changes nothing.
    seed(fixtures, hubspot)
    first = hubspot.snapshot()
    seed(fixtures, hubspot, now=NOW + timedelta(hours=2))  # later the same day
    assert hubspot.snapshot() == first


def test_seed_links_hero_deal_to_its_people_and_company(fixtures, hubspot):
    seed(fixtures, hubspot)
    deal = hubspot.with_kit_id("deals", "hc-acme-deal-01")
    linked_contacts = {l[3] for l in hubspot.links if l[:2] == ("deals", deal) and l[2] == "contacts"}
    expected = {hubspot.with_kit_id("contacts", k) for k in ["hc-acme-champion", "hc-acme-blocker", "hc-acme-it-lead"]}
    assert linked_contacts == expected
    assert ("deals", deal, "companies", hubspot.with_kit_id("companies", "hc-acme-company"), 5) in hubspot.links


def test_activity_dates_stay_recent_on_any_seed_day(fixtures):
    # Data seeded on different days stays "recent".
    def newest_past_activity_age(now):
        plan = build_plan(fixtures, now)
        times = [
            datetime.strptime(r["properties"]["hs_timestamp"], "%Y-%m-%dT%H:%M:%S.000Z").replace(tzinfo=timezone.utc)
            for t in ["notes", "calls", "emails", "meetings", "tasks"] for r in plan.records[t]
        ]
        return min(now - t for t in times if t <= now)

    assert newest_past_activity_age(NOW) < timedelta(days=10)
    assert newest_past_activity_age(NOW + timedelta(days=200)) < timedelta(days=10)


def test_open_deals_never_look_overdue(fixtures):
    plan = build_plan(fixtures, NOW)
    for deal in plan.records["deals"]:
        if not deal["properties"]["dealstage"].startswith("closed"):
            assert deal["properties"]["closedate"] > NOW.strftime("%Y-%m-%dT%H:%M:%S")


# --- Reset: the safety test (reset never deletes untagged records) ----------------------


def test_reset_removes_only_tagged_records(fixtures, hubspot):
    others = add_real_customer_data(hubspot)
    seed(fixtures, hubspot)

    result = reset(hubspot, "healthcare", fixtures)

    for object_type, record_id in others:
        assert record_id in hubspot.records[object_type], "reset deleted a record it didn't create"
    assert sum(len(r) for r in hubspot.records.values()) == len(others)
    assert result.deleted["companies"] == len(fixtures.companies)


def test_reset_all_keeps_untagged_records(fixtures, hubspot):
    others = add_real_customer_data(hubspot)
    seed(fixtures, hubspot)
    reset(hubspot, None)
    remaining = {(t, i) for t, records in hubspot.records.items() for i in records}
    assert remaining == set(others[:3])  # the finserv demo went too; the three untagged stayed


def test_reset_refuses_untagged_records_even_if_search_returns_them(fixtures, hubspot):
    others = add_real_customer_data(hubspot)
    seed(fixtures, hubspot)
    hubspot.search_returns_everything = True  # a bug in search must not cause a wrong delete

    result = reset(hubspot, "healthcare", fixtures)

    for object_type, record_id in others:
        assert record_id in hubspot.records[object_type]
    assert result.skipped == len(others)


def test_reset_then_reseed_gives_a_fresh_copy(fixtures, hubspot):
    seed(fixtures, hubspot)
    reset(hubspot, "healthcare", fixtures)
    seed(fixtures, hubspot)
    assert len(hubspot.records["contacts"]) == len(fixtures.contacts)


def test_dry_run_plan_needs_no_hubspot(fixtures, hubspot):
    build_plan(fixtures, NOW)
    assert hubspot.calls == 0


# --- Live (opt-in) -------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.skipif(
    not (os.environ.get("HUBSPOT_TOKEN") and os.environ.get("KIT_LIVE_WRITE") == "1"),
    reason="writes to HubSpot; set KIT_LIVE_WRITE=1 and HUBSPOT_TOKEN to run",
)
def test_live_seed_twice_then_reset(fixtures):
    from kit.hubspot import HubSpotClient

    live = HubSpotClient()
    owner = pick_owner(live)
    for _ in range(2):
        assert apply_plan(build_plan(fixtures, datetime.now(timezone.utc), owner), live).errors == []
    ids = {t: sorted(r["id"] for r in live.read_by_kit_ids(t, [k for k in kits])) for t, kits in
           [("companies", [c.kit_id for c in fixtures.companies]), ("deals", [d.kit_id for d in fixtures.deals])]}
    assert len(ids["companies"]) == len(fixtures.companies)  # no duplicates after two seeds
    assert len(ids["deals"]) == len(fixtures.deals)
    reset(live, "healthcare", fixtures)
    assert live.read_by_kit_ids("companies", [c.kit_id for c in fixtures.companies]) == []
