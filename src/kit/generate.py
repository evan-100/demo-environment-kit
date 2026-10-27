"""`kit generate`: turn a story file into fixtures using an LLM.

Split of work:
- The **LLM** writes the creative parts: names, job titles, company
  descriptions, deal names, and the timeline of notes, calls, emails, meetings.
- **Code** fills in everything mechanical: kit IDs, email addresses, phone
  numbers, filler domains, and the links between records. These are easy to
  get exactly right in code and easy for an LLM to get subtly wrong.

The LLM answers in JSON that must match the "draft" models below (structured
output). The drafts are then turned into fixture records and checked by
validate.py before anything is saved.
"""

import os
import re
from typing import Callable, Literal, TypeVar

import anthropic
from pydantic import BaseModel

from kit.schemas import (
    DEAL_STAGES,
    ActivityFixture,
    CompanyFixture,
    ContactFixture,
    DealFixture,
    FixtureSet,
    HeroAccount,
    RelativeDate,
    Story,
)

# Model used for generation. Override with KIT_LLM_MODEL in .env.
DEFAULT_MODEL = "claude-opus-5-5"


class GenerateError(Exception):
    pass


# --- What the LLM returns ---------------------------------------------------------
# Kept deliberately simple: no IDs, no emails, no phone numbers.


class DraftContact(BaseModel):
    persona: str  # the story's persona key, e.g. "champion"
    first_name: str
    last_name: str
    job_title: str


class DraftDealName(BaseModel):
    deal_key: str  # the story's deal key, e.g. "deal-01"
    name: str


class DraftActivity(BaseModel):
    type: Literal["note", "call", "email", "meeting", "task"]
    # Days relative to today: -12 means 12 days ago, 5 means 5 days from now.
    day: int
    title: str
    body: str
    contact_personas: list[str]
    deal_key: str | None
    direction: Literal["inbound", "outbound"] | None
    duration_minutes: int | None
    task_status: Literal["not_started", "in_progress", "completed"] | None


class HeroDraft(BaseModel):
    company_description: str
    phone_area_code: str
    contacts: list[DraftContact]
    deals: list[DraftDealName]
    activities: list[DraftActivity]


class FillerContact(BaseModel):
    first_name: str
    last_name: str
    job_title: str


class FillerDeal(BaseModel):
    name: str
    stage: Literal[*DEAL_STAGES]
    amount: int
    # Days until close. Negative for deals that are already closed won or lost.
    close_in_days: int


class FillerCompany(BaseModel):
    name: str
    city: str
    state: str
    employees: int
    description: str
    phone_area_code: str
    contacts: list[FillerContact]
    deals: list[FillerDeal]


class FillerDraft(BaseModel):
    companies: list[FillerCompany]


# --- Prompts ------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You write realistic but entirely fictional CRM demo data for a B2B sales demo.

Rules:
- Every company and person is invented. Never use a real company, hospital,
  brand or well-known person's name, and avoid names that closely resemble one.
- Write like a real sales rep's CRM: specific, plain, a little terse. Notes and
  call summaries mention concrete pains, numbers, objections and next steps.
- Never write a calendar date (no "October 12", no "2026-10-12"). Say
  "last week", "in two weeks", "next quarter" instead. The data is re-dated
  every time it is loaded, so fixed dates would become wrong.
- Use US cities, states (two-letter codes) and real US phone area codes that
  match the city.
"""


def hero_prompt(story: Story, account: HeroAccount, names_taken: list[str]) -> str:
    personas = "\n".join(f"- {p.key}: {p.title} ({p.attitude})" for p in account.personas)
    deals = "\n".join(
        f"- {d.key}: stage {d.stage}, ${d.amount:,}, closes in {d.close_in_days} days"
        for d in account.deals
    )
    return f"""\
Story: {story.industry}. {story.narrative}
Seller: {story.seller_company.name}.

Write the CRM records for this hero account:
{account.name} — {account.size:,} employees, {account.city}, {account.state}.

People (one contact per persona, use these persona keys exactly):
{personas}

Deals (use these deal keys exactly; give each a realistic deal name):
{deals}

Timeline to turn into 6 to 10 activities (notes, calls, emails, meetings, tasks):
{account.activity_story}

For each activity:
- day: negative for the past (-21 = three weeks ago), positive only for things
  scheduled in the future (a booked meeting, a task due date).
