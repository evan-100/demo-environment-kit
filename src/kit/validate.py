"""Checks on a whole fixture set, run after generation. No LLM involved.

The pydantic models in schemas.py already check each record on its own
(reserved domains, phone range, deal stages...). This file checks how records
fit together, plus a few story-level rules. Each check adds plain-language
messages to a list; an empty list means the fixtures are good.
"""

import re

from kit.schemas import FixtureSet, Story, find_duplicates

# Absolute dates make demo data go stale, so none may appear in any text.
# Catches "2026-10-12" and "October 12".
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
ABSOLUTE_DATE_PATTERNS = [
    re.compile(r"\b(19|20)\d{2}-\d{2}-\d{2}\b"),
    re.compile(rf"\b({MONTHS}) \d{{1,2}}(st|nd|rd|th)?\b"),
]


def validate_fixtures(
    fixtures: FixtureSet, story: Story, other_stories: list[FixtureSet] | None = None
) -> list[str]:
    errors: list[str] = []
    check_ids(fixtures, story, errors)
    check_references(fixtures, errors)
    check_emails(fixtures, errors)
    check_heroes(fixtures, story, errors)
    check_volume(fixtures, story, errors)
    check_no_absolute_dates(fixtures, errors)
    check_not_reused_across_stories(fixtures, other_stories or [], errors)
    return errors


def check_ids(fixtures: FixtureSet, story: Story, errors: list[str]) -> None:
    all_records = fixtures.companies + fixtures.contacts + fixtures.deals + fixtures.activities
    all_ids = [r.kit_id for r in all_records]
    if duplicates := find_duplicates(all_ids):
        errors.append(f"kit_id used more than once: {duplicates}")
    prefix = f"{story.id_prefix}-"
    for kit_id in all_ids:
        if not kit_id.startswith(prefix):
            errors.append(f"kit_id '{kit_id}' should start with '{prefix}'")


def check_references(fixtures: FixtureSet, errors: list[str]) -> None:
    """Every link between records must point at a record that exists."""
    company_ids = {c.kit_id for c in fixtures.companies}
    contact_company = {c.kit_id: c.company_id for c in fixtures.contacts}
    deal_company = {d.kit_id: d.company_id for d in fixtures.deals}

    for contact in fixtures.contacts:
        if contact.company_id not in company_ids:
            errors.append(f"contact '{contact.kit_id}' points to missing company '{contact.company_id}'")

    for deal in fixtures.deals:
        if deal.company_id not in company_ids:
            errors.append(f"deal '{deal.kit_id}' points to missing company '{deal.company_id}'")
        for contact_id in deal.contact_ids:
            if contact_id not in contact_company:
                errors.append(f"deal '{deal.kit_id}' points to missing contact '{contact_id}'")
            elif contact_company[contact_id] != deal.company_id:
                errors.append(f"deal '{deal.kit_id}' includes '{contact_id}' from a different company")

    for activity in fixtures.activities:
        if activity.company_id and activity.company_id not in company_ids:
            errors.append(f"activity '{activity.kit_id}' points to missing company '{activity.company_id}'")
        for contact_id in activity.contact_ids:
            if contact_id not in contact_company:
                errors.append(f"activity '{activity.kit_id}' points to missing contact '{contact_id}'")
        if activity.deal_id and activity.deal_id not in deal_company:
            errors.append(f"activity '{activity.kit_id}' points to missing deal '{activity.deal_id}'")


def check_emails(fixtures: FixtureSet, errors: list[str]) -> None:
    """Every contact's email must be on their own company's domain, and unique."""
    domain_of = {c.kit_id: c.domain for c in fixtures.companies}
    for contact in fixtures.contacts:
        expected = domain_of.get(contact.company_id)
        actual = contact.email.split("@")[1]
        if expected and actual != expected:
            errors.append(f"contact '{contact.kit_id}' email is on '{actual}', but their company uses '{expected}'")
    if duplicates := find_duplicates([c.email for c in fixtures.contacts]):
        errors.append(f"email address used more than once: {duplicates}")


def check_heroes(fixtures: FixtureSet, story: Story, errors: list[str]) -> None:
    """Every hero account, persona and deal in the story must be in the fixtures, as written."""
    companies_by_domain = {c.domain: c for c in fixtures.companies}
    for account in story.accounts:
        company = companies_by_domain.get(account.domain)
        if company is None or not company.is_hero:
            errors.append(f"hero account '{account.key}' ({account.domain}) is missing")
            continue

        personas_found = {c.persona for c in fixtures.contacts if c.company_id == company.kit_id}
        for persona in account.personas:
            if persona.key not in personas_found:
                errors.append(f"hero account '{account.key}' has no contact for persona '{persona.key}'")

        deals = {d.kit_id: d for d in fixtures.deals}
        for story_deal in account.deals:
            deal = deals.get(f"{story.id_prefix}-{account.key}-{story_deal.key}")
            if deal is None:
                errors.append(f"hero deal '{account.key}/{story_deal.key}' is missing")
            elif (deal.stage, deal.amount) != (story_deal.stage, story_deal.amount):
                errors.append(f"hero deal '{deal.kit_id}' doesn't match the story's stage and amount")

        if not any(a.company_id == company.kit_id for a in fixtures.activities):
            errors.append(f"hero account '{account.key}' has no activities")


def check_volume(fixtures: FixtureSet, story: Story, errors: list[str]) -> None:
    filler_companies = [c for c in fixtures.companies if not c.is_hero]
    filler_ids = {c.kit_id for c in filler_companies}
    filler_deals = [d for d in fixtures.deals if d.company_id in filler_ids]
    if len(filler_companies) != story.volume.filler_accounts:
        errors.append(f"expected {story.volume.filler_accounts} filler accounts, found {len(filler_companies)}")
    if len(filler_deals) != story.volume.filler_deals:
        errors.append(f"expected {story.volume.filler_deals} filler deals, found {len(filler_deals)}")


def check_no_absolute_dates(fixtures: FixtureSet, errors: list[str]) -> None:
    texts = [(c.kit_id, c.description) for c in fixtures.companies]
    texts += [(d.kit_id, d.name) for d in fixtures.deals]
    texts += [(a.kit_id, f"{a.title} {a.body}") for a in fixtures.activities]
    for kit_id, text in texts:
        for pattern in ABSOLUTE_DATE_PATTERNS:
            if match := pattern.search(text):
                errors.append(f"'{kit_id}' mentions a fixed date ('{match.group(0)}'). Use relative wording")


def check_not_reused_across_stories(
    fixtures: FixtureSet, other_stories: list[FixtureSet], errors: list[str]
) -> None:
    """Two stories must not share a company name or domain, or seeding one could clash with the other."""
    for other in other_stories:
        other_names = {c.name.lower() for c in other.companies}
        other_domains = {c.domain for c in other.companies}
        for company in fixtures.companies:
            if company.name.lower() in other_names or company.domain in other_domains:
                errors.append(f"company '{company.name}' is already used by story '{other.story_id}'")
