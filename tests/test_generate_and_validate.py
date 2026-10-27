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
    FillerActivity,
    DraftContact,
    DraftDealName,
    FillerCompany,
    FillerContact,
    FillerDeal,
    FillerDraft,
    HeroDraft,
    generate_fixtures,
)
from kit.schemas import RelativeDate, Story
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


def history(is_open: bool = True) -> list[FillerActivity]:
    """A background deal's light history: a past call, plus a next step for open deals."""
    past = FillerActivity(type="call", day=-9, title="Intro call", body="Walked through intake pain points.")
    upcoming = FillerActivity(type="task", day=4, title="Send pricing", body="Follow up with a quote.")
    return [past, upcoming] if is_open else [past]


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
                    FillerDeal(name="Lakeshore - Intake", stage="qualifiedtobuy", amount=28500, close_in_days=45, activities=history()),
                    FillerDeal(name="Lakeshore - Add-on", stage="closedwon", amount=9200, close_in_days=-20, activities=history(False)),
                ],
            ),
            FillerCompany(
                name="Pinecrest Pediatrics", city="Boise", state="ID", employees=80,
                description="Pediatric clinics.", phone_area_code="208",
                contacts=[FillerContact(first_name="Sam", last_name="Lee", job_title="Office Director")],
                deals=[
                    FillerDeal(name="Pinecrest - Intake", stage="contractsent", amount=14750, close_in_days=12, activities=history()),
                    FillerDeal(name="Pinecrest - Extra", stage="appointmentscheduled", amount=5000, close_in_days=30, activities=history()),
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
    assert champion.email == "dana.okafor@acmeregional.example.com"
    assert champion.phone == "+1 614-555-0100"
    assert fixtures.companies[1].domain == "lakeshoreorthopedicpartners.example.com"


def test_relative_days_become_relative_dates(fixtures):
    first, _, task = [a for a in fixtures.activities if a.kit_id.startswith("hc-acme-")]
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
    fixtures.contacts[0].email = "dana.okafor@pinecrestpediatrics.example.com"
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
    contacts_file.write_text(contacts_file.read_text().replace("acmeregional.example.com", "acmeregional.com"))
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


def test_filler_is_requested_in_batches_that_add_up():
    import re

    data = yaml.safe_load(STORY_FILE.read_text())
    data["accounts"] = data["accounts"][:1]  # volume stays at 25 companies / 40 deals
    story = Story.model_validate(data)
    filler_prompts = []

    def counting_llm(prompt, output_model):
        if output_model is HeroDraft:
            return fake_llm(prompt, output_model)
        filler_prompts.append(prompt)
        # Answer each numbered slot ("3. a company with 2 deals") with exactly that many deals.
        slots = [int(n) for n in re.findall(r"a company with (\d+) deal", prompt)]
        start = len(filler_prompts) * 100
        return FillerDraft(companies=[
            FillerCompany(
                name=f"Filler Clinic {start + i}", city="Austin", state="TX", employees=50,
                description="Clinic.", phone_area_code="512",
                contacts=[FillerContact(first_name="Lee", last_name=f"Ng{start + i}", job_title="Manager")],
                deals=[FillerDeal(name=f"Deal {start + i}-{d}", stage="qualifiedtobuy", amount=9000, close_in_days=30,
                                  activities=history())
                       for d in range(count)],
            )
            for i, count in enumerate(slots)
        ])

    fixtures = generate_fixtures(story, ask=counting_llm)
    assert len(filler_prompts) == 3  # 10 + 10 + 5 companies
    assert "Filler Clinic 100" in filler_prompts[1]  # later batches are told which names are taken
    assert validate_fixtures(fixtures, story) == []


def test_background_deals_get_a_linked_history(fixtures):
    deal = next(d for d in fixtures.deals if d.kit_id == "hc-f01-deal-01")
    linked = [a for a in fixtures.activities if a.deal_id == deal.kit_id]
    assert [a.kit_id for a in linked] == ["hc-f01-deal-01-act-1", "hc-f01-deal-01-act-2"]
    past, upcoming = linked
    assert past.when.days_ago == 9 and past.direction == "outbound"
    assert upcoming.when.days_from_now == 4 and upcoming.task_status == "not_started"
    assert upcoming.company_id == deal.company_id and upcoming.contact_ids == deal.contact_ids


def test_open_deal_without_a_next_step_is_caught(story, fixtures):
    fixtures.activities = [a for a in fixtures.activities if a.kit_id != "hc-f01-deal-01-act-2"]
    assert "open deal 'hc-f01-deal-01' has no upcoming task or meeting" in validate_fixtures(fixtures, story)


def test_fill_slots_matches_by_deal_count_and_asks_again_for_the_rest():
    from kit.generate import fill_slots

    def company(name, open_deals_with_step=0, open_deals_without_step=0):
        deals = [FillerDeal(name=f"{name} {i}", stage="qualifiedtobuy", amount=9000, close_in_days=30,
                            activities=history()) for i in range(open_deals_with_step)]
        deals += [FillerDeal(name=f"{name} x{i}", stage="qualifiedtobuy", amount=9000, close_in_days=30,
                             activities=history()[:1]) for i in range(open_deals_without_step)]
        return FillerCompany(name=name, city="Austin", state="TX", employees=50, description="Clinic.",
                             phone_area_code="512", contacts=[], deals=deals)

    # Slots: two companies with 2 deals, one with 1. The LLM returned only two companies,
    # and one open deal of "Beta" has no next step, so Beta can only fill the 1-deal slot.
    matched, still_open = fill_slots([2, 2, 1], [company("Alpha", 3), company("Beta", 1, 1)])
    assert [(c.name, len(c.deals)) for c in matched] == [("Alpha", 2), ("Beta", 1)]
    assert still_open == [2]  # asked for again on the next try


def test_same_person_at_two_companies_is_caught(story, fixtures):
    other = next(c for c in fixtures.contacts if c.company_id != "hc-acme-company")
    other.first_name, other.last_name = "Dana", "Okafor"  # the Acme champion's name
    assert any("same person appears more than once" in e for e in validate_fixtures(fixtures, story))


def test_overdue_task_is_caught(story, fixtures):
    task = next(a for a in fixtures.activities if a.type == "task" and a.when.days_from_now)
    task.when = RelativeDate(days_ago=2)
    assert f"task '{task.kit_id}' is overdue (dated in the past but not completed)" in validate_fixtures(fixtures, story)