- contact_personas: the persona keys involved.
- deal_key: the deal it's about, or null.
- direction: for calls and emails only (from the seller's side), else null.
- duration_minutes: for calls and meetings only, else null.
- task_status: for tasks only, else null.
The most recent past activity should be within the last 10 days.
{avoid_names(names_taken)}"""


def avoid_names(names_taken: list[str]) -> str:
    # Each LLM call starts fresh, so tell it which names earlier calls already used.
    if not names_taken:
        return ""
    return f"Other accounts already use these first names, so pick different ones: {', '.join(names_taken)}.\n"


# Long lists are where LLMs lose count, so filler is requested in batches of this many companies.
FILLER_BATCH_SIZE = 10


def deals_per_company(companies: int, deals: int) -> list[int]:
    """Spread deals over companies as evenly as possible, e.g. 25 companies / 40 deals -> [2, 2, ..., 1, 1]."""
    return [deals // companies + (1 if i < deals % companies else 0) for i in range(companies)]


def filler_prompt(story: Story, deal_counts: list[int], names_taken: list[str], companies_taken: list[str]) -> str:
    """Ask for one batch of filler companies; `deal_counts` has one entry per company wanted."""
    taken = [a.name for a in story.accounts] + companies_taken
    slots = "\n".join(f"  {n}. a company with {c} deal{'s' if c != 1 else ''}" for n, c in enumerate(deal_counts, 1))
    return f"""\
Story: {story.industry}. {story.narrative}
Seller: {story.seller_company.name}.

Write background accounts so the seller's pipeline looks like a real sales team's.
Return exactly {len(deal_counts)} companies in this industry, varied in size and location, in this order:
{slots}

- Do not reuse or resemble these company names: {", ".join(taken)}.
- 1 or 2 contacts per company (buyers or evaluators for this kind of product).
- About 80% of deals are open, spread across all of these stages:
  appointmentscheduled, qualifiedtobuy, presentationscheduled, decisionmakerboughtin, contractsent.
  Open deals have close_in_days between 7 and 120.
- The rest are closedwon or closedlost with close_in_days between -90 and -1.
- Amounts fit the company size and are not round thousands every time.
- Descriptions are one sentence.
{avoid_names(names_taken)}"""


# --- Calling the LLM ------------------------------------------------------------------

DraftT = TypeVar("DraftT", bound=BaseModel)

# A function that sends a prompt and returns the answer as the given draft model.
# Tests swap in a fake so no real LLM is called.
AskFunction = Callable[[str, type[DraftT]], DraftT]


def ask_claude(prompt: str, output_model: type[DraftT]) -> DraftT:
    """Ask Claude for JSON matching `output_model` (structured output)."""
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    # Streaming avoids HTTP timeouts on long answers. The server-side fallback
    # retries on another model if this one declines the request.
    with client.beta.messages.stream(
        model=os.environ.get("KIT_LLM_MODEL", DEFAULT_MODEL),
        max_tokens=64000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "medium"},
        output_format=output_model,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        message = stream.get_final_message()

    if message.stop_reason == "refusal":
        raise GenerateError("the model declined to write this data. Try rewording the story.")
    if message.stop_reason == "max_tokens":
        raise GenerateError("the model's answer was cut off. Try fewer filler records.")
    if message.parsed_output is None:
        raise GenerateError("the model's answer didn't match the expected format")
    return message.parsed_output


# --- Turning drafts into fixtures ---------------------------------------------------------


def slug(text: str) -> str:
    """'Dana Okafor-Reyes' -> 'danaokaforreyes'. Used for emails and domains."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def to_relative(day: int) -> RelativeDate:
    return RelativeDate(days_ago=-day) if day <= 0 else RelativeDate(days_from_now=day)


class PhoneBook:
    """Hands out fictional phone numbers: +1 AAA-555-0100, then -0101, and so on."""

    def __init__(self):
        self.next_number = 0

    def new(self, area_code: str) -> str:
        if not re.fullmatch(r"\d{3}", area_code):
            area_code = "555"
        number = f"+1 {area_code}-555-01{self.next_number % 100:02d}"
        self.next_number += 1
        return number


def build_hero(story: Story, account: HeroAccount, draft: HeroDraft, phones: PhoneBook) -> FixtureSet:
    prefix = f"{story.id_prefix}-{account.key}"
    company_id = f"{prefix}-company"

    company = CompanyFixture(
        kit_id=company_id,
        name=account.name,
        domain=account.domain,
        city=account.city,
        state=account.state,
        employees=account.size,
        description=draft.company_description,
        is_hero=True,
    )

    contact_id_for = {}  # persona key -> contact kit_id
    contacts = []
    for person in draft.contacts:
        kit_id = f"{prefix}-{person.persona}"
        contact_id_for[person.persona] = kit_id
        contacts.append(
            ContactFixture(
                kit_id=kit_id,
                company_id=company_id,
                first_name=person.first_name,
                last_name=person.last_name,
                email=f"{slug(person.first_name)}.{slug(person.last_name)}@{account.domain}",
                job_title=person.job_title,
                phone=phones.new(draft.phone_area_code),
                persona=person.persona,
            )
        )

    deal_names = {d.deal_key: d.name for d in draft.deals}
    deals = [
        DealFixture(
            kit_id=f"{prefix}-{story_deal.key}",
            company_id=company_id,
            contact_ids=list(contact_id_for.values()),
            # Fall back to a plain name if the LLM skipped a deal.
            name=deal_names.get(story_deal.key, f"{account.name} - {story.seller_company.name}"),
            stage=story_deal.stage,
            amount=story_deal.amount,
            close_date=to_relative(story_deal.close_in_days),
        )
        for story_deal in account.deals
    ]

    activities = []
    for number, item in enumerate(sorted(draft.activities, key=lambda a: a.day), start=1):
        activities.append(
            ActivityFixture(
                kit_id=f"{prefix}-act-{number:02d}",
                type=item.type,
                when=to_relative(item.day),
                title=item.title,
                body=item.body,
                company_id=company_id,
                # Unknown persona keys are kept so validation reports them.
                contact_ids=[contact_id_for.get(p, f"{prefix}-{p}") for p in item.contact_personas],
                deal_id=f"{prefix}-{item.deal_key}" if item.deal_key else None,
                direction=item.direction if item.type in ("call", "email") else None,
                duration_minutes=item.duration_minutes if item.type in ("call", "meeting") else None,
                task_status=item.task_status if item.type == "task" else None,
            )
        )

    return FixtureSet(
        story_id=story.id, companies=[company], contacts=contacts, deals=deals, activities=activities
    )


def build_filler(story: Story, draft: FillerDraft, phones: PhoneBook) -> FixtureSet:
    companies, contacts, deals = [], [], []
    deals_left = story.volume.filler_deals

    # The LLM is asked for exact counts; if it writes extra, drop the extras.
    for number, item in enumerate(draft.companies[: story.volume.filler_accounts], start=1):
        prefix = f"{story.id_prefix}-f{number:02d}"
        company_id = f"{prefix}-company"
        domain = f"{slug(item.name)}.example.com"
        companies.append(
            CompanyFixture(
                kit_id=company_id,
                name=item.name,
                domain=domain,
                city=item.city,
                state=item.state,
                employees=max(item.employees, 1),
                description=item.description,
            )
        )

        contact_ids = []
        for contact_number, person in enumerate(item.contacts, start=1):
            kit_id = f"{prefix}-contact-{contact_number:02d}"
            contact_ids.append(kit_id)
            contacts.append(
                ContactFixture(
                    kit_id=kit_id,
                    company_id=company_id,
                    first_name=person.first_name,
                    last_name=person.last_name,
                    email=f"{slug(person.first_name)}.{slug(person.last_name)}@{domain}",
                    job_title=person.job_title,
                    phone=phones.new(item.phone_area_code),
                )
            )

        for deal_number, deal in enumerate(item.deals[:deals_left], start=1):
            deals.append(
                DealFixture(
                    kit_id=f"{prefix}-deal-{deal_number:02d}",
                    company_id=company_id,
                    contact_ids=contact_ids[:1],
                    name=deal.name,
                    stage=deal.stage,
                    amount=deal.amount,
                    close_date=to_relative(deal.close_in_days),
                )
            )
        deals_left = story.volume.filler_deals - len(deals)

    return FixtureSet(story_id=story.id, companies=companies, contacts=contacts, deals=deals, activities=[])


def generate_fixtures(story: Story, ask: AskFunction = ask_claude) -> FixtureSet:
    """Build the full fixture set for a story: one LLM call per hero account, one for filler."""
    phones = PhoneBook()
    parts = []
    names_taken: list[str] = []
    for account in story.accounts:
        draft = ask(hero_prompt(story, account, names_taken), HeroDraft)
        parts.append(build_hero(story, account, draft, phones))
        names_taken += [c.first_name for c in draft.contacts]
    # Filler in batches, each told which company names earlier batches used.
    counts = deals_per_company(story.volume.filler_accounts, story.volume.filler_deals)
    filler = FillerDraft(companies=[])
    for start in range(0, len(counts), FILLER_BATCH_SIZE):
        batch_counts = counts[start : start + FILLER_BATCH_SIZE]
        companies_taken = [c.name for c in filler.companies]
        draft = ask(filler_prompt(story, batch_counts, names_taken, companies_taken), FillerDraft)
        filler.companies += draft.companies[: len(batch_counts)]  # drop extras the LLM added
        names_taken += [p.first_name for c in draft.companies for p in c.contacts]
    parts.append(build_filler(story, filler, phones))

    return FixtureSet(
        story_id=story.id,
        companies=[c for part in parts for c in part.companies],
        contacts=[c for part in parts for c in part.contacts],
        deals=[d for part in parts for d in part.deals],
        activities=[a for part in parts for a in part.activities],
    )
