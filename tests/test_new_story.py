"""Tests for `kit new` (with a fake LLM) and for stories that reuse another story's background.

No real LLM or HubSpot calls: the fake LLM returns fixed drafts, and FakeHubSpot
(from the seed tests) stands in for the account.
"""

import shutil
from pathlib import Path

import pytest
import yaml

from kit import actions
from kit.fixtures import load_fixtures
from kit.generate import FillerDraft, HeroDraft, generate_fixtures
from kit.new import DraftAccount, DraftPersona, DraftStoryDeal, StoryDraft, new_story, record_prefix
from kit.schemas import Story, load_story
from kit.validate import validate_fixtures
from test_generate_and_validate import fake_llm
from test_seed_reset import FakeHubSpot, seed

REPO = Path(__file__).parent.parent
EXISTING = [load_story(path) for path in sorted((REPO / "stories").glob("*.yaml"))]


def draft(background: str | None = "healthcare") -> StoryDraft:
    return StoryDraft(
        story_id="Acme Price Pushback",
        background=background,
        industry="Healthcare provider network",
        narrative="Acme is close to signing but the CFO wants a lower price.",
        seller_name="Brightwell Health Software",
        accounts=[DraftAccount(
            name="Acme Regional Health", size=4200, city="Columbus", state="OH",
            personas=[
                DraftPersona(key="champion", title="VP Patient Operations", attitude="champion"),
                DraftPersona(key="Blocker", title="Chief Financial Officer", attitude="skeptical"),
            ],
            deals=[DraftStoryDeal(stage="decisionmakerboughtin", amount=160000, close_in_days=30)],
            activity_story="Discovery five weeks ago. CFO pushed back on price last week. Pricing call next week.",
        )],
    )


def write_new_story(tmp_path, story_draft: StoryDraft) -> tuple[Path, dict]:
    return new_story("Acme, CFO pushing back on price", EXISTING, tmp_path, ask=lambda prompt, model: story_draft)


# --- kit new ---------------------------------------------------------------------------


def test_new_story_is_a_valid_story_file(tmp_path):
    path, data = write_new_story(tmp_path, draft())
    story = load_story(path)  # the file on disk passes the same rules as a hand-written story
    assert path.name == "acme-price-pushback.yaml"
    assert story.accounts[0].key == "acme"
    assert story.accounts[0].domain == "acmeregionalhealth.example.com"
    assert [p.key for p in story.accounts[0].personas] == ["champion", "blocker"]  # keys are tidied to lowercase
    assert story.accounts[0].deals[0].key == "deal-01"
    assert story.golden_paths == ["pipeline-review", "deal-deep-dive", "account-360", "rep-activity-feed"]
    assert path.read_text().startswith('# Written by `kit new` from: "Acme, CFO pushing back on price"')


def test_same_industry_reuses_the_background_and_seller(tmp_path):
    _, data = write_new_story(tmp_path, draft("healthcare"))
    assert data["background"] == "healthcare" and "volume" not in data
    assert data["seller_company"] == {"name": "Brightwell Health Software", "domain": "brightwell.example.com"}


def test_new_industry_gets_its_own_background_volume(tmp_path):
    _, data = write_new_story(tmp_path, draft(background=None))
    assert "background" not in data and data["volume"] == {"filler_accounts": 25, "filler_deals": 40}


def test_unknown_background_is_ignored(tmp_path):
    _, data = write_new_story(tmp_path, draft(background="no-such-story"))
    assert "background" not in data and "volume" in data


def test_record_prefix_is_never_reused():
    assert record_prefix("acme-price-pushback", set()) == "app"
    assert record_prefix("acme-price-pushback", {"app"}) == "ac"
    assert record_prefix("healthcare", {"hc", "fs", "mf"}) == "he"


# --- Reusing a background ----------------------------------------------------------------


@pytest.fixture
def story(tmp_path) -> Story:
    path, _ = write_new_story(tmp_path, draft())
    return load_story(path)


@pytest.fixture
def base():
    return load_fixtures("healthcare")


def test_borrowed_background_needs_only_hero_calls(story, base):
    asked = []

    def recording_llm(prompt, output_model):
        asked.append(output_model)
        return fake_llm(prompt, output_model)

    fixtures = generate_fixtures(story, ask=recording_llm, background=base)
    assert asked == [HeroDraft]  # one call for the one hero account, none for filler
    assert FillerDraft not in asked
    filler = [c for c in fixtures.companies if not c.is_hero]
    assert len(filler) == len([c for c in base.companies if not c.is_hero])


def test_borrowed_records_are_rekeyed_to_the_new_story(story, base):
    fixtures = generate_fixtures(story, ask=fake_llm, background=base)
    records = fixtures.companies + fixtures.contacts + fixtures.deals + fixtures.activities
    assert all(r.kit_id.startswith(f"{story.id_prefix}-") for r in records)
    assert not any("acme" in c.kit_id for c in fixtures.companies if not c.is_hero)  # base's heroes not copied
    assert validate_fixtures(fixtures, story, [base], shares_background_with={"healthcare"}) == []


def test_unrelated_stories_still_cannot_share_companies(story, base):
    fixtures = generate_fixtures(story, ask=fake_llm, background=base)
    errors = validate_fixtures(fixtures, story, [base], shares_background_with=set())
    assert any("already used by story 'healthcare'" in e for e in errors)


def test_seed_refuses_while_a_story_with_the_same_background_is_loaded(tmp_path, monkeypatch, base):
    shutil.copy(REPO / "stories" / "healthcare.yaml", tmp_path)
    path, _ = write_new_story(tmp_path, draft())
    monkeypatch.setattr(actions, "STORIES_DIR", tmp_path)
    new = load_story(path)
    hubspot = FakeHubSpot()

    assert actions.loaded_neighbours(new, hubspot) == []
    seed(base, hubspot)
    assert actions.loaded_neighbours(new, hubspot) == ["healthcare"]


def test_story_needs_volume_or_background_not_both():
    data = yaml.safe_load((REPO / "stories" / "healthcare.yaml").read_text())
    with pytest.raises(ValueError, match="either 'volume'"):
        Story.model_validate(data | {"background": "finserv"})
    with pytest.raises(ValueError, match="either 'volume'"):
        Story.model_validate({k: v for k, v in data.items() if k != "volume"})
