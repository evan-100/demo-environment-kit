# Video script (about 2 minutes)

Screen recording, terminal on the left, HubSpot on the right. Times are targets.

**Before recording**
- `uv run kit reset healthcare --reseed --yes` (fresh, passing demo)
- HubSpot open on Deals → Sales Pipeline (board view), logged in to the test account
- Terminal font large, `clear`

---

**0:00–0:15 · The problem** *(HubSpot board on screen)*
> "Sales reps maintain their own demo accounts by hand. The data goes stale, dates drift into the past,
> someone edits a deal, and the rep finds out live, in front of the customer.
> This kit keeps demo environments realistic, current and verified before every call."

**0:15–0:35 · One story file** *(open `stories/healthcare.yaml`)*
> "A demo starts as one story file: the industry, the hero accounts, who's the champion, who's skeptical,
> and what happened over the last six weeks. An LLM turns it into fixture data once. That data is validated
> and committed, and every date in it is relative, so it never goes stale."

**0:35–0:50 · Seed** *(run `uv run kit seed healthcare`; switch to HubSpot, open the Acme deal timeline)*
> "Seeding loads about 190 records into HubSpot in around fifteen seconds. Every record is tagged, so seeding
> twice changes nothing and reset never touches anything else. Here's the Acme deal: contract sent,
> CFO pushing back on price last week, champion asking for an ROI summary three days ago."

**0:50–1:10 · Something breaks** *(run `uv run kit break healthcare`, refresh the deal in HubSpot)*
> "Now someone edits the deal. Wrong stage, close date in the past. That's exactly the kind of thing a rep
> discovers mid-demo."

**1:10–1:45 · The agent** *(run `uv run python examples/get_acme_ready.py`)*
> "Instead, the rep asks an agent: get the healthcare demo ready for Acme and tell me if anything's broken.
> The agent uses the kit through MCP. It runs the golden-path checks, the four screens the rep will show,
> finds two failing, runs the fix, checks again, and reports back in plain language: what broke, what it
> fixed, and the fallback to use if anything still fails."
*(hold on the agent's summary; switch to HubSpot, refresh the deal: back to Contract Sent)*

**1:45–2:00 · Proof** *(run `uv run kit metrics`, then show the README)*
> "Every run is logged. Across ten fresh seeds per story, these are the real numbers: seed time, reset time
> and how often every check passed. Three industry stories, and the same golden paths for each."

---

**Captions to add:** "3 industry stories", "seed twice = no change", "reset touches only tagged records",
"4 golden paths checked before every call".
