"""Tests for the generator (with a fake LLM) and for fixture validation.

The fake LLM returns fixed drafts, so these tests are free, fast and never
call a real model. The live generator is exercised with `kit generate`.
"""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from kit.fixtures import load_fixtures, save_fixtures
from kit.generate import (
    DraftActivity,
    DraftContact,
    DraftDealName,
    FillerCompany,
    FillerContact,
    FillerDeal,
    FillerDraft,
    HeroDraft,
    generate_fixtures,
)
from kit.schemas import Story
from kit.validate import validate_fixtures

STORY_FILE = Path(__file__).parent.parent / "stories" / "healthcare.yaml"


def small_story() -> Story:
    """The healthcare story cut down to one hero account and a little filler."""
    data = yaml.safe_load(STORY_FILE.read_text())
    data["accounts"] = data["accounts"][:1]  # just Acme
    data["volume"] = {"filler_accounts": 2, "filler_deals": 3}
    return Story.model_validate(data)


def activity(day: int, **changes) -> DraftActivity:
    values = dict(
        type="call", day=day, title="Discovery call", body="Check-in lines at three hospitals.",
        contact_personas=["champion"], deal_key="deal-01", direction="outbound",
        duration_minutes=30, task_status=None,
    )
    return DraftActivity(**(values | changes))


def fake_llm(prompt: str, output_model):
    """Stands in for Claude: returns a fixed, valid answer for each kind of request."""
    if output_model is HeroDraft:
        return HeroDraft(
            company_description="Three-hospital system in central Ohio.",
            phone_area_code="614",
            contacts=[
                DraftContact(persona="champion", first_name="Dana", last_name="Okafor", job_title="VP Patient Operations"),
                DraftContact(persona="blocker", first_name="Martin", last_name="Hale", job_title="CFO"),
                DraftContact(persona="it-lead", first_name="Priya", last_name="Venkat", job_title="Director of Clinical Applications"),
            ],
            deals=[DraftDealName(deal_key="deal-01", name="Acme - Patient Intake Platform")],
            activities=[
                activity(-42),
                activity(-7, type="note", title="CFO pricing objection", contact_personas=["blocker"],
                         direction=None, duration_minutes=None),
                activity(2, type="task", title="Send ROI summary", task_status="not_started",
                         direction="outbound", duration_minutes=15),  # extra fields should be dropped
            ],
        )
    return FillerDraft(
        companies=[
            FillerCompany(
                name="Lakeshore Orthopedic Partners", city="Chicago", state="IL", employees=300,
                description="Orthopedic group.", phone_area_code="312",
                contacts=[FillerContact(first_name="Ana", last_name="Ruiz", job_title="Practice Manager")],
                deals=[
                    FillerDeal(name="Lakeshore - Intake", stage="qualifiedtobuy", amount=28500, close_in_days=45),
                    FillerDeal(name="Lakeshore - Add-on", stage="closedwon", amount=9200, close_in_days=-20),
                ],
            ),
            FillerCompany(
                name="Pinecrest Pediatrics", city="Boise", state="ID", employees=80,
                description="Pediatric clinics.", phone_area_code="208",
                contacts=[FillerContact(first_name="Sam", last_name="Lee", job_title="Office Director")],
                deals=[
                    FillerDeal(name="Pinecrest - Intake", stage="contractsent", amount=14750, close_in_days=12),
                    FillerDeal(name="Pinecrest - Extra", stage="appointmentscheduled", amount=5000, close_in_days=30),
                ],
            ),
        ]
    )


@pytest.fixture
def story():
    return small_story()


@pytest.fixture
def fixtures(story):
    return generate_fixtures(story, ask=fake_llm)


# --- The generator ------------------------------------------------------------------


def test_generated_fixtures_pass_validation(story, fixtures):
    assert validate_fixtures(fixtures, story) == []


def test_code_fills_in_ids_emails_and_phones(fixtures):
    champion = next(c for c in fixtures.contacts if c.persona == "champion")
    assert champion.kit_id == "hc-acme-champion"
    assert champion.email == "dana.okafor@acmeregional.example"
    assert champion.phone == "+1 614-555-0100"
    assert fixtures.companies[1].domain == "lakeshoreorthopedicpartners.example"


def test_relative_days_become_relative_dates(fixtures):
    first, _, task = fixtures.activities
    assert first.when.days_ago == 42
    assert task.when.days_from_now == 2


def test_fields_that_dont_apply_are_dropped(fixtures):
    task = fixtures.activities[-1]
    assert task.direction is None and task.duration_minutes is None
    assert task.task_status == "not_started"


def test_extra_filler_deals_are_trimmed(story, fixtures):
    hero_ids = {"hc-acme-company"}
    filler_deals = [d for d in fixtures.deals if d.company_id not in hero_ids]
    assert len(filler_deals) == story.volume.filler_deals  # the fake LLM wrote 4, the story asks for 3


def test_save_and_load_round_trip(tmp_path, fixtures):
    save_fixtures(fixtures, tmp_path)
    assert load_fixtures("healthcare", tmp_path) == fixtures


# --- Validation catches deliberately broken fixtures (M2 "done when") ------------------


def test_email_on_wrong_company_domain_is_caught(story, fixtures):
    fixtures.contacts[0].email = "dana.okafor@pinecrestpediatrics.example"
    assert any("email is on" in e for e in validate_fixtures(fixtures, story))


def test_link_to_missing_record_is_caught(story, fixtures):
    fixtures.deals[0].company_id = "hc-nobody-company"
    fixtures.activities[0].contact_ids = ["hc-acme-ghost"]
    errors = validate_fixtures(fixtures, story)
    assert any("missing company 'hc-nobody-company'" in e for e in errors)
    assert any("missing contact 'hc-acme-ghost'" in e for e in errors)


def test_fixed_date_in_text_is_caught(story, fixtures):
    fixtures.activities[0].body = "Kickoff call on October 12 with the ops team."
    assert any("fixed date" in e for e in validate_fixtures(fixtures, story))


def test_missing_persona_is_caught(story, fixtures):
    fixtures.contacts = [c for c in fixtures.contacts if c.persona != "blocker"]
    assert any("persona 'blocker'" in e for e in validate_fixtures(fixtures, story))


def test_company_reused_by_another_story_is_caught(story, fixtures):
    other = fixtures.model_copy(update={"story_id": "finserv"})
    assert any("already used by story 'finserv'" in e for e in validate_fixtures(fixtures, story, [other]))


def test_broken_fixture_file_is_rejected_on_load(tmp_path, fixtures):
    save_fixtures(fixtures, tmp_path)
    contacts_file = tmp_path / "healthcare" / "contacts.json"
    contacts_file.write_text(contacts_file.read_text().replace("acmeregional.example", "acmeregional.com"))
    with pytest.raises(ValidationError, match="not a reserved domain"):
        load_fixtures("healthcare", tmp_path)


def test_later_calls_are_told_which_names_are_taken():
    data = yaml.safe_load(STORY_FILE.read_text())  # both hero accounts this time
    data["volume"] = {"filler_accounts": 2, "filler_deals": 3}
    prompts = []

    def recording_llm(prompt, output_model):
        prompts.append(prompt)
        return fake_llm(prompt, output_model)

    generate_fixtures(Story.model_validate(data), ask=recording_llm)
    assert "Dana" not in prompts[0]
    assert "Dana" in prompts[1] and "Dana" in prompts[2]  # second hero call and filler call
