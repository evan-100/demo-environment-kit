"""Tests for the readiness checks, using a Snapshot built straight from the fixtures.

`snapshot_as_seeded` describes what HubSpot would hold right after a perfect
seed, so the golden paths can be evaluated without any API calls.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from kit.check import (
    Activity,
    CompanyView,
    DealView,
    PipelineDeal,
    Snapshot,
    evaluate,
    load_golden_path,
)
from kit.fixtures import load_fixtures
from kit.report import latest_report, write_reports
from kit.schemas import FixtureSet, load_story
from kit.seed import resolve, start_of_today

NOW = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)
PATHS = ["pipeline-review", "deal-deep-dive", "account-360", "rep-activity-feed"]


def snapshot_as_seeded(fixtures: FixtureSet, now: datetime) -> Snapshot:
    today = start_of_today(now)

    def activities_where(match) -> list[Activity]:
        return [
            Activity(type=a.type, at=resolve(a.when, a.kit_id, today), done=a.task_status == "completed"
                     if a.type == "task" else resolve(a.when, a.kit_id, today) <= now)
            for a in fixtures.activities if match(a)
        ]

    hero_ids = [c.kit_id for c in fixtures.companies if c.is_hero]
    deals = [
        DealView(kit_id=d.kit_id, found=True, name=d.name, stage=d.stage, expected_stage=d.stage,
                 close_date=resolve(d.close_date, d.kit_id, today), contact_count=len(d.contact_ids),
                 activities=activities_where(lambda a, d=d: a.deal_id == d.kit_id))
        for d in fixtures.deals if d.company_id in hero_ids
    ]
    companies = [
        CompanyView(kit_id=c, found=True, contact_count=sum(x.company_id == c for x in fixtures.contacts),
                    deal_count=sum(x.company_id == c for x in fixtures.deals),
                    activities=activities_where(lambda a, c=c: a.company_id == c))
        for c in hero_ids
    ]
    pipeline = [PipelineDeal(stage=d.stage, close_date=resolve(d.close_date, d.kit_id, today)) for d in fixtures.deals]
    return Snapshot(now=now, hero_deals=deals, hero_companies=companies, pipeline=pipeline,
                    rep_activities=activities_where(lambda a: True))


@pytest.fixture
def snapshot():
    return snapshot_as_seeded(load_fixtures("healthcare"), NOW)


def run(snapshot, name):
    return evaluate(load_golden_path(name), snapshot, "healthcare")


def failures(result):
    return [f"{r.subject}: {r.description}" for r in result.results if not r.passed]


def test_story_lists_only_existing_golden_paths():
    assert load_story("stories/healthcare.yaml").golden_paths == PATHS
    for name in PATHS:
        load_golden_path(name)  # raises if a file is missing or names an unknown check


@pytest.mark.parametrize("name", PATHS)
def test_every_golden_path_passes_on_fresh_data(snapshot, name):
    assert failures(run(snapshot, name)) == []


@pytest.mark.parametrize("seed_day", [NOW, NOW + timedelta(days=90), NOW + timedelta(days=400)])
def test_golden_paths_still_pass_when_seeded_months_later(seed_day):
    # Relative dates: the same fixtures pass whatever day they're seeded.
    later = snapshot_as_seeded(load_fixtures("healthcare"), seed_day)
    for name in PATHS:
        assert failures(run(later, name)) == []


def test_broken_hero_deal_fails_deep_dive_and_pipeline(snapshot):
    # What `kit break` does: wrong stage and a close date 5 days ago.
    acme = snapshot.hero_deals[0]
    broken = replace(acme, stage="appointmentscheduled", close_date=NOW - timedelta(days=5))
    snapshot.hero_deals[0] = broken
    snapshot.pipeline.append(PipelineDeal(stage=broken.stage, close_date=broken.close_date))

    deep_dive = run(snapshot, "deal-deep-dive")
    assert not deep_dive.passed
    assert failures(deep_dive) == [
        "hc-acme-deal-01: stage matches the story",
        "hc-acme-deal-01: close date is in the future",
    ]
    assert failures(run(snapshot, "pipeline-review")) == ["pipeline: no open deal past its close date"]
    assert deep_dive.fallback == "fallbacks/healthcare/deal-deep-dive.md" and deep_dive.fallback_exists


def test_missing_hero_deal_fails_every_assertion(snapshot):
    snapshot.hero_deals[0] = DealView(kit_id="hc-acme-deal-01", found=False, expected_stage="contractsent")
    result = run(snapshot, "deal-deep-dive")
    acme = [r for r in result.results if r.subject == "hc-acme-deal-01"]
    assert acme and all(not r.passed and r.detail == "record not found in HubSpot" for r in acme)


def test_stale_timeline_fails(snapshot):
    for deal in snapshot.hero_deals:
        deal.activities = [replace(a, at=a.at - timedelta(days=60)) for a in deal.activities]
    assert "hc-acme-deal-01: newest activity under 10 days old" in failures(run(snapshot, "deal-deep-dive"))


def test_thin_pipeline_fails(snapshot):
    snapshot.pipeline = snapshot.pipeline[:10]
    assert "pipeline: at least 30 open deals" in failures(run(snapshot, "pipeline-review"))


def test_reports_are_written_and_latest_is_found(snapshot, tmp_path, monkeypatch):
    results = [run(snapshot, name) for name in PATHS]
    monkeypatch.chdir(tmp_path)  # write reports into a scratch folder, not the repo
    json_path, md_path = write_reports("healthcare", NOW, results)
    assert latest_report("healthcare")["summary"] == "4/4 golden paths pass"
    assert "| Deal deep-dive | PASS |" in md_path.read_text()
