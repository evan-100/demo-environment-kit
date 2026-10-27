"""Tests for the HubSpot client. HubSpot is mocked except for the one live test."""

import os

import httpx
import pytest
from dotenv import load_dotenv

from kit import hubspot
from kit.hubspot import HubSpotClient, HubSpotError

load_dotenv()  # so the live test sees HUBSPOT_TOKEN from .env


def make_client(handler) -> HubSpotClient:
    """A client whose HTTP calls go to `handler` instead of the internet."""
    client = HubSpotClient(token="test-token")
    client.http = httpx.Client(base_url=hubspot.BASE_URL, transport=httpx.MockTransport(handler))
    return client


def test_retries_after_rate_limit(monkeypatch):
    monkeypatch.setattr(hubspot.time, "sleep", lambda seconds: None)  # don't actually wait
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(429, json={"errorType": "RATE_LIMIT"})
        return httpx.Response(200, json={"results": []})

    assert make_client(handler).deal_pipelines() == []
    assert len(calls) == 3


def test_retries_after_dropped_connection(monkeypatch):
    monkeypatch.setattr(hubspot.time, "sleep", lambda seconds: None)
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return httpx.Response(200, json={"results": []})

    assert make_client(handler).deal_pipelines() == []
    assert len(calls) == 2


def test_connection_that_keeps_failing_becomes_a_hubspot_error(monkeypatch):
    monkeypatch.setattr(hubspot.time, "sleep", lambda seconds: None)

    def handler(request):
        raise httpx.ConnectError("no route to host")

    with pytest.raises(HubSpotError, match="connection failed"):
        make_client(handler).deal_pipelines()


def test_client_error_is_not_retried():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(403, json={"message": "missing scope"})

    with pytest.raises(HubSpotError, match="missing scope"):
        make_client(handler).deal_pipelines()
    assert len(calls) == 1


def test_uses_dated_api_version():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={"results": []})

    make_client(handler).deal_pipelines()
    assert seen == [f"/crm/pipelines/{hubspot.API_VERSION}/0-3"]


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("HUBSPOT_TOKEN"), reason="HUBSPOT_TOKEN not set")
def test_live_ping():
    pipelines = HubSpotClient().deal_pipelines()
    assert pipelines, "the account should have at least one deal pipeline"
