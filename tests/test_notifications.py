import asyncio
import json
from uuid import uuid4
from urllib.parse import parse_qs

import httpx
import pytest

from revin_take_home.notifications import NotificationSender, sender_ready


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("CONFIRMATION_DELIVERY_MODE", "live")
    for key, value in {
        "TWILIO_ACCOUNT_SID": "AC" + "a" * 32,
        "TWILIO_AUTH_TOKEN": "fake-only",
        "TWILIO_FROM_NUMBER": "+12125550100",
        "RESEND_API_KEY": "fake-only",
        "CONFIRMATION_FROM_EMAIL": "test@example.com",
    }.items():
        monkeypatch.setenv(key, value)


def job(channel="sms"):
    return {
        "id": str(uuid4()),
        "call_id": str(uuid4()),
        "channel": channel,
        "recipient": "+12125550101" if channel == "sms" else "caller@example.com",
        "content": {
            "reference": str(uuid4()),
            "job_type": "repair",
            "arrival_window": "Tuesday, 9 AM to noon",
            "street": "99 Example Street",
            "unit": "none",
            "city": "New York",
            "state": "NY",
            "zip": "10023",
            "technician_name": "Alex Reed",
            "synthetic_roster": True,
        },
    }


def run_send(item, handler):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await NotificationSender(client).send(item)

    return asyncio.run(run())


def test_no_credentials_is_blocked_without_network(monkeypatch):
    monkeypatch.setenv("CONFIRMATION_DELIVERY_MODE", "live")
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)

    def never(_):
        raise AssertionError("Must not call provider")

    assert run_send(job(), never)["status"] == "blocked_unconfigured"


def test_sms_uses_verified_recipient_and_provider_receipt(configured):
    item = job()
    seen = []

    def handler(r):
        seen.append(r)
        data = parse_qs(r.content.decode())
        assert data["To"] == [item["recipient"]]
        assert (
            "test appointment" in data["Body"][0]
            and "no real technician" in data["Body"][0]
        )
        return httpx.Response(201, json={"sid": "SM" + "a" * 32, "status": "queued"})

    result = run_send(item, handler)
    assert result["status"] == "submitted" and result["provider_name"] == "twilio"
    assert len(seen) == 1


def test_email_uses_idempotency_key_and_plain_text(configured):
    item = job("email")

    def handler(r):
        assert r.headers["Idempotency-Key"] == "appointment-confirmation/" + item["id"]
        data = json.loads(r.content)
        assert data["to"] == [item["recipient"]] and "html" not in data
        return httpx.Response(200, json={"id": str(uuid4())})

    assert run_send(item, handler)["status"] == "submitted"


@pytest.mark.parametrize("prefix", ["test_", "eval_"])
def test_automated_fixtures_are_blocked_before_provider_call(configured, prefix):
    item = job()
    item["call_id"] = prefix + str(uuid4())

    def never(_):
        raise AssertionError("Must not call provider")

    assert run_send(item, never)["error"] == "test_recipient_suppressed"


@pytest.mark.parametrize(
    "code,status",
    [(400, "failed"), (401, "failed"), (429, "failed"), (500, "delivery_unknown")],
)
def test_provider_errors_do_not_claim_delivery_or_retry(configured, code, status):
    seen = []

    def handler(r):
        seen.append(r)
        return httpx.Response(code)

    assert run_send(job(), handler)["status"] == status
    assert len(seen) == 1


def test_timeout_does_not_retry_uncertain_send(configured):
    seen = []

    def timeout(r):
        seen.append(r)
        raise httpx.ReadTimeout("response lost")

    assert run_send(job(), timeout)["status"] == "delivery_unknown"
    assert len(seen) == 1


def test_bad_provider_receipt_does_not_claim_success(configured):
    assert (
        run_send(job(), lambda r: httpx.Response(201, json={"sid": "bad"}))["status"]
        == "delivery_unknown"
    )


@pytest.mark.parametrize(
    "provider,event,expected",
    [
        ("twilio", "sent", "submitted"),
        ("twilio", "delivered", "delivered"),
        ("twilio", "undelivered", "failed"),
        ("resend", "sent", "submitted"),
        ("resend", "delivered", "delivered"),
        ("resend", "bounced", "failed"),
    ],
)
def test_poll_distinguishes_acceptance_from_delivery(
    configured, provider, event, expected
):
    item = job("sms" if provider == "twilio" else "email")
    item["provider_name"] = provider
    item["provider_id"] = "SM" + "a" * 32 if provider == "twilio" else str(uuid4())

    def handler(r):
        assert r.method == "GET"
        return httpx.Response(
            200,
            json={"sid": item["provider_id"], "status": event}
            if provider == "twilio"
            else {"id": item["provider_id"], "last_event": event},
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await NotificationSender(client).poll(item)

    assert asyncio.run(run())["status"] == expected


def test_notification_tools_reject_recipient_or_body_injection():
    from pydantic import ValidationError
    from revin_take_home.workflow import PrepareConfirmation, ConfirmNotification

    with pytest.raises(ValidationError):
        PrepareConfirmation(
            channel="sms", caller_quote="Text me", recipient="+12125550999"
        )
    with pytest.raises(ValidationError):
        PrepareConfirmation(
            channel="email",
            caller_quote="Email me",
            email="one@example.com\nBcc:two@example.com",
        )
    with pytest.raises(ValidationError):
        ConfirmNotification(
            confirmation_token=str(uuid4()),
            decision="accepted",
            caller_quote="Yes",
            sender_ready=True,
        )


def test_workflow_derives_sender_readiness_from_saved_channel(monkeypatch):
    from revin_take_home.workflow import WorkflowStore, ConfirmNotification

    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "fake")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "fake")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+12125550100")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    token = str(uuid4())
    seen = []

    def handler(r):
        args = json.loads(r.content)
        seen.append(args)
        if r.url.path.endswith("get_confirmation_status"):
            return httpx.Response(
                200,
                json={
                    "status": "current",
                    "notifications": [
                        {"confirmation_token": token, "channel": "email"}
                    ],
                },
            )
        assert args["p_sender_ready"] is False  # SMS credentials must not enable email.
        return httpx.Response(200, json={"status": "blocked_unconfigured"})

    async def run():
        async with httpx.AsyncClient(
            base_url="https://isolated.test/", transport=httpx.MockTransport(handler)
        ) as client:
            return await WorkflowStore(client).execute(
                "confirm_notification",
                "call",
                "operation",
                ConfirmNotification(
                    confirmation_token=token, decision="accepted", caller_quote="Yes"
                ),
            )

    assert asyncio.run(run())["status"] == "blocked_unconfigured"
    assert len(seen) == 2


def test_stub_mode_never_contacts_provider_even_with_credentials(
    configured, monkeypatch
):
    monkeypatch.setenv("CONFIRMATION_DELIVERY_MODE", "stub")

    def never(_):
        raise AssertionError("Stub cannot contact provider")

    assert not sender_ready("sms") and not sender_ready("email")
    assert run_send(job(), never)["error"] == "stub_mode_no_external_delivery"
