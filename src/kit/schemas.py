"""Data shapes for the kit, checked with pydantic.

Two families of models live here:

1. **Story** models: the YAML file a human writes (`stories/<name>.yaml`).
2. **Fixture** models: the JSON the generator writes (`fixtures/<story>/*.json`)
   and the seeder reads. Fixtures never hold real dates, only relative ones
   (`days_ago` / `days_from_now`) that get turned into dates at seed time.

Per-field rules (fictional domains, 555-01xx phones, key formats) are enforced
here. Cross-record checks, like "every deal points to a real company", come in
M2 (validation after generation).
"""

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --- Shared rules -------------------------------------------------------------

# Deal stages in the HubSpot test account's default "Sales Pipeline",
# as printed by `kit ping` on 2026-10-26.
OPEN_DEAL_STAGES = [
    "appointmentscheduled",
    "qualifiedtobuy",
    "presentationscheduled",
    "decisionmakerboughtin",
    "contractsent",
]
CLOSED_DEAL_STAGES = ["closedwon", "closedlost"]
DEAL_STAGES = OPEN_DEAL_STAGES + CLOSED_DEAL_STAGES

# Lowercase words joined by hyphens, e.g. "acme" or "deal-01".
KEY_PATTERN = r"^[a-z0-9]+(-[a-z0-9]+)*$"

# Fictional phone numbers: the 555-0100 to 555-0199 range, e.g. "+1 312-555-0142".
PHONE_PATTERN = r"^\+1 \d{3}-555-01\d{2}$"

# Domains reserved for examples, so they can never belong to a real company (RFC 2606).
# Bare ".example" is reserved too, but HubSpot rejects it in email addresses, so we use these.
RESERVED_SECOND_LEVEL = {"example.com", "example.org", "example.net"}


def is_reserved_domain(domain: str) -> bool:
    """True if the domain can never belong to a real company.

    Allowed: example.com / .org / .net and their subdomains (e.g. "brightwell.example.com").
    """
    domain = domain.lower()
    return any(domain == d or domain.endswith("." + d) for d in RESERVED_SECOND_LEVEL)


def check_reserved_domain(domain: str) -> str:
    if not is_reserved_domain(domain):
        raise ValueError(
            f"'{domain}' is not a reserved domain. Use something like 'acme.example.com', so the data stays fictional."
        )
    return domain.lower()


def find_duplicates(items: list[str]) -> list[str]:
    seen, duplicates = set(), []
    for item in items:
        if item in seen and item not in duplicates:
            duplicates.append(item)
        seen.add(item)
    return duplicates


class StrictModel(BaseModel):
    # Reject unknown fields, so a typo like "persona:" instead of "personas:" fails loudly.
    model_config = ConfigDict(extra="forbid")


# --- 1. Story file --------------------------------------------------------------


class SellerCompany(StrictModel):
    """The (fictional) company whose product is being demoed."""

    name: str
    domain: str

    _domain_is_reserved = field_validator("domain")(check_reserved_domain)


class Persona(StrictModel):
    """A person at a hero account, described by role. The generator invents the name."""

    key: str = Field(pattern=KEY_PATTERN)
    title: str
    attitude: Literal["champion", "supporter", "neutral", "skeptical", "blocker"]


class StoryDeal(StrictModel):
    key: str = Field(pattern=KEY_PATTERN)
    stage: Literal[*DEAL_STAGES]
    amount: int = Field(gt=0)
    # Days until the close date. Negative means it closed in the past.
    close_in_days: int

    @model_validator(mode="after")
    def open_deals_close_in_future(self):
        # An open deal with a close date in the past looks broken in a pipeline review.
        if self.stage in OPEN_DEAL_STAGES and self.close_in_days <= 0:
            raise ValueError(
                f"deal '{self.key}' is open ({self.stage}) so close_in_days must be above 0"
            )
        if self.stage in CLOSED_DEAL_STAGES and self.close_in_days > 0:
            raise ValueError(
                f"deal '{self.key}' is closed ({self.stage}) so close_in_days can't be in the future"
            )
        return self


class HeroAccount(StrictModel):
    """An account the demo story is about. It gets detailed, consistent data."""

    key: str = Field(pattern=KEY_PATTERN)
    name: str
    domain: str
    size: int = Field(gt=0, description="Number of employees")
    city: str
    state: str
    personas: list[Persona] = Field(min_length=1)
    deals: list[StoryDeal] = Field(min_length=1)
    # Plain-language timeline the generator turns into notes, calls, emails and meetings.
    activity_story: str

    _domain_is_reserved = field_validator("domain")(check_reserved_domain)

    @model_validator(mode="after")
    def keys_are_unique(self):
        for kind, keys in [
            ("persona", [p.key for p in self.personas]),
            ("deal", [d.key for d in self.deals]),
        ]:
            if duplicates := find_duplicates(keys):
                raise ValueError(f"account '{self.key}' repeats {kind} keys: {duplicates}")
        return self


class Volume(StrictModel):
    """Background data, so pipeline views look like a real sales team's."""

    filler_accounts: int = Field(ge=0)
    filler_deals: int = Field(ge=0)


