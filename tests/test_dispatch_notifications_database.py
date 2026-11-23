"""Local-only scheduling and outbox tests; every fixture rolls back."""

import os
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from test_weather_contact_database import flow  # noqa: F401 (pytest fixture)

pytestmark = pytest.mark.skipif(
    urlparse(os.environ.get("TEST_DATABASE_URL", "")).hostname
    not in ("127.0.0.1", "localhost", "::1"),
    reason="Requires isolated TEST_DATABASE_URL",
)


from test_dispatch_database import book


def test_confirmation_requires_committed_booking(flow):
    assert (
        flow.rpc("prepare_confirmation", flow.call, "sms", "Text please")["status"]
        == "not_booked"
    )


def test_text_prepares_booked_callback_and_test_consent_never_sends(flow):
    result, _ = book(flow)
    n = flow.rpc("prepare_confirmation", flow.call, "sms", "Text this number")
    assert (
        n["status"] == "awaiting_confirmation"
        and n["recipient"] == result["details"]["callback_number"]
    )
    r = flow.rpc(
        "confirm_notification",
        flow.call,
        n["confirmation_token"],
        "accepted",
        "Text this number",
        True,
    )
    assert r["status"] == "suppressed_test"
    assert flow.rpc("claim_appointment_notification") is None
    assert (
        flow.rpc(
            "confirm_notification",
            flow.call,
            n["confirmation_token"],
            "accepted",
            "Text this number",
            True,
        )["notification_id"]
        == n["notification_id"]
    )


def test_email_correction_invalidates_old_confirmation(flow):
    book(flow)
    old = flow.rpc(
        "prepare_confirmation", flow.call, "email", "Email me", "first@example.com"
    )
    new = flow.rpc(
        "prepare_confirmation",
        flow.call,
        "email",
        "Actually second",
        "second@example.com",
    )
    assert old["confirmation_token"] != new["confirmation_token"]
    assert (
        flow.rpc(
            "confirm_notification",
            flow.call,
            old["confirmation_token"],
            "accepted",
            "Yes",
            True,
        )["status"]
        == "stale_confirmation"
    )
    assert (
        flow.rpc("get_confirmation_status", flow.call)["notifications"][0]["status"]
        == "awaiting_confirmation"
    )


@pytest.mark.parametrize(
    "email", ["bad", "a@b", "x\n@example.com", "x@example.com\nBcc:bad@example.com"]
)
def test_malformed_email_cannot_be_prepared(flow, email):
    book(flow)
    assert (
        flow.rpc("prepare_confirmation", flow.call, "email", "Email please", email)[
            "status"
        ]
        == "invalid_email"
    )


def live_queue(flow, ready=True):
    # Random isolated fixture ID shaped like a phone call; never reaches a provider.
    flow.call = str(uuid4())
    flow.rev = 0
    book(flow)
    n = flow.rpc("prepare_confirmation", flow.call, "sms", "Text please")
    r = flow.rpc(
        "confirm_notification",
        flow.call,
        n["confirmation_token"],
        "accepted",
        "Yes, text me",
        ready,
    )
    return n, r


def test_unconfigured_sender_is_explicit_and_booking_remains(flow):
    _, r = live_queue(flow, False)
    assert r["status"] == "blocked_unconfigured"
    assert flow.rpc("claim_appointment_notification") is None
    assert flow.rpc("get_booking_status", flow.call)["status"] == "booked"


def test_outbox_lease_cannot_send_twice_and_unknown_send_is_not_retried(flow):
    n, r = live_queue(flow)
    assert r["status"] == "queued"
    job = flow.rpc("claim_appointment_notification")
    assert job["action"] == "send"
    assert flow.rpc("claim_appointment_notification") is None
    flow.db.execute(
        "update public.appointment_notifications set lease_until=clock_timestamp()-interval '1 second' where id=%s",
        (job["id"],),
    )
    assert flow.rpc("claim_appointment_notification") is None
    assert (
        flow.rpc("get_confirmation_status", flow.call)["notifications"][0]["status"]
        == "delivery_unknown"
    )
    assert (
        flow.rpc(
            "finish_appointment_notification",
            job["id"],
            job["lease_token"],
            "submitted",
            "SM" + "a" * 32,
            "twilio",
        )["status"]
        == "lease_lost"
    )


