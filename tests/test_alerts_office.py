import asyncio
import json
from datetime import datetime

import httpx
import pytest

from revin_take_home.alerts import DispatcherAlertSender
from revin_take_home.office import NEW_YORK, office_status, parse_hours

ALERT = {
    "id": "a",
    "call_id": "call-123",
    "kind": "urgent",
    "lease_token": "t",
    "title": "URGENT priority: no heat, vulnerable person at home",
    "body": "Kings County | Call ref call-123",
}


def send(alert, handler, monkeypatch, topic="summit-test-topic"):
    if topic:
        monkeypatch.setenv("NTFY_TOPIC", topic)
    else:
        monkeypatch.delenv("NTFY_TOPIC", raising=False)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await DispatcherAlertSender(client).send(alert)

    return asyncio.run(run())


def test_alert_posts_json_with_priority_and_no_extra_fields(monkeypatch):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "x"})

    assert send(ALERT, handler, monkeypatch) == {"status": "sent"}
    assert seen == [
        {
            "topic": "summit-test-topic",
            "title": ALERT["title"],
            "message": ALERT["body"],
            "priority": 5,
            "tags": ["red_circle"],
        }
    ]


@pytest.mark.parametrize(
    "code,status", [(500, "queued"), (429, "queued"), (400, "failed"), (403, "failed")]
)
def test_alert_retries_only_transient_failures(monkeypatch, code, status):
    assert send(ALERT, lambda r: httpx.Response(code), monkeypatch)["status"] == status


def test_network_failure_is_retried(monkeypatch):
    def handler(request):
        raise httpx.ConnectTimeout("down")

    assert send(ALERT, handler, monkeypatch)["status"] == "queued"


def test_unconfigured_or_test_calls_never_reach_provider(monkeypatch):
    def handler(request):
        raise AssertionError("must not send")

    assert (
        send(ALERT, handler, monkeypatch, topic=None)["status"]
        == "blocked_unconfigured"
    )
    assert (
        send({**ALERT, "call_id": "eval_1"}, handler, monkeypatch)["status"]
        == "suppressed_test"
    )


@pytest.mark.parametrize(
    "when,open_",
    [
        (datetime(2026, 11, 23, 9, 0), True),  # Monday morning
        (datetime(2026, 11, 23, 18, 0), False),  # closing time
        (datetime(2026, 11, 23, 7, 59), False),
        (datetime(2026, 11, 28, 12, 0), False),  # Saturday
    ],
)
def test_office_hours_in_new_york_time(monkeypatch, when, open_):
    monkeypatch.setenv("OFFICE_HOURS", "mon-fri 08:00-18:00")
    status = office_status(when.replace(tzinfo=NEW_YORK))
    assert status["office_open"] is open_
    assert status["office_hours"] == "Monday to Friday, 8 AM to 6 PM Eastern"


def test_transfer_availability_follows_configuration(monkeypatch):
    monkeypatch.delenv("TRANSFER_NUMBER", raising=False)
    assert office_status()["live_transfer_configured"] is False
    monkeypatch.setenv("TRANSFER_NUMBER", "+15555550100")
    assert office_status()["live_transfer_configured"] is True


@pytest.mark.parametrize(
    "spec", ["weekdays 9-5", "fri-mon 08:00-18:00", "mon-fri 18:00-08:00"]
)
def test_invalid_office_hours_fail_loudly(spec):
    with pytest.raises(ValueError):
        parse_hours(spec)