class Story(StrictModel):
    id: str = Field(pattern=KEY_PATTERN)
    # Short prefix for every demo_kit_id in this story, e.g. "hc" -> "hc-acme-deal-01".
    id_prefix: str = Field(pattern=r"^[a-z]{2,4}$")
    industry: str
    narrative: str
    seller_company: SellerCompany
    accounts: list[HeroAccount] = Field(min_length=1)
    volume: Volume
    # Names of files in checks/ (e.g. "deal-deep-dive" -> checks/deal-deep-dive.yaml).
    golden_paths: list[str] = Field(min_length=1)

    @field_validator("golden_paths")
    @classmethod
    def golden_path_names(cls, names: list[str]) -> list[str]:
        for name in names:
            if not re.match(KEY_PATTERN, name):
                raise ValueError(f"golden path '{name}' should look like 'deal-deep-dive'")
        return names

    @model_validator(mode="after")
    def accounts_are_unique(self):
        if duplicates := find_duplicates([a.key for a in self.accounts]):
            raise ValueError(f"account keys repeat: {duplicates}")
        if duplicates := find_duplicates([a.domain for a in self.accounts]):
            raise ValueError(f"account domains repeat: {duplicates}")
        if self.seller_company.domain in [a.domain for a in self.accounts]:
            raise ValueError("an account can't share the seller company's domain")
        return self


def load_story(path: str | Path) -> Story:
    """Read a story YAML file and check it. Raises pydantic.ValidationError if it's wrong."""
    with open(path) as file:
        return Story.model_validate(yaml.safe_load(file))


# --- 2. Fixtures (generator output, seeder input) ------------------------------


class RelativeDate(StrictModel):
    """A date given relative to the moment of seeding, so demo data never goes stale.

    Exactly one of the two fields is set: {"days_ago": 12} or {"days_from_now": 30}.
    """

    days_ago: int | None = Field(default=None, ge=0)
    days_from_now: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def exactly_one(self):
        if (self.days_ago is None) == (self.days_from_now is None):
            raise ValueError("set exactly one of 'days_ago' or 'days_from_now'")
        return self

    def resolve(self, now: datetime) -> datetime:
        """Turn the relative date into a real one, measured from `now`."""
        if self.days_ago is not None:
            return now - timedelta(days=self.days_ago)
        return now + timedelta(days=self.days_from_now)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class FixtureRecord(StrictModel):
    # The stable tag written to HubSpot as demo_kit_id, e.g. "hc-acme-deal-01".
    kit_id: str = Field(pattern=KEY_PATTERN)


class CompanyFixture(FixtureRecord):
    name: str
    domain: str
    city: str
    state: str
    employees: int = Field(gt=0)
    description: str
    is_hero: bool = False

    _domain_is_reserved = field_validator("domain")(check_reserved_domain)


class ContactFixture(FixtureRecord):
    company_id: str  # kit_id of the contact's company
    first_name: str
    last_name: str
    email: str
    job_title: str
    phone: str = Field(pattern=PHONE_PATTERN)
    # The persona key from the story, for hero contacts only (e.g. "champion").
    persona: str | None = None

    @field_validator("email")
    @classmethod
    def email_on_reserved_domain(cls, email: str) -> str:
        if email.count("@") != 1:
            raise ValueError(f"'{email}' is not an email address")
        check_reserved_domain(email.split("@")[1])
        return email.lower()


class DealFixture(FixtureRecord):
    company_id: str
    contact_ids: list[str] = []
    name: str
    stage: Literal[*DEAL_STAGES]
    amount: int = Field(gt=0)
    close_date: RelativeDate

    @model_validator(mode="after")
    def open_deals_close_in_future(self):
        # Same rule as StoryDeal: nothing in the pipeline review may look overdue.
        closes_in_future = bool(self.close_date.days_from_now)
        if self.stage in OPEN_DEAL_STAGES and not closes_in_future:
            raise ValueError(f"deal '{self.kit_id}' is open, so its close date must be in the future")
        if self.stage in CLOSED_DEAL_STAGES and closes_in_future:
            raise ValueError(f"deal '{self.kit_id}' is closed, so its close date can't be in the future")
        return self


class ActivityFixture(FixtureRecord):
    """One timeline entry: a note, call, email, meeting or task."""

    type: Literal["note", "call", "email", "meeting", "task"]
    when: RelativeDate
    title: str
    body: str
    company_id: str | None = None
    contact_ids: list[str] = []
    deal_id: str | None = None
    # Calls and emails only: from the seller's point of view.
    direction: Literal["inbound", "outbound"] | None = None
    # Calls and meetings only.
    duration_minutes: int | None = Field(default=None, gt=0)
    # Tasks only.
    task_status: Literal["not_started", "in_progress", "completed"] | None = None

    @model_validator(mode="after")
    def linked_to_something(self):
        # An activity attached to nothing would never show up on any record a rep opens.
        if not (self.company_id or self.contact_ids or self.deal_id):
            raise ValueError(f"activity '{self.kit_id}' isn't linked to a company, contact or deal")
        return self


class FixtureSet(StrictModel):
    """Everything generated for one story. Saved as fixtures/<story>/*.json."""

    story_id: str
    companies: list[CompanyFixture]
    contacts: list[ContactFixture]
    deals: list[DealFixture]
    activities: list[ActivityFixture]