def test_provider_acceptance_is_not_delivery_and_poll_does_not_resend(flow):
    live_queue(flow)
    job = flow.rpc("claim_appointment_notification")
    r = flow.rpc(
        "finish_appointment_notification",
        job["id"],
        job["lease_token"],
        "submitted",
        "SM" + "a" * 32,
        "twilio",
    )
    assert r["status"] == "submitted"
    flow.db.execute(
        "update public.appointment_notifications set next_check_at=clock_timestamp()-interval '1 second' where id=%s",
        (job["id"],),
    )
    poll = flow.rpc("claim_appointment_notification")
    assert poll["action"] == "poll" and poll["provider_id"] == "SM" + "a" * 32
    assert (
        flow.rpc(
            "finish_appointment_notification",
            poll["id"],
            poll["lease_token"],
            "delivered",
        )["status"]
        == "delivered"
    )
    assert flow.rpc("claim_appointment_notification") is None


def test_failed_confirmation_does_not_cancel_assignment(flow):
    live_queue(flow)
    job = flow.rpc("claim_appointment_notification")
    flow.rpc(
        "finish_appointment_notification",
        job["id"],
        job["lease_token"],
        "failed",
        None,
        None,
        "provider_rejected",
    )
    assert flow.rpc("get_booking_status", flow.call)["dispatch"]["status"] == "assigned"


def test_message_snapshot_does_not_include_medical_facts(flow):
    book(flow, True)
    n = flow.rpc(
        "prepare_confirmation", flow.call, "email", "Email please", "caller@example.com"
    )
    content = flow.db.execute(
        "select content from public.appointment_notifications where id=%s",
        (n["notification_id"],),
    ).fetchone()[0]
    assert "unsafe_to_wait" not in content and "issue_summary" not in content
    assert content["technician_name"] and content["arrival_window"]


def test_declining_confirmation_does_not_change_booking(flow):
    book(flow)
    assert (
        flow.rpc("prepare_confirmation", flow.call, "none", "No confirmation thanks")[
            "status"
        ]
        == "declined"
    )
    assert flow.rpc("get_booking_status", flow.call)["status"] == "booked"


def test_outbox_and_dispatch_mutations_are_not_public(flow):
    for role in ("anon", "authenticated", "service_role"):
        assert not flow.db.execute(
            "select has_table_privilege(%s,'public.dispatch_jobs','INSERT')", (role,)
        ).fetchone()[0]
        assert not flow.db.execute(
            "select has_table_privilege(%s,'public.appointment_notifications','UPDATE')",
            (role,),
        ).fetchone()[0]


def test_declining_cancels_unsent_confirmation(flow):
    live_queue(flow)
    assert (
        flow.rpc("prepare_confirmation", flow.call, "none", "Actually no message")[
            "status"
        ]
        == "declined"
    )
    assert flow.rpc("claim_appointment_notification") is None


def test_different_recipient_cannot_replace_already_queued_email(flow):
    flow.call = str(uuid4())
    flow.rev = 0
    book(flow)
    first = flow.rpc(
        "prepare_confirmation", flow.call, "email", "Email please", "first@example.com"
    )
    flow.rpc(
        "confirm_notification",
        flow.call,
        first["confirmation_token"],
        "accepted",
        "Yes",
        True,
    )
    assert (
        flow.rpc(
            "prepare_confirmation", flow.call, "email", "Change it", "other@example.com"
        )["status"]
        == "recipient_change_requires_review"
    )


def test_explicit_stub_saves_receipt_without_queuing_delivery(flow):
    book(flow)
    n = flow.rpc("prepare_confirmation", flow.call, "sms", "Text please")
    r = flow.rpc(
        "confirm_notification",
        flow.call,
        n["confirmation_token"],
        "accepted",
        "Yes",
        True,
        True,
    )
    assert r["status"] == "simulated" and r["external_message_sent"] is False
    assert flow.rpc("claim_appointment_notification") is None
    assert (
        flow.rpc("get_confirmation_status", flow.call)["notifications"][0]["status"]
        == "simulated"
    )
