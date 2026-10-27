# HubSpot API notes

Checked against HubSpot's developer docs on **2026-10-26**. Everything below links to the page it came from.
Items marked **(unverified)** were not stated in the docs and must be confirmed with a live call before we rely on them.

## TL;DR: decisions for this project

| Question | Decision | Why |
|---|---|---|
| Auth | **Service key** (Bearer token) | Legacy private apps can no longer be created as of today, 2026-10-26. Service keys are the documented replacement for single-account REST scripts. |
| Account | **Developer test account** created from Evan's standard HubSpot account | Keeps demo data away from any real account, and it's free. It expires after 90 days *without API calls*. A kit that seeds and checks regularly keeps it alive. |
| API version | **Date-based `2026-09`** paths (e.g. `/crm/objects/2026-09/deals`) | HubSpot says new integrations should use the latest date version. v4 associations lose support in March 2027. The version string lives in one constant in `src/kit/hubspot.py`. |
| Rate limit | Assume **100 requests / 10 s** and **5 search requests / s** | Free/Starter limits for private distribution. Batch endpoints take 100 records per call, so a whole story seeds in a few dozen requests. |
| Upsert key | Custom unique property **`demo_kit_id`** (`hasUniqueValue: true`) used as `idProperty` | Batch upsert supports a custom unique property. This makes "seed twice = same state" natural. |
| Past activity dates | Set `hs_timestamp` on each activity | It's the documented timeline-position field. The docs' examples use 2021 dates, but they never say outright that past dates are allowed **(unverified, confirm in M3)**. |

## 1. Authentication

