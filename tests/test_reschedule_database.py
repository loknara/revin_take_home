"""Self-service rescheduling and cancelling, against real SQL in a rolled-back transaction.

A visit booked on one call is found, moved or cancelled on a later call from the same number.
"""

import os
from urllib.parse import urlparse
from uuid import uuid4

import pytest

from test_dispatch_database import book
from test_weather_contact_database import flow  # noqa: F401 (pytest fixture)

pytestmark = pytest.mark.skipif(
    urlparse(os.environ.get("TEST_DATABASE_URL", "")).hostname
    not in ("127.0.0.1", "localhost", "::1"),
    reason="Requires isolated TEST_DATABASE_URL",
)


def call_back(flow):  # noqa: F811
    """Book a visit, then start a new call from the same number."""
    booked, _ = book(flow)
    first_call = flow.call
    flow.call, flow.rev = "test_flow_" + uuid4().hex, 0
    return booked, first_call


def count(flow, sql, *args):  # noqa: F811
    return flow.db.execute(sql, args).fetchone()[0]


def test_lookup_is_bound_to_the_caller_id(flow):  # noqa: F811
    booked, _ = call_back(flow)
    found = flow.rpc("find_my_appointment", flow.call, flow.number)
    assert found["status"] == "found" and len(found["appointments"]) == 1
    visit = found["appointments"][0]
    assert visit["appointment_id"] == booked["reference"]
    assert visit["service_address"] == "99 Example Street" and visit["starts_at"]
    assert flow.rpc("find_my_appointment", flow.call, "+12125550000")["status"] == "none_found"
    assert flow.rpc("find_my_appointment", flow.call, None)["status"] == "no_caller_id"
    other = flow.rpc("change_appointment", flow.call, uuid4().hex, "+12125550000", 0,
                     booked["reference"], "cancel", "Cancel it")
    assert other["status"] == "not_found"
    assert count(flow, "select count(*) from public.appointments where id=%s", booked["reference"]) == 1


def test_reschedule_books_the_new_window_and_releases_the_old_one_together(flow):  # noqa: F811
    booked, first_call = call_back(flow)
    # The live 3-second confirmation rule is on here (tests switch it off by default):
    # it refused the reschedule's confirmation step in a live chat test.
    flow.db.execute("set local summit.confirmation_gap_ms = 3000")
    old = booked["reference"]
    r = flow.rpc("change_appointment", flow.call, uuid4().hex, flow.number, 0, old,
                 "reschedule", "Yes, that's the one, I need to move it")
    assert r["status"] == "saved" and r["rescheduling"]["appointment_id"] == old
    details = r["state"]["details"]
    assert details["service_address"] == "99 Example Street"
    assert details["address_confirmed"] is True and details["callback_confirmed"] is True
    assert r["readiness"]["can_book"] is True
    flow.rev = r["state"]["revision"]
    slots = flow.rpc("search_slots", flow.call, flow.rev)
    proposal = flow.rpc("propose_booking", flow.call, flow.rev, slots["search_id"], slots["slots"][0]["slot_id"])
    # Declining keeps the old visit.
    flow.rpc("confirm_booking", flow.call, flow.rev, proposal["proposal_id"], "declined", "No, not that one")
    assert count(flow, "select count(*) from public.appointments where id=%s", old) == 1
    proposal = flow.rpc("propose_booking", flow.call, flow.rev, slots["search_id"], slots["slots"][0]["slot_id"])
    done = flow.rpc("confirm_booking", flow.call, flow.rev, proposal["proposal_id"], "accepted", "Yes, book that")
    assert done["status"] == "booked" and done["replaced"]["service_address"] == "99 Example Street"
    assert count(flow, "select count(*) from public.appointments where id=%s", old) == 0
    assert count(flow, "select count(*) from public.dispatch_jobs where call_id=%s", first_call) == 0
    assert count(flow, "select count(*) from public.appointments where call_id=%s", flow.call) == 1
    history = flow.db.execute(
        "select action, replaced_by::text, original_call_id from public.appointment_history where appointment_id=%s", (old,)
    ).fetchone()
    assert history == ("rescheduled", done["reference"], first_call)
    assert flow.rpc("find_my_appointment", flow.call, flow.number)["status"] == "none_found"


def test_cancel_removes_the_visit_and_frees_the_technician(flow):  # noqa: F811
    booked, first_call = call_back(flow)
    r = flow.rpc("change_appointment", flow.call, uuid4().hex, flow.number, 0,
                 booked["reference"], "cancel", "Yes, please cancel it")
    assert r["status"] == "cancelled" and r["cancelled"]["starts_at"]
    assert count(flow, "select count(*) from public.appointments where call_id=%s", first_call) == 0
    assert count(flow, "select count(*) from public.dispatch_jobs where call_id=%s", first_call) == 0
    assert count(flow, "select count(*) from public.appointment_history where action='cancelled' and appointment_id=%s",
                 booked["reference"]) == 1
    again = flow.rpc("change_appointment", flow.call, uuid4().hex, flow.number, 0,
                     booked["reference"], "cancel", "Cancel it")
    assert again["status"] == "not_found"


def test_tables_are_not_exposed_to_clients(flow):  # noqa: F811
    for table in ("appointment_history", "appointment_changes"):
        assert count(flow, "select count(*) from information_schema.role_table_grants where table_name=%s "
                     "and grantee in ('anon','authenticated')", table) == 0
