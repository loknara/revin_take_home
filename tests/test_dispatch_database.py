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


def book(flow, priority=False):
    flow.weather()
    flow.ready()
    if priority:
        flow.save({"unsafe_to_wait": True})
    slots = flow.rpc("search_slots", flow.call, flow.rev)
    assert slots["status"] == "available"
    proposal = flow.rpc(
        "propose_booking",
        flow.call,
        flow.rev,
        slots["search_id"],
        slots["slots"][0]["slot_id"],
    )
    assert proposal["status"] == "offered"
    result = flow.rpc(
        "confirm_booking",
        flow.call,
        flow.rev,
        proposal["proposal_id"],
        "accepted",
        "Yes, book that window",
    )
    assert result["status"] == "booked"
    return result, proposal


def test_booking_atomically_assigns_a_technician_and_is_idempotent(flow):
    result, proposal = book(flow)
    assert result["dispatch_status"] == "assigned"
    assert (
        result["dispatch"]["status"] == "assigned"
        and result["dispatch"]["synthetic_roster"] is True
    )
    assert result["dispatch"]["technician_name"]
    again = flow.rpc(
        "confirm_booking",
        flow.call,
        flow.rev,
        proposal["proposal_id"],
        "accepted",
        "Yes, book that window",
    )
    assert again["dispatch"]["job_id"] == result["dispatch"]["job_id"]
    assert (
        flow.db.execute(
            "select count(*) from public.dispatch_jobs where call_id=%s", (flow.call,)
        ).fetchone()[0]
        == 1
    )


def test_priority_gets_earliest_regular_window_and_persistent_assignment(flow):
    # D1: there is no on-call person, so priority means the earliest REGULAR window.
    result, _ = book(flow, priority=True)
    assert result["dispatch"]["priority"] == "priority"
    hour, dow, priority_only = flow.db.execute(
        "select extract(hour from s.starts_at at time zone 'America/New_York'),"
        " extract(isodow from s.starts_at at time zone 'America/New_York'), s.priority_only"
        " from public.dispatch_jobs j join public.appointments a on a.id=j.appointment_id"
        " join public.appointment_slots s on s.id=a.slot_id where j.call_id=%s",
        (flow.call,),
    ).fetchone()
    assert hour in (9, 13) and dow != 7 and priority_only is False


def test_no_technician_means_no_available_slots(flow):
    flow.weather()
    flow.ready()
    flow.db.execute("update public.technicians set active=false")
    assert flow.rpc("search_slots", flow.call, flow.rev)["status"] == "no_availability"


def test_technician_disappears_after_offer_no_partial_booking(flow):
    flow.weather()
    flow.ready()
    slots = flow.rpc("search_slots", flow.call, flow.rev)
    p = flow.rpc(
        "propose_booking",
        flow.call,
        flow.rev,
        slots["search_id"],
        slots["slots"][0]["slot_id"],
    )
    flow.db.execute("update public.technicians set active=false")
    r = flow.rpc(
        "confirm_booking", flow.call, flow.rev, p["proposal_id"], "accepted", "Yes"
    )
    assert r["status"] == "technician_unavailable"
    assert flow.rpc("get_booking_status", flow.call)["status"] == "not_booked"
    assert (
        flow.db.execute(
            "select count(*) from public.booking_agreements where call_id=%s",
            (flow.call,),
        ).fetchone()[0]
        == 0
    )


def test_overlapping_windows_get_different_technicians(flow):
    first, _ = book(flow)
    flow.call = "test_flow_" + uuid4().hex
    flow.rev = 0
    second, _ = book(flow)
    assert first["starts_at"] == second["starts_at"]
    assert first["dispatch"]["technician_id"] != second["dispatch"]["technician_id"]


def test_gas_never_gets_appointment(flow):
    flow.weather()
    flow.ready()
    flow.save({"gas_smell_reported": True})
    assert flow.rpc("search_slots", flow.call, flow.rev)["status"] == "not_eligible"
    assert flow.rpc("get_booking_status", flow.call)["status"] == "not_booked"


def test_concurrent_bookings_cannot_take_same_technician_across_slots():
    import psycopg
    from psycopg.types.json import Jsonb
    from concurrent.futures import ThreadPoolExecutor

    scope = "test_workflow_" + uuid4().hex
    calls = [scope + "_a", scope + "_b"]
    slot_ids = [str(uuid4()), str(uuid4())]
    with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True) as db:
        original = db.execute("select id,active from public.technicians").fetchall()
        chosen = original[0][0]
        try:
            db.execute("update public.technicians set active=(id=%s)", (chosen,))
            starts = db.execute(
                "select ((now() at time zone 'America/New_York')::date+1+time '9:00') at time zone 'America/New_York'"
            ).fetchone()[0]
            # Use next Monday if tomorrow is Sunday, inside normal shift hours.
            if (
                starts.astimezone(
                    __import__("zoneinfo").ZoneInfo("America/New_York")
                ).isoweekday()
                == 7
            ):
                starts += __import__("datetime").timedelta(days=1)
            proposals = []
            for i, call in enumerate(calls):
                db.execute(
                    """insert into public.appointment_slots(id,zone,service_type,starts_at,ends_at,capacity,inventory_scope)
                 values(%s,'manhattan','maintenance',%s+%s*interval '1 minute',%s+interval '3 hours',2,%s)""",
                    (slot_ids[i], starts, i, starts, scope),
                )
                facts = dict(
                    caller_name="Concurrency Fixture",
                    property_type="residential",
                    service_type="maintenance",
                    service_category="hvac",
                    service_address="99 Example Street",
                    service_unit="none",
                    service_city="New York",
                    service_state="NY",
                    service_zip="10023",
                    issue_summary="Annual maintenance",
                    callback_number="+12125550199",
                )
                db.execute(
                    "select public.update_call_intake(%s,%s,%s,0,%s,%s)",
                    (call, str(uuid4()), "+12125550199", Jsonb(facts), "Fixture"),
                )
                db.execute(
                    "select public.update_call_intake(%s,%s,%s,1,%s,%s,%s,%s)",
                    (
                        call,
                        str(uuid4()),
                        "+12125550199",
                        Jsonb({"address_confirmed": True, "callback_confirmed": True}),
                        "Yes",
                        "99 Example Street",
                        "+12125550199",
                    ),
                )
                found = db.execute(
                    "select public.search_slots(%s,2)", (call,)
                ).fetchone()[0]
                proposal = db.execute(
                    "select public.propose_booking(%s,2,%s,%s)",
                    (call, found["search_id"], slot_ids[i]),
                ).fetchone()[0]
                assert proposal["status"] == "offered"
                proposals.append(proposal["proposal_id"])

            def confirm(i):
                with psycopg.connect(
                    os.environ["TEST_DATABASE_URL"], autocommit=True
                ) as connection:
                    return connection.execute(
                        "select public.confirm_booking(%s,2,%s,%s,%s)",
                        (calls[i], proposals[i], "accepted", "Yes"),
                    ).fetchone()[0]

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(confirm, range(2)))
            assert sorted(r["status"] for r in results) == [
                "booked",
                "technician_unavailable",
            ]
            assert (
                db.execute(
                    "select count(*) from public.dispatch_jobs where inventory_scope=%s",
                    (scope,),
                ).fetchone()[0]
                == 1
            )
        finally:
            db.execute(
                "delete from public.call_intakes where call_id=any(%s)", (calls,)
            )
            db.execute(
                "delete from public.appointment_slots where inventory_scope=%s",
                (scope,),
            )
            for id, active in original:
                db.execute(
                    "update public.technicians set active=%s where id=%s", (active, id)
                )
