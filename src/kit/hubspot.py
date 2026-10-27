"""Small HubSpot API client.

Every call to HubSpot goes through this file, so auth, the API version and
rate-limit handling live in one place. See docs/hubspot-notes.md for where
each detail comes from.
"""

import os
import time

import httpx
from dotenv import load_dotenv

BASE_URL = "https://api.hubapi.com"

# HubSpot's date-based API version. Bump this in one place when moving to a newer release.
API_VERSION = "2026-09"

# Retry settings for rate limits (429) and server errors (5xx).
# HubSpot doesn't send a Retry-After header, so we wait 1s, 2s, 4s, 8s, 16s.
MAX_RETRIES = 5
FIRST_WAIT_SECONDS = 1.0


# Most batch endpoints take at most 100 records per call; search returns at most 200 per page.
BATCH_SIZE = 100
SEARCH_PAGE_SIZE = 200

# The custom properties every kit record carries. Upsert, reset and checks match on them.
KIT_ID = "demo_kit_id"
KIT_STORY = "demo_kit_story"


class HubSpotError(Exception):
    """Raised when HubSpot returns an error we can't recover from by retrying."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def chunks(items: list, size: int = BATCH_SIZE) -> list[list]:
    """Split a list into pieces of at most `size` items."""
    return [items[i : i + size] for i in range(0, len(items), size)]


def batch_errors(response: dict) -> list[str]:
    """Batch calls can partly succeed (HTTP 207). Pull out the messages for the parts that failed."""
    return [error.get("message", str(error)) for error in response.get("errors", [])]


def load_token() -> str:
    """Read the HubSpot service key from the environment (or the .env file)."""
    load_dotenv()
    token = os.environ.get("HUBSPOT_TOKEN", "").strip()
    if not token:
        raise HubSpotError(
            "HUBSPOT_TOKEN is not set. Copy .env.example to .env and paste your service key."
        )
    return token


class HubSpotClient:
    def __init__(self, token: str | None = None):
        self.http = httpx.Client(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {token or load_token()}"},
            timeout=30.0,
        )

    def request(self, method: str, path: str, json: dict | None = None) -> dict:
        """Send one request, retrying on rate limits, server errors and dropped connections.

        Retrying is safe for every call the kit makes: upserts, links, reads and
        deletes all end in the same state if they run twice.
        """
        wait = FIRST_WAIT_SECONDS
        for attempt in range(MAX_RETRIES + 1):
            try:
                response = self.http.request(method, path, json=json)
            except httpx.TransportError as error:
                # Connection dropped, timed out, or never opened (seen live: "Server disconnected").
                if attempt < MAX_RETRIES:
                    time.sleep(wait)
                    wait *= 2
                    continue
                raise HubSpotError(f"{method} {path} -> connection failed after {MAX_RETRIES} retries: {error}")

            retryable = response.status_code == 429 or response.status_code >= 500
            if retryable and attempt < MAX_RETRIES:
                time.sleep(wait)
                wait *= 2
                continue

            if response.status_code >= 400:
                # HubSpot error bodies explain the problem (e.g. a missing scope), so show them.
                raise HubSpotError(
                    f"{method} {path} -> {response.status_code}: {response.text}", response.status_code
                )

            return response.json() if response.content else {}

        raise HubSpotError(f"{method} {path} still failing after {MAX_RETRIES} retries")

    def get(self, path: str) -> dict:
        return self.request("GET", path)

    def post(self, path: str, json: dict) -> dict:
        return self.request("POST", path, json=json)

    # --- Specific calls -------------------------------------------------------

    def deal_pipelines(self) -> list[dict]:
        """All deal pipelines with their stages. Story files must use these stage IDs."""
        return self.get(f"/crm/pipelines/{API_VERSION}/0-3")["results"]

    # The methods below are what seed.py and reset.py use. Tests replace this
    # class with an in-memory fake that has the same methods.

    def owners(self) -> list[dict]:
        """The account's users who can own records (needs crm.objects.owners.read)."""
        return self.get(f"/crm/owners/{API_VERSION}")["results"]

    def create_property_if_missing(self, object_type: str, definition: dict) -> None:
        """Create a property; do nothing if it already exists.

        We don't check first with a GET: reading email property settings needs an
        extra permission (connected-email-data-access) that creating them doesn't.
        """
        try:
            self.post(f"/crm/properties/{API_VERSION}/{object_type}", definition)
        except HubSpotError as error:
            if error.status_code != 409:  # 409 = already exists
                raise

    def batch_upsert(self, object_type: str, records: list[dict]) -> list[str]:
        """Create or update records matched on demo_kit_id.

        Each record is {"kit_id": ..., "properties": {...}}. Returns error messages (empty = all good).
        """
        errors = []
        for chunk in chunks(records):
            inputs = [{"idProperty": KIT_ID, "id": r["kit_id"], "properties": r["properties"]} for r in chunk]
            response = self.post(f"/crm/objects/{API_VERSION}/{object_type}/batch/upsert", {"inputs": inputs})
            errors += batch_errors(response)
        return errors

    def read_by_kit_ids(self, object_type: str, kit_ids: list[str]) -> list[dict]:
        """Look records up by demo_kit_id. Unlike search, this sees records the moment they're written.

        Returns [{"id": hubspot id, "kit_id": ..., "story": ...}]. Kit IDs that don't exist are skipped.
        """
        found = []
        for chunk in chunks(kit_ids):
            response = self.post(
                f"/crm/objects/{API_VERSION}/{object_type}/batch/read",
                {"idProperty": KIT_ID, "inputs": [{"id": k} for k in chunk], "properties": [KIT_ID, KIT_STORY]},
            )
            found += [simplify(record) for record in response.get("results", [])]
        return found

    def find_by_story(self, object_type: str, story: str | None) -> list[dict]:
        """Search for kit records of one story (or of every story when `story` is None).

        Note: search can lag a few seconds behind writes, so reset also uses read_by_kit_ids.
        """
        if story is None:
            kit_filter = {"propertyName": KIT_STORY, "operator": "HAS_PROPERTY"}
        else:
            kit_filter = {"propertyName": KIT_STORY, "operator": "EQ", "value": story}
        body = {"filterGroups": [{"filters": [kit_filter]}], "properties": [KIT_ID, KIT_STORY], "limit": SEARCH_PAGE_SIZE}

        found = []
        while True:
            response = self.post(f"/crm/objects/{API_VERSION}/{object_type}/search", body)
            found += [simplify(record) for record in response["results"]]
            next_page = response.get("paging", {}).get("next", {}).get("after")
            if not next_page:
                return found
            body["after"] = next_page

    def associate(self, from_type: str, to_type: str, links: list[tuple[str, str, int]]) -> list[str]:
        """Link records. Each link is (from HubSpot id, to HubSpot id, association type id)."""
        errors = []
        for chunk in chunks(links):
            inputs = [
                {"from": {"id": from_id}, "to": {"id": to_id},
                 "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": type_id}]}
                for from_id, to_id, type_id in chunk
            ]
            response = self.post(f"/crm/associations/{API_VERSION}/{from_type}/{to_type}/batch/create", {"inputs": inputs})
            errors += batch_errors(response)
        return errors

    # --- Reads used by the readiness checks (check.py) ---------------------------------

    def read_by_ids(self, object_type: str, ids: list[str], properties: list[str], id_property: str | None = None) -> list[dict]:
        """Read records with chosen properties, by HubSpot id (or by demo_kit_id if id_property=KIT_ID).

        Returns [{"id": ..., "properties": {...}}]. Missing records are skipped.
        """
        found = []
        for chunk in chunks(ids):
            body = {"inputs": [{"id": i} for i in chunk], "properties": properties}
            if id_property:
                body["idProperty"] = id_property
            response = self.post(f"/crm/objects/{API_VERSION}/{object_type}/batch/read", body)
            found += [{"id": r["id"], "properties": r.get("properties", {})} for r in response.get("results", [])]
        return found

    def linked_ids(self, from_type: str, ids: list[str], to_type: str) -> dict[str, list[str]]:
        """For each record id, the ids of the `to_type` records linked to it."""
        linked = {i: [] for i in ids}
        for chunk in chunks(ids):
            response = self.post(
                f"/crm/associations/{API_VERSION}/{from_type}/{to_type}/batch/read",
                {"inputs": [{"id": i} for i in chunk]},
            )
            for result in response.get("results", []):
                targets = {str(t["toObjectId"]) for t in result.get("to", [])}
                linked[str(result["from"]["id"])] = sorted(targets)
        return linked

    def search(self, object_type: str, filters: list[dict], properties: list[str]) -> list[dict]:
        """All records matching every filter (paged). Returns [{"id": ..., "properties": {...}}]."""
        body = {"filterGroups": [{"filters": filters}] if filters else [], "properties": properties,
                "limit": SEARCH_PAGE_SIZE}
        found = []
        while True:
            response = self.post(f"/crm/objects/{API_VERSION}/{object_type}/search", body)
            found += [{"id": r["id"], "properties": r.get("properties", {})} for r in response["results"]]
            next_page = response.get("paging", {}).get("next", {}).get("after")
            if not next_page:
                return found
            body["after"] = next_page

    def archive(self, object_type: str, ids: list[str]) -> None:
        """Delete records (HubSpot moves them to its recycle bin)."""
        for chunk in chunks(ids):
            self.post(f"/crm/objects/{API_VERSION}/{object_type}/batch/archive", {"inputs": [{"id": i} for i in chunk]})


def simplify(record: dict) -> dict:
    properties = record.get("properties", {})
    return {"id": record["id"], "kit_id": properties.get(KIT_ID), "story": properties.get(KIT_STORY)}