### Legacy private apps are being shut off for new creation
- New legacy private apps can't be created after **2026-09-28** in accounts created on or after that date, or after **2026-10-26** in older accounts. Existing ones "continue to work as-is".
  Source: [Legacy Private App Creation Being Disabled (changelog)](https://developers.hubspot.com/changelog/legacy-private-app-creation-sunset)
- The changelog recommends **Service Keys** for new in-account API integrations.

### Service keys (what we use)
Source: [Make API requests using a service key](https://developers.hubspot.com/docs/apps/developer-platform/build-apps/authentication/account-service-keys)
- Made for "simple third-party integrations" and calling REST APIs directly. Each key gets object-specific scopes.
- **Create:** in the HubSpot account go to **Development → Keys → Service keys → Create service key**. Name it, add scopes, then **Create**. (The changelog says *Settings → Integrations → Service Keys*. The docs page is newer, so try that path first.)
- Needs a **super admin** or a user with the **Developer tools access** permission.
- **Use:** `Authorization: Bearer pat-na1-…`
- **Rotation:** "Rotate and expire later" keeps the old key working for 7 days. HubSpot recommends rotating every 6 months.
- **Limits:** same as privately distributed apps. 100 apps + service keys + MCP connectors per account.
- **Can't** be used for webhooks or UI extensions. We don't need either.
- **Confirmed 2026-10-26:** a service key *can* be created inside a developer test account. Ours lives in a test account created from Evan's main account, and `kit ping` works with it. (The docs don't say this.) Fallback if that ever changes: a developer-platform app with `auth.type: static` and `distribution: private` (see below).

### Other options (not used)
Source: [Authentication overview](https://developers.hubspot.com/docs/apps/developer-platform/build-apps/authentication/overview)
- **Static auth app:** a developer-platform project app with `auth.type: static` and `distribution: private`. Its token is in the app settings and is also sent as a Bearer token. More setup (CLI, project files) than a service key. This is our fallback.
- **OAuth:** needed only for multi-account or marketplace apps. Overkill here.

### Scopes for the service key
Source: [Scopes](https://developers.hubspot.com/docs/apps/developer-platform/build-apps/authentication/scopes)
- `crm.objects.contacts.read` / `.write`
- `crm.objects.companies.read` / `.write`
- `crm.objects.deals.read` / `.write`
- `crm.schemas.contacts.read` / `.write`, `crm.schemas.companies.read` / `.write`, `crm.schemas.deals.read` / `.write` (to create the `demo_kit_*` properties)
- `sales-email-read` (for logged email activities)
- **Confirmed live 2026-10-26:**
  - The `crm.schemas.*` scopes alone let you create properties but **not read or write records**. Every records call (read, search, upsert) returned 403 `MISSING_SCOPES` until the `crm.objects.*` scopes were added.
  - Creating properties on activities needs per-activity scopes. HubSpot's 403 named them `notes-write`, `calls-write`, `emails-write`, `meetings-write` and `tasks-write`. The upsert error lists `crm.objects.notes.read`, `crm.objects.calls.read` and so on, so add both read and write for each activity type.
  - The `demo_kit_id` and `demo_kit_story` properties were created on companies, contacts and deals.

## 2. Which account to use
Source: [HubSpot account types](https://developers.hubspot.com/docs/getting-started/account-types.md), [Configurable test accounts](https://developers.hubspot.com/docs/developer-tooling/local-development/configurable-test-accounts.md)
- Old "developer accounts" were migrated into standard HubSpot accounts on **2026-03-09**. You now create test accounts from a normal (free) HubSpot account.
- **Developer test account:** a free account with a 90-day trial of many Enterprise features. Up to **10** per standard account.
  - **Expiry:** "Developer test accounts will expire after 90 days if no API calls are made to the account." Renew under **Test accounts → Actions → Renew trials**, or with an API call from an OAuth app in the same developer account. API renewal works only within the 30 days before expiry.
  - Create in the UI under **Development → Testing → Test Accounts → Create developer test account**, or with `hs test-account create` in the CLI.
  - **Configurable test accounts** let you choose a tier per hub (Starter/Pro/Enterprise) from a JSON config. That's useful if we want the demo to show only Sales Hub Starter features.
- **Caveat for demos:** a test account shows Enterprise trial features, so a demo there may show features a prospect on a lower tier wouldn't get. Keep that in mind when the prospect is on a lower tier.
- A free standard CRM portal would also work and never expires. But it mixes demo data with Evan's own account, and principle §3.3 (never touch untagged records) is easier to trust in an account that holds only demo data.

## 3. Rate limits
Source: [API usage guidelines and limits](https://developers.hubspot.com/docs/developer-tooling/platform/usage-guidelines)

| Tier | Per 10 s (per app) | Per day (per account) |
|---|---|---|
| Free / Starter | 100 | 250,000 |
| Professional | 190 | 625,000 |
| Enterprise | 190 | 1,000,000 |

- Service keys follow the private-app limits. A test account with Enterprise trials *may* get the higher tier. **Plan for 100 / 10 s.**
- A **429** body includes `"errorType": "RATE_LIMIT"` and `policyName` (daily vs. the 10-second window).
- Response headers: `X-HubSpot-RateLimit-Max`, `X-HubSpot-RateLimit-Remaining`, `X-HubSpot-RateLimit-Interval-Milliseconds`, plus daily versions. The `-Secondly` headers are deprecated.
- No `Retry-After` header is documented. Our client backs off on its own: wait, then retry with growing delays. HubSpot says to treat 5xx errors under load like 429s.
- Keep error responses under 5% of daily requests.

**CRM search** has its own, stricter limits. Source: [Search the CRM](https://developers.hubspot.com/docs/api-reference/latest/crm/search-the-crm)
- **5 requests / second per account**, shared by everything in the account. Search responses carry no rate-limit headers.
- Max 200 results per page, 10,000 results per query, 5 filterGroups × 6 filters (18 total), query under 3,000 characters.
- **Archived records don't appear in search.** That suits reset and checks.
- Newly written records can take a few seconds to show up in search **(unverified, from experience; confirm in M3)**. The checks may need a short wait or retry after a seed.

## 4. API versioning
Sources: [API reference overview](https://developers.hubspot.com/docs/api-reference/latest/overview.md), [Introducing date-based API versioning](https://developers.hubspot.com/changelog/introducing-date-based-api-versioning), [Versioning](https://developers.hubspot.com/docs/developer-tooling/platform/versioning.md)
- Paths now look like `/crm/objects/2026-09/contacts`. New versions ship every March and September, and each is supported for at least 18 months.
- "For new integrations, always use the latest date version." The latest is **2026-09**. It should be supported until at least about March 2028 (our estimate from the 18-month rule).
- **v4** APIs (including `/crm/v4/associations`) lose support with the 2027-03 release. v1–v3 still work, with no end date announced.
- Note: the OpenAPI spec files linked from `llms.txt` (`/docs/specs/2026-09/*.json`) returned 404 when checked, so these notes come from the guide pages.

## 5. CRM objects: batch create / upsert / archive
Sources: [Custom objects guide (generic object endpoints)](https://developers.hubspot.com/docs/api-reference/latest/crm/objects/custom-objects/guide.md), [Contacts guide](https://developers.hubspot.com/docs/api-reference/latest/crm/objects/contacts/guide.md)

All `POST`, under `/crm/objects/2026-09/{objectType}` (`contacts`, `companies`, `deals`, or IDs `0-1`, `0-2`, `0-3`):

| Action | Path |
|---|---|
| Batch create | `/batch/create` |
| Batch upsert | `/batch/upsert` |
| Batch read | `/batch/read` |
| Batch archive (delete) | `/batch/archive` |
| Search | `/search` |

- **100 records per batch request.**
- Upsert body: `{"inputs": [{"idProperty": "demo_kit_id", "id": "hc-acme-deal-01", "properties": {...}}]}`. `idProperty` goes **in each input**, in the body.
- Contacts: "you can use `email` or a custom unique identifier property". Partial upserts aren't supported with `email`, which is another reason to key on `demo_kit_id`.
- Batch **create** accepts an inline `associations` array per input. The upsert docs don't mention associations, so the seeder will **upsert objects first, then create associations in a separate batch call**. Associating the same pair twice should be a no-op **(unverified, confirm in M3)**.

## 6. Custom properties
Source: [Properties guide](https://developers.hubspot.com/docs/api-reference/latest/crm/properties/guide.md)
- Create: `POST /crm/properties/2026-09/{objectType}`. Read one: `GET /crm/properties/2026-09/{objectType}/{propertyName}`.
- Required body fields: `groupName`, `name`, `label`, `type`, `fieldType`. Single-line text is `type: "string"`, `fieldType: "text"`.
- `hasUniqueValue: true` makes the property a unique identifier. Max **10** per object, and the flag **can't be changed** after creation.
- Our properties on contacts, companies, deals **and every activity type** (so reset can find activities too):
  - `demo_kit_id`: string/text, `hasUniqueValue: true`
  - `demo_kit_story`: string/text (used by reset and search filters)
- Whether activity objects (notes, calls…) accept custom properties is **unverified**. If they don't, reset will find activities through their associations to tagged records instead.
- The guide doesn't document creating a property **group**. We'll use the built-in groups (`contactinformation`, `companyinformation`, `dealinformation`), or confirm group creation later.

## 7. Associations
Source: [Associate records guide](https://developers.hubspot.com/docs/api-reference/latest/crm/associations/associate-records/guide.md)

All `POST`, under `/crm/associations/2026-09/{fromObjectType}/{toObjectType}`:

| Action | Path |
|---|---|
| Batch create default (unlabeled) | `/batch/associate/default` |
| Batch create labeled | `/batch/create` |
| Batch read | `/batch/read` |
| Batch archive | `/batch/archive` |

`HUBSPOT_DEFINED` association type IDs (direction matters):

| From → To | Type ID |
|---|---|
| Contact → company (primary) | 1 |
| Contact → company | 279 |
| Company → contact (primary) | 2 |
| Company → contact | 280 |
| Deal → contact | 3 |
| Contact → deal | 4 |
| Deal → company (primary) | 5 |
| Deal → company | 341 |
| Company → deal (primary) | 6 |
| Company → deal | 342 |

Activities:

| Activity | → Contact | → Company | → Deal |
|---|---|---|---|
| Note | 202 | 190 | 214 |
| Call | 194 | 182 | 206 |
| Email | 198 | 186 | 210 |
| Meeting | 200 | 188 | 212 |
| Task | 204 | 192 | 216 |

Custom labels: `GET /crm/associations/2026-09/{from}/{to}/labels`.

## 8. Activities (engagements)
Sources: [Notes guide](https://developers.hubspot.com/docs/api-reference/latest/crm/activities/notes/guide.md), [Calls guide](https://developers.hubspot.com/docs/api-reference/latest/crm/activities/calls/guide.md)
- Each activity is its own CRM object: `POST /crm/objects/2026-09/{notes|calls|emails|meetings|tasks}`. Batch endpoints follow the generic object pattern in §5.
- **`hs_timestamp` is required.** It "determines where the [activity] sits on the record timeline". It accepts Unix milliseconds or a UTC ISO string. Our relative dates (`days_ago: 12`) resolve to this field at seed time.
- Associations can be included at creation: `"associations": [{"to": {"id": "301"}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 190}]}]`
- Note fields: `hs_note_body` (max 65,536 chars), `hubspot_owner_id`.
- Call fields: `hs_call_title`, `hs_call_body`, `hs_call_duration` (ms), `hs_call_direction` (`INBOUND`/`OUTBOUND`), `hs_call_status` (e.g. `COMPLETED`).
- Emails, meetings and tasks follow the same pattern. Exact field names get confirmed from their guides in M3.
- Community reports say `hs_lastmodifieddate` changes on later updates even when `hs_timestamp` is backdated. **Checks must use `hs_timestamp`** to judge how recent an activity is, never the modified date.

## 9. Deal pipelines
Source: [Pipelines guide](https://developers.hubspot.com/docs/api-reference/latest/crm/pipelines/guide.md)
- List deal pipelines and their stages: `GET /crm/pipelines/2026-09/0-3` (`0-3` = deals). One pipeline's stages: `GET /crm/pipelines/2026-09/0-3/{pipelineId}/stages`.
- The docs don't list the default stage IDs. The story example uses `contractsent`, from memory of older accounts. **`kit ping` prints the real pipeline and stage IDs from the account**, and story files must use those.

## Open questions to settle with live calls
1. ~~Can a service key be created inside a developer test account?~~ Yes, confirmed in M0.
2. Which scopes do notes/calls/meetings/tasks and pipeline reads need? (M0/M3)
3. Do activity objects accept the custom `demo_kit_*` properties? (M3)
4. Are past `hs_timestamp` values accepted and shown in timeline order? (M3)
5. Is re-associating an existing pair a no-op? (M3)
6. How long after a write until search sees the record? (M3/M4)
