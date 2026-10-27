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


class HubSpotError(Exception):
    """Raised when HubSpot returns an error we can't recover from by retrying."""


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
        """Send one request, retrying on rate limits and server errors."""
        wait = FIRST_WAIT_SECONDS
        for attempt in range(MAX_RETRIES + 1):
            response = self.http.request(method, path, json=json)

            retryable = response.status_code == 429 or response.status_code >= 500
            if retryable and attempt < MAX_RETRIES:
                time.sleep(wait)
                wait *= 2
                continue

            if response.status_code >= 400:
                # HubSpot error bodies explain the problem (e.g. a missing scope), so show them.
                raise HubSpotError(f"{method} {path} -> {response.status_code}: {response.text}")

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
