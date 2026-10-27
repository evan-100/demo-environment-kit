# Progress

One line per milestone when it finishes (see SPEC §7).

- M0 done (2026-10-26): HubSpot docs checked → `docs/hubspot-notes.md`. Repo skeleton, `.env.example`, HubSpot client with retry, and `kit ping` are in place. Live `kit ping` succeeds; 4/4 tests pass including the live one. Default deal pipeline id `default`, stages: appointmentscheduled, qualifiedtobuy, presentationscheduled, decisionmakerboughtin, contractsent, closedwon, closedlost. Next: M1.
- M1 done (2026-10-26): `src/kit/schemas.py` holds the story models (YAML a human writes) and fixture models (JSON the generator writes, the seeder reads). Rules enforced per field: reserved domains only, 555-01xx phones, known deal stages, open deals close in the future, no unknown fields, exactly-one relative date. `stories/healthcare.yaml` (Acme Regional Health + Northfield Family Clinics) validates; 24 tests pass. Cross-record checks (every reference points to a real record, email domain matches company) are left for M2. Next: M2.
