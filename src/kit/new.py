"""`kit new`: turn a one-line description into a story file you can review.

    kit new "a healthcare demo for Acme Regional Health where the CFO is pushing back on price"

The LLM writes the creative parts of the story (accounts, people, deals, the timeline).
Code fills in the mechanical parts: the story id and record prefix, example.com
domains, background volume, golden paths. The result is written to stories/<id>.yaml
and nothing else happens: a person reads and adjusts the file, then runs `kit generate`.

If the description fits the industry of an existing story, the new story reuses that
story's background accounts (`background: <story>`), so generating it only costs the
hero-account calls.
"""

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

from kit.generate import AskFunction, ask_claude, slug
from kit.schemas import CLOSED_DEAL_STAGES, DEAL_STAGES, OPEN_DEAL_STAGES, Story

# Background volume for a story in a new industry (same as the bundled stories).
DEFAULT_VOLUME = {"filler_accounts": 25, "filler_deals": 40}
DEFAULT_GOLDEN_PATHS = ["pipeline-review", "deal-deep-dive", "account-360", "rep-activity-feed"]


# --- What the LLM returns ---------------------------------------------------------


class DraftPersona(BaseModel):
    key: str  # short, e.g. "champion", "cfo", "it-lead"
    title: str
    attitude: Literal["champion", "supporter", "neutral", "skeptical", "blocker"]


class DraftStoryDeal(BaseModel):
    stage: Literal[*DEAL_STAGES]
    amount: int
    close_in_days: int


class DraftAccount(BaseModel):
    name: str
    size: int
    city: str
    state: str
    personas: list[DraftPersona]
    deals: list[DraftStoryDeal]
    activity_story: str


class StoryDraft(BaseModel):
    story_id: str  # a short name for the file, e.g. "acme-intake"
    # The id of an existing story in the same industry whose background accounts fit, or null.
    background: str | None
    industry: str
    narrative: str
    seller_name: str
    accounts: list[DraftAccount]


def new_story_prompt(description: str, existing: list[Story]) -> str:
    stories = "\n".join(
        f"- {s.id}: {s.industry} (seller: {s.seller_company.name})" for s in existing if s.background is None
    ) or "- (none yet)"
    return f"""\
A sales rep described the demo they need:
"{description}"

Write a demo story for it: the plan the CRM data will be generated from.

Existing stories whose background accounts (a sales team's wider pipeline) can be reused:
{stories}
If the demo is in the same industry and for the same kind of product as one of these, set
`background` to its id and use that story's seller name, unless the rep named a different
seller. Otherwise set `background` to null.

- story_id: two or three lowercase words joined by hyphens, naming the demo.
- industry and narrative: one line and two or three sentences, like a brief for the demo.
- seller_name: the (fictional) company whose product is being demoed.
- accounts: the hero accounts the demo is about. Use the ones the rep named (keep their names
  exactly); otherwise invent one or two. Each has:
  - 2 to 4 personas: the buying group, each with a job title and attitude to the deal.
    Persona keys are short lowercase words joined by hyphens (champion, cfo, it-lead).
  - 1 deal, open (one of {", ".join(OPEN_DEAL_STAGES)}), close_in_days between 14 and 90,
    unless the rep asked for something else. Closed stages: {", ".join(CLOSED_DEAL_STAGES)}.
  - activity_story: 4 to 6 sentences covering the last six weeks, in relative time ("three
    weeks ago", "last week"). Include something from the last week, and end with an upcoming
    task or meeting, so the deal has a next step.
  - city and state: a US city and two-letter state; size: number of employees.
Follow the rep's description wherever it says something specific (stages, objections, people).
"""


# --- Turning the draft into a story file -------------------------------------------


def unique(candidate: str, taken: set[str]) -> str:
    """'acme' -> 'acme-2' if 'acme' is taken, and so on."""
    name, number = candidate, 2
    while name in taken:
        name, number = f"{candidate}-{number}", number + 1
    return name


