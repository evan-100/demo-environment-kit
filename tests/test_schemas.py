"""Tests for the story and fixture models."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from kit.schemas import (
    ActivityFixture,
    ContactFixture,
    RelativeDate,
    Story,
    is_reserved_domain,
    load_story,
)

STORIES = Path(__file__).parent.parent / "stories"


def healthcare_data() -> dict:
    """The healthcare story as a plain dict, so tests can break one thing at a time."""
    return yaml.safe_load((STORIES / "healthcare.yaml").read_text())


# --- Story file ---------------------------------------------------------------


def test_healthcare_story_is_valid():
    story = load_story(STORIES / "healthcare.yaml")
    assert story.id == "healthcare"
    assert [a.key for a in story.accounts] == ["acme", "northfield"]
    assert story.accounts[0].deals[0].stage == "contractsent"


def test_real_looking_domain_is_rejected():
    data = healthcare_data()
    data["accounts"][0]["domain"] = "acmehealth.com"
    with pytest.raises(ValidationError, match="not a reserved domain"):
        Story.model_validate(data)


def test_unknown_deal_stage_is_rejected():
    data = healthcare_data()
    data["accounts"][0]["deals"][0]["stage"] = "negotiation"
    with pytest.raises(ValidationError):
        Story.model_validate(data)


def test_open_deal_closing_in_the_past_is_rejected():
    data = healthcare_data()
    data["accounts"][0]["deals"][0]["close_in_days"] = -5
    with pytest.raises(ValidationError, match="close_in_days must be above 0"):
        Story.model_validate(data)


def test_duplicate_account_keys_are_rejected():
    data = healthcare_data()
    data["accounts"][1]["key"] = "acme"
    with pytest.raises(ValidationError, match="account keys repeat"):
        Story.model_validate(data)


def test_typo_in_field_name_is_rejected():
    data = healthcare_data()
    data["accounts"][0]["persona"] = data["accounts"][0].pop("personas")
    with pytest.raises(ValidationError):
        Story.model_validate(data)


@pytest.mark.parametrize(
    "domain, allowed",
    [
        ("brightwell.example.com", True),
        ("brightwell.example", False),  # reserved, but HubSpot rejects it in emails
        ("example.com", True),
        ("mail.example.org", True),
        ("acme.com", False),
        ("example.com.evil.io", False),
        ("notexample.com", False),
    ],
)
def test_reserved_domains(domain, allowed):
    assert is_reserved_domain(domain) is allowed


# --- Fixtures -----------------------------------------------------------------


def make_contact(**changes) -> dict:
    contact = {
        "kit_id": "hc-acme-contact-01",
        "company_id": "hc-acme-company",
        "first_name": "Dana",
        "last_name": "Okafor",
        "email": "dana.okafor@acmeregional.example.com",
        "job_title": "VP Patient Operations",
        "phone": "+1 614-555-0142",
        "persona": "champion",
    }
    return contact | changes


def test_valid_contact():
    assert ContactFixture.model_validate(make_contact()).persona == "champion"


@pytest.mark.parametrize("phone", ["+1 614-555-0200", "+1 614-555-1234", "614-555-0142"])
def test_phone_outside_fictional_range_is_rejected(phone):
    with pytest.raises(ValidationError):
        ContactFixture.model_validate(make_contact(phone=phone))


def test_contact_email_on_real_domain_is_rejected():
    with pytest.raises(ValidationError, match="not a reserved domain"):
        ContactFixture.model_validate(make_contact(email="dana@gmail.com"))


def test_relative_date_needs_exactly_one_field():
    with pytest.raises(ValidationError):
        RelativeDate.model_validate({})
    with pytest.raises(ValidationError):
        RelativeDate.model_validate({"days_ago": 3, "days_from_now": 3})


def test_relative_dates_stay_recent_whatever_day_we_seed():
    # Data seeded on different days stays "recent".
    when = RelativeDate(days_ago=12)
    for seed_day in [datetime(2026, 11, 1, tzinfo=timezone.utc), datetime(2027, 6, 15, tzinfo=timezone.utc)]:
        assert seed_day - when.resolve(seed_day) == timedelta(days=12)
    assert RelativeDate(days_from_now=30).resolve(seed_day) == seed_day + timedelta(days=30)


def test_activity_must_be_linked_to_a_record():
    with pytest.raises(ValidationError, match="isn't linked"):
        ActivityFixture.model_validate(
            {
                "kit_id": "hc-acme-note-01",
                "type": "note",
                "when": {"days_ago": 2},
                "title": "Follow-up",
                "body": "Sent ROI summary.",
            }
        )