def record_prefix(story_id: str, taken: set[str]) -> str:
    """A 2-4 letter prefix for the story's kit IDs, e.g. 'acme-intake' -> 'ai', not used by another story."""
    words = story_id.split("-")
    initials = "".join(w[0] for w in words if w and w[0].isalpha())
    letters = re.sub(r"[^a-z]", "", story_id)
    for candidate in [initials[:4], letters[:2], letters[:3], letters[:4]]:
        if 2 <= len(candidate) <= 4 and candidate not in taken:
            return candidate
    for a in "abcdefghijklmnopqrstuvwxyz":  # last resort: any free two-letter pair
        for b in "abcdefghijklmnopqrstuvwxyz":
            if a + b not in taken:
                return a + b
    raise ValueError("no free record prefix")


def account_key(name: str, taken: set[str]) -> str:
    """'Acme Regional Health' -> 'acme', the first word as a record key."""
    first = re.sub(r"[^a-z0-9]", "", name.lower().split()[0]) or "account"
    return unique(first, taken)


def build_story(draft: StoryDraft, existing: list[Story]) -> dict:
    """The story file's contents, as a dict in the same order as the hand-written stories."""
    story_id = unique(slug_key(draft.story_id), {s.id for s in existing})
    by_id = {s.id: s for s in existing if s.background is None}
    background = by_id.get(draft.background) if draft.background else None

    seller = {"name": draft.seller_name, "domain": f"{slug(draft.seller_name)}.example.com"}
    if background and slug(draft.seller_name) == slug(background.seller_company.name):
        seller = background.seller_company.model_dump()

    accounts, keys = [], set()
    for account in draft.accounts:
        key = account_key(account.name, keys)
        keys.add(key)
        persona_keys: set[str] = set()
        personas = []
        for persona in account.personas:
            persona_key = unique(slug_key(persona.key), persona_keys)
            persona_keys.add(persona_key)
            personas.append({"key": persona_key, "title": persona.title, "attitude": persona.attitude})
        accounts.append({
            "key": key,
            "name": account.name,
            "domain": f"{slug(account.name)}.example.com",
            "size": max(account.size, 1),
            "city": account.city,
            "state": account.state,
            "personas": personas,
            "deals": [
                {"key": f"deal-{n:02d}", "stage": d.stage, "amount": max(d.amount, 1), "close_in_days": d.close_in_days}
                for n, d in enumerate(account.deals, start=1)
            ],
            "activity_story": account.activity_story,
        })

    data = {
        "id": story_id,
        "id_prefix": record_prefix(story_id, {s.id_prefix for s in existing}),
        "industry": draft.industry,
        "narrative": draft.narrative,
        "seller_company": seller,
        "accounts": accounts,
    }
    if background:
        data["background"] = background.id
    else:
        data["volume"] = dict(DEFAULT_VOLUME)
    data["golden_paths"] = list(DEFAULT_GOLDEN_PATHS)
    Story.model_validate(data)  # same rules as a hand-written story
    return data


def slug_key(text: str) -> str:
    """'Acme Intake!' -> 'acme-intake': lowercase words joined by hyphens."""
    return "-".join(re.findall(r"[a-z0-9]+", text.lower())) or "story"


# --- Writing the file ------------------------------------------------------------------


class StoryDumper(yaml.SafeDumper):
    pass


def long_text_folded(dumper, text: str):
    # Long text as a folded block (">"), like the hand-written stories, instead of one long quoted line.
    style = ">" if len(text) > 70 else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", text, style=style)


StoryDumper.add_representer(str, long_text_folded)

HEADER = """\
# Written by `kit new` from: "{description}"
# Everything here is fictional: invented companies and people, example.com domains, 555-01xx phones.
# Read it and adjust anything, then run `uv run kit generate {story_id}`.
{background_note}
"""


def story_yaml(data: dict, description: str) -> str:
    note = (f"# Background accounts are reused from '{data['background']}', so only the hero accounts are generated.\n"
            if "background" in data else "")
    header = HEADER.format(description=description.replace("\n", " "), story_id=data["id"], background_note=note)
    return header + yaml.dump(data, Dumper=StoryDumper, sort_keys=False, width=88, allow_unicode=True)


def new_story(description: str, existing: list[Story], stories_dir: Path,
              ask: AskFunction = ask_claude) -> tuple[Path, dict]:
    """Draft a story from the description and write it to stories_dir/<id>.yaml. Returns (path, contents)."""
    draft = ask(new_story_prompt(description, existing), StoryDraft)
    data = build_story(draft, existing)
    path = stories_dir / f"{data['id']}.yaml"
    path.write_text(story_yaml(data, description))
    return path, data
