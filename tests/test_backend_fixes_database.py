"""Backend fixes from the 2026-11-23 test findings, against real SQL in a rolled-back transaction.

Covers O1, O7, O9, O10, O19, O21, O22, O23, O24, O33, O39 and decisions D1, D3. The HTTP
tests drive the FastAPI webhook with a local transport onto the same transaction, so they
check the full tool contract (status, missing, expected, hint) end to end.
"""

import json
import os
import re
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from test_dispatch_database import book
from test_weather_contact_database import flow  # noqa: F401 (pytest fixture)

pytestmark = pytest.mark.skipif(
    urlparse(os.environ.get("TEST_DATABASE_URL", "")).hostname
    not in ("127.0.0.1", "localhost", "::1"),
    reason="Requires isolated TEST_DATABASE_URL",
)

MAYA = "+12125550101"
SECRET = "isolated-fixture-only-not-a-real-secret"


@pytest.fixture
def tool(flow):  # noqa: F811
    from psycopg import sql
    from psycopg.types.json import Jsonb

    from revin_take_home.app import Settings, create_app
    from revin_take_home.demo import DemoDirectory
    from revin_take_home.intake import SupabaseIntakeStore
    from revin_take_home.workflow import WorkflowStore

    def database(request):
        name = request.url.path.rsplit("/", 1)[-1]
        args = json.loads(request.content)
        statement = sql.SQL("select public.{}({})").format(
            sql.Identifier(name),
            sql.SQL(",").join(
                sql.SQL("{} => %s").format(sql.Identifier(key)) for key in args
            ),
        )
        value = flow.db.execute(
            statement,
            tuple(Jsonb(v) if isinstance(v, dict) else v for v in args.values()),
        ).fetchone()[0]
        return httpx.Response(200, json=value)

    db = httpx.AsyncClient(
        base_url="https://isolated.test/rest/v1/",
        transport=httpx.MockTransport(database),
    )
    app = create_app(
        Settings(assistant_id="c1d4d1d8-b2e4-48b9-9ffe-ca4bbea2dd43", webhook_secret=SECRET),
        directory=DemoDirectory(),
        intake_store=SupabaseIntakeStore(db),
        workflow_store=WorkflowStore(db),
    )
    with TestClient(app) as api:

        def call(name, args, call_id=None):
            r = api.post(
                "/vapi/tools",
                headers={"Authorization": "Bearer " + SECRET},
                json={
                    "message": {
                        "type": "tool-calls",
                        "call": {
                            "type": "inboundPhoneCall",
                            "id": call_id or flow.call,
                            "customer": {"number": flow.number},
                        },
                        "toolCallList": [
                            {"id": uuid4().hex, "function": {"name": name, "arguments": args}}
                        ],
                    }
                },
            )
            assert r.status_code == 200
            result = json.loads(r.json()["results"][0]["result"])
            if isinstance(result.get("state"), dict):
                flow.rev = result["state"]["revision"]
            return result

        yield call


def age(flow, seconds=60):  # noqa: F811
    """Pretend the caller took a while to answer (the guard needs a real gap)."""
    for table in ("intake_events", "contact_prefill_events"):
        flow.db.execute(
            f"update public.{table} set created_at=created_at-make_interval(secs=>%s) where call_id=%s",
            (seconds, flow.call),
        )


def real_gap(flow):  # noqa: F811
    flow.db.execute("set local summit.confirmation_gap_ms = 3000")


def assert_contract(result):
    assert {"status", "missing", "expected", "hint"} <= set(result)
    assert isinstance(result["missing"], list) and isinstance(result["expected"], dict)
    assert result["hint"] and result["hint"].count(".") <= 2


def maya_name(tool):
    return tool(
        "update_intake",
        {
            "expected_revision": 0,
            "caller_quote": "It's Maya Chen, my AC needs a tune-up",
            "changes": {
                "caller_name": "Maya Chen",
                "service_type": "maintenance",
                "service_category": "hvac",
                "issue_summary": "Annual AC maintenance",
            },
        },
    )


# O39 / O23: malformed confirmations and facts outside `changes` -----------------------


def test_confirmation_fields_inside_changes_are_accepted(flow, tool):  # noqa: F811
    maya_name(tool)
    r = tool(
        "update_intake",
        {
            "expected_revision": flow.rev,
            "caller_quote": "Yes, word for word, that is correct",
            "changes": {
                "address_confirmed": True,
                "callback_confirmed": True,
                "confirmation_address": "152 West 72nd Street",
                "confirmation_callback": MAYA,
            },
        },
    )
    assert r["status"] == "saved"
    details = r["state"]["details"]
    assert details["address_confirmed"] is True and details["callback_confirmed"] is True
    assert set(r["moved_into_place"]) == {
        "changes.confirmation_address",
        "changes.confirmation_callback",
    }


def test_missing_confirmation_fields_name_the_field_and_saved_value(flow, tool):  # noqa: F811
    maya_name(tool)
    r = tool(
        "update_intake",
        {
            "expected_revision": flow.rev,
            "caller_quote": "Yes",
            "changes": {"address_confirmed": True, "callback_confirmed": True},
        },
    )
    assert r["status"] == "confirmation_mismatch"
    assert_contract(r)
    assert r["missing"] == ["confirmation_address", "confirmation_callback"]
    assert r["expected"] == {
        "confirmation_address": "152 West 72nd Street",
        "confirmation_callback": MAYA,
    }
    assert "top level" in r["hint"]
    # Echoing expected exactly then works.
    r = tool(
        "update_intake",
        {
            "expected_revision": flow.rev,
            "caller_quote": "Yes",
            "changes": {"address_confirmed": True, "callback_confirmed": True},
        }
        | r["expected"],
    )
    assert r["status"] == "saved"


def test_top_level_facts_are_moved_into_changes_not_dropped(flow, tool):  # noqa: F811
    r = tool(
        "update_intake",
        {
            "expected_revision": 0,
            "caller_quote": "My AC stopped cooling",
            "issue_summary": "AC not cooling",
            "cooling_unavailable": True,
            "changes": {"service_type": "repair"},
        },
    )
    assert r["status"] == "saved"
    details = r["state"]["details"]
    assert details["issue_summary"] == "AC not cooling"
    assert details["cooling_unavailable"] is True and details["service_type"] == "repair"
    assert set(r["moved_into_place"]) == {"issue_summary", "cooling_unavailable"}


def test_unknown_fields_still_fail_with_the_field_named(flow, tool):  # noqa: F811
    r = tool(
        "update_intake",
        {"expected_revision": 0, "caller_quote": "hi", "changes": {"customer_id": "x"}},
    )
    assert r["status"] == "invalid_arguments"
    assert_contract(r)
    assert r["bad_fields"] == ["changes.customer_id"]
    assert "changes.customer_id" in r["hint"]


# O1: confirmations must be the caller's answer in a later turn --------------------------


def test_same_turn_confirmation_is_refused_then_accepted_after_a_real_answer(flow, tool):  # noqa: F811
    real_gap(flow)
    maya_name(tool)
    confirm = {
        "changes": {"address_confirmed": True, "callback_confirmed": True},
        "confirmation_address": "152 West 72nd Street",
        "confirmation_callback": MAYA,
    }
    r = tool("update_intake", {"expected_revision": flow.rev, "caller_quote": "Yes"} | confirm)
    assert r["status"] == "needs_caller_yes" and r["reason"] == "same_turn_as_address_save"
    assert_contract(r)
    assert r["expected"]["confirmation_address"] == "152 West 72nd Street"
    assert r["state"]["details"]["address_confirmed"] is False
    age(flow)
    r = tool(
        "update_intake",
        {
            "expected_revision": flow.rev,
            "caller_quote": "Just to confirm, 152 West 72nd Street, apartment 4B, 10023, and the number you're calling from?",
        }
        | confirm,
    )
    assert r["status"] == "needs_caller_yes" and r["reason"] == "quote_is_readback"
    r = tool(
        "update_intake",
        {"expected_revision": flow.rev, "caller_quote": "Yes, that's right."} | confirm,
    )
    assert r["status"] == "saved"
    assert r["state"]["details"]["address_confirmed"] is True


@pytest.mark.parametrize(
    "quote,agent",
    [
        ("Yes", False),
        ("Yep, that's me", False),
        ("Yes, that's right, and that number works", False),
        ("Just to confirm, it's 99 Example Street?", True),
        ("So I have you at 99 Example Street", True),
        ("Okay, let me confirm the address", True),
        ("Is that right? two one two five five five zero one nine nine", True),
        ("Your address is 99 Example Street", True),
        # R2: callers repeat their details when they agree; that is not a readback.
        ("99 Example Street, unit none, 10023", False),
        ("You can reach me at 212-555-0199", False),
        ("Yes, that's correct. 99 Example Street, and the callback number is 212-555-0199.", False),
        ("The address is correct, 99 Example Street", False),
        ("The address is 99 Example Street, New York, NY 10023", False),
    ],
)
def test_readback_quote_detection(flow, quote, agent):  # noqa: F811
    d = {
        "service_address": "99 Example Street",
        "service_zip": "10023",
        "callback_number": "+12125550199",
    }
    from psycopg.types.json import Jsonb

    assert (
        flow.db.execute(
            "select public.quote_is_agent_readback(%s,%s,true,true)", (quote, Jsonb(d))
        ).fetchone()[0]
        is agent
    )


def test_replay_of_a_saved_confirmation_is_not_judged_again(flow):  # noqa: F811
    real_gap(flow)
    flow.save({"service_address": "99 Example Street", "service_zip": "10023"})
    age(flow)
    op = uuid4().hex
    args = (flow.call, op, flow.number, flow.rev, {"address_confirmed": True}, "Yes", "99 Example Street", None)
    first = flow.rpc("update_call_intake", *args)
    assert first["status"] == "saved"
    flow.db.execute("update public.intake_events set created_at=now() where call_id=%s", (flow.call,))
    assert flow.rpc("update_call_intake", *args)["replayed"] is True


# O33 / O10: address hygiene -----------------------------------------------------------------


def test_new_street_clears_stale_account_unit_and_zip(flow):  # noqa: F811
    r = flow.save({"caller_name": "Maya Chen", "service_type": "maintenance"})
    assert r["state"]["details"]["service_unit"] == "4B"
    r = flow.save({"service_address": "310 West 80th Street"})
    d = r["state"]["details"]
    assert d["service_address"] == "310 West 80th Street"
    assert "service_unit" not in d and "service_zip" not in d
    assert d["address_confirmed"] is False
    assert "service_unit" in r["readiness"]["missing_fields"]


def test_restated_parts_survive_a_street_change(flow):  # noqa: F811
    flow.save({"caller_name": "Maya Chen", "service_type": "maintenance"})
    d = flow.save({"service_address": "154 West 72nd Street", "service_unit": "4B"})["state"]["details"]
    assert d["service_unit"] == "4B" and "service_zip" not in d


def test_caller_supplied_address_is_untouched_by_street_change(flow):  # noqa: F811
    flow.save({"service_address": "1 Main Street", "service_unit": "2", "service_zip": "11201"})
    d = flow.save({"service_address": "3 Main Street"})["state"]["details"]
    assert d["service_unit"] == "2" and d["service_zip"] == "11201"


def test_covered_zip_fills_city_and_state_and_rederives_on_change(flow):  # noqa: F811
    r = flow.save({"service_address": "1 Main Street", "service_zip": "11201"})
    d = r["state"]["details"]
    assert (d["service_city"], d["service_state"]) == ("Brooklyn", "NY")
    assert r["readiness"]["coverage"] == "covered"
    assert "service_state" not in r["readiness"]["missing_fields"]
    d = flow.save({"service_zip": "11375"})["state"]["details"]
    assert (d["service_city"], d["service_state"]) == ("Queens", "NY")
    d = flow.save({"service_city": "Forest Hills"})["state"]["details"]
    assert d["service_city"] == "Forest Hills"
    d = flow.save({"service_zip": "10023"})["state"]["details"]
    assert d["service_city"] == "Forest Hills"  # the caller's own answer is never replaced


def test_uncovered_zip_derives_nothing(flow):  # noqa: F811
    d = flow.save({"service_zip": "10451"})["state"]["details"]
    assert "service_city" not in d and "service_state" not in d


# O21 / O22 / O24 / O9: follow-ups -------------------------------------------------------


def test_followup_creates_the_record_from_caller_id(flow, tool):  # noqa: F811
    r = tool("request_followup", {"expected_revision": 0, "reason": "no_availability", "caller_quote": "Have someone call me"})
    assert r["status"] == "recorded" and r["created_call_record"] is True
    details = flow.db.execute(
        "select details from public.call_intakes where call_id=%s", (flow.call,)
    ).fetchone()[0]
    assert details["callback_number"] == flow.number


def test_sales_call_is_out_of_scope_and_pages_nobody(flow, tool):  # noqa: F811
    r = tool(
        "request_followup",
        {"expected_revision": 7, "reason": "caller_requested", "caller_quote": "Have the owner call me about SEO"},
    )
    assert r["status"] == "out_of_scope"
    assert_contract(r)
    assert "decline" in r["hint"]
    assert not flow.db.execute(
        "select 1 from public.dispatcher_alerts where call_id=%s", (flow.call,)
    ).fetchone()


def test_callback_request_needs_name_and_confirmed_number(flow, tool):  # noqa: F811
    tool(
        "update_intake",
        {"expected_revision": 0, "caller_quote": "My heat is out", "changes": {"heating_unavailable": True, "issue_summary": "No heat"}},
    )
    args = {"reason": "caller_requested", "caller_quote": "Can a person call me?"}
    r = tool("request_followup", {"expected_revision": flow.rev} | args)
    assert r["status"] == "needs_details"
    assert_contract(r)
    assert r["missing"] == ["caller_name", "callback_confirmed"]
    assert r["expected"] == {"confirmation_callback": flow.number}
    tool("update_intake", {"expected_revision": flow.rev, "caller_quote": "Dana", "changes": {"caller_name": "Dana"}})
    tool(
        "update_intake",
        {"expected_revision": flow.rev, "caller_quote": "Yes, this number", "changes": {"callback_confirmed": True}, "confirmation_callback": flow.number},
    )
    r = tool("request_followup", {"expected_revision": flow.rev} | args)
    assert r["status"] == "recorded" and r["dispatcher_alert"] == "queued"


def test_repeated_failures_stop_and_technical_followup_pages_without_pii(flow, tool):  # noqa: F811
    tool("update_intake", {"expected_revision": 0, "caller_quote": "AC broken", "changes": {"issue_summary": "AC broken"}})
    stale = {"expected_revision": 0, "caller_quote": "It's Dana", "changes": {"caller_name": "Dana Whitfield"}}
    assert tool("update_intake", stale)["status"] == "revision_conflict"
    r = tool("update_intake", stale)
    assert r["status"] == "revision_conflict" and r["expected"] == {"expected_revision": 1}
    r = tool("update_intake", stale)
    assert r["status"] == "stop_retrying" and r["failed_status"] == "revision_conflict"
    assert "technical_issue" in r["hint"]
    r = tool("request_followup", {"expected_revision": flow.rev, "reason": "technical_issue", "caller_quote": "Okay"})
    assert r["status"] == "recorded" and r["dispatcher_alert"] == "queued"
    title, body = flow.db.execute(
        "select title, body from public.dispatcher_alerts where call_id=%s and kind='callback_requested'",
        (flow.call,),
    ).fetchone()
    assert "technical" in title.lower()
    assert "Dana" not in body + title and not re.search(r"\d{4}", body.replace(flow.call[:8], ""))


def test_success_resets_the_failure_run(flow, tool):  # noqa: F811
    stale = {"expected_revision": 5, "caller_quote": "AC broken", "changes": {"issue_summary": "AC broken"}}
    assert tool("update_intake", stale)["status"] == "revision_conflict"
    assert tool("update_intake", stale)["status"] == "revision_conflict"
    assert tool("update_intake", stale | {"expected_revision": flow.rev})["status"] == "saved"
    assert tool("update_intake", stale)["status"] == "revision_conflict"


# D1 / O19 / O3: scheduling ---------------------------------------------------------------


def test_priority_search_offers_regular_windows_only(flow, tool):  # noqa: F811
    flow.weather()
    flow.ready()
    flow.save({"unsafe_to_wait": True})
    # An old on-call window, as the previous migration seeded them, is never offered.
    flow.db.execute(
        """insert into public.appointment_slots(zone,service_type,starts_at,ends_at,capacity,inventory_scope,priority_only)
           values('manhattan','repair',date_trunc('hour',now())+interval '1 hour',date_trunc('hour',now())+interval '3 hours',8,'demo',true)
           on conflict do nothing"""
    )
    r = tool("search_slots", {"expected_revision": flow.rev})
    assert r["status"] == "available"
    for slot in r["slots"]:
        local = flow.db.execute(
            "select extract(hour from %s::timestamptz at time zone 'America/New_York'), s.priority_only from public.appointment_slots s where s.id=%s",
            (slot["starts_at"], slot["slot_id"]),
        ).fetchone()
        assert local[0] in (9, 13) and local[1] is False
        assert re.match(r"^(today|tomorrow, [A-Z][a-z]+day, |[A-Z][a-z]+day, )", slot["spoken"])
        assert "between" in slot["spoken"] and not re.search(r"\d", slot["spoken"])
    proposal = tool(
        "propose_booking",
        {"expected_revision": flow.rev, "search_id": r["search_id"], "slot_id": r["slots"][0]["slot_id"]},
    )
    assert proposal["spoken"] == r["slots"][0]["spoken"]


def test_on_call_technicians_no_longer_cover_overnight(flow):  # noqa: F811
    flow.db.execute(
        """insert into public.appointment_slots(zone,service_type,starts_at,ends_at,capacity,inventory_scope)
           values('manhattan','repair','2026-12-01 02:00-05','2026-12-01 04:00-05',1,'test_overnight')"""
    )
    covers = flow.db.execute(
        """select bool_or(public.technician_can_cover(t,s,true)) from public.technicians t, public.appointment_slots s
           where s.inventory_scope='test_overnight' and t.on_call"""
    ).fetchone()[0]
    assert covers is False


@pytest.mark.parametrize(
    "day,holiday",
    [("2026-11-26", True), ("2027-11-25", True), ("2026-12-25", True), ("2026-11-27", False), ("2026-11-19", False)],
)
def test_demo_holidays(flow, day, holiday):  # noqa: F811
    assert flow.db.execute("select public.is_demo_holiday(%s::date)", (day,)).fetchone()[0] is holiday


def test_seeding_never_offers_holiday_windows(flow):  # noqa: F811
    flow.db.execute(
        """insert into public.appointment_slots(zone,service_type,starts_at,ends_at,capacity,inventory_scope)
           select 'manhattan','repair',(d+time '09:00') at time zone 'America/New_York',(d+time '12:00') at time zone 'America/New_York',40,'evaluation'
           from generate_series(current_date+1,current_date+40,'1 day') d where public.is_demo_holiday(d::date)
           on conflict(zone,service_type,starts_at,inventory_scope) do update set enabled=true"""
    )
    flow.rpc("seed_demo_capacity", "evaluation")
    assert not flow.db.execute(
        """select 1 from public.appointment_slots where inventory_scope='evaluation' and enabled and starts_at>now()
           and public.is_demo_holiday((starts_at at time zone 'America/New_York')::date)"""
    ).fetchone()


# D3 / O7: emergencies -------------------------------------------------------------------


def test_emergency_after_booking_holds_the_appointment(flow):  # noqa: F811
    book(flow)
    r = flow.rpc("report_safety_emergency", flow.call, uuid4().hex, "gas_smell", "I smell gas now", flow.number)
    assert r["appointment_on_hold"] is True
    status = flow.rpc("get_booking_status", flow.call)
    assert status["status"] == "on_hold" and status["hold_reason"] == "safety_emergency"
    title, body, number = flow.db.execute(
        "select title, body, callback_number from public.dispatcher_alerts where call_id=%s and kind='emergency'",
        (flow.call,),
    ).fetchone()
    assert "ON HOLD" in body and "Callback number on file" in body
    assert number == flow.number
    text = title + body
    assert "Test Caller" not in text and "Example" not in text and "555" not in text


def test_emergency_without_booking_reports_no_hold(flow):  # noqa: F811
    r = flow.rpc("report_safety_emergency", flow.call, uuid4().hex, "carbon_monoxide_alarm", "Alarm is going off", flow.number)
    assert r["appointment_on_hold"] is False
    body = flow.db.execute(
        "select body from public.dispatcher_alerts where call_id=%s and kind='emergency'", (flow.call,)
    ).fetchone()[0]
    assert "no booking made" in body


def test_first_call_emergency_gets_callback_and_location_for_dispatcher(flow):  # noqa: F811
    r = flow.rpc("report_safety_emergency", flow.call, uuid4().hex, "gas_smell", "I smell gas", MAYA)
    # The agent is not handed an address to read back during an evacuation...
    assert "service_address" not in r["state"]["details"]
    assert r["state"]["details"]["callback_number"] == MAYA
    # ...but the record and the alert row have what the dispatcher needs.
    d = flow.db.execute("select details from public.call_intakes where call_id=%s", (flow.call,)).fetchone()[0]
    assert d["service_address"] == "152 West 72nd Street" and d["address_confirmed"] is False
    assert d["callback_confirmed"] is False
    body, number = flow.db.execute(
        "select body, callback_number from public.dispatcher_alerts where call_id=%s and kind='emergency'",
        (flow.call,),
    ).fetchone()
    assert body.startswith("New York County") and number == MAYA
    assert "152" not in body and "Maya" not in body and "555" not in body


def test_emergency_with_shared_or_unknown_number_prefills_callback_only(flow):  # noqa: F811
    flow.rpc("report_safety_emergency", flow.call, uuid4().hex, "gas_smell", "I smell gas", "+12125550999")
    d = flow.db.execute("select details from public.call_intakes where call_id=%s", (flow.call,)).fetchone()[0]
    assert d["callback_number"] == "+12125550999" and "service_address" not in d


# Round 2 voice findings R1, R2 ---------------------------------------------------------


def test_plain_address_save_is_never_judged_as_a_confirmation(flow, tool):  # noqa: F811
    """R1: a save with no confirmation flags must save, whatever the quote says."""
    real_gap(flow)
    r = tool(
        "update_intake",
        {
            "expected_revision": 0,
            "caller_quote": "The address is 123 Example Street, Los Angeles, California 90210.",
            "changes": {"service_address": "123 Example Street", "service_unit": "none", "service_zip": "90210"},
        },
    )
    assert r["status"] == "saved"
    assert r["state"]["details"]["service_zip"] == "90210"


def test_caller_repeating_details_confirms(flow, tool):  # noqa: F811
    """R2: "Yes, <address>, and the number is <number>" is the caller's own yes."""
    real_gap(flow)
    tool(
        "update_intake",
        {
            "expected_revision": 0,
            "caller_quote": "It's 410 East 89th Street, apartment 2C, 10128",
            "changes": {"caller_name": "Jordan", "service_address": "410 East 89th Street", "service_unit": "2C", "service_zip": "10128", "callback_number": "+12125550188"},
        },
    )
    age(flow)
    r = tool(
        "update_intake",
        {
            "expected_revision": flow.rev,
            "caller_quote": "Yes, that's correct. 410 East 89th Street, apartment 2C, and you can reach me at 212-555-0188.",
            "changes": {"address_confirmed": True, "callback_confirmed": True},
            "confirmation_address": "410 East 89th Street",
            "confirmation_callback": "+12125550188",
        },
    )
    assert r["status"] == "saved"
    assert r["state"]["details"]["address_confirmed"] is True
    assert r["state"]["details"]["callback_confirmed"] is True


def test_refused_confirmation_still_saves_the_new_facts(flow, tool):  # noqa: F811
    """At the database, a yes bundled with a new address saves the address, not the yes.
    (The webhook rejects such bundles earlier with a named error; this is the backstop.)"""
    real_gap(flow)
    r = flow.rpc(
        "update_call_intake", flow.call, uuid4().hex, flow.number, 0,
        {"service_address": "152 West 72nd Street", "service_unit": "4B", "service_zip": "10023", "address_confirmed": True},
        "152 West 72nd Street, apartment 4B, 10023", "152 West 72nd Street", None,
    )
    assert r["status"] == "needs_caller_yes" and r["reason"] == "same_turn_as_address_save"
    assert r["saved_other_changes"] is True
    details = r["state"]["details"]
    assert details["service_address"] == "152 West 72nd Street" and details["service_unit"] == "4B"
    assert details["address_confirmed"] is False
    age(flow)
    r = tool(
        "update_intake",
        {
            "expected_revision": r["state"]["revision"],
            "caller_quote": "Yes, that's right.",
            "changes": {"address_confirmed": True},
            "confirmation_address": "152 West 72nd Street",
        },
    )
    assert r["status"] == "saved" and r["state"]["details"]["address_confirmed"] is True


def test_saved_facts_hint_says_they_were_saved():
    from revin_take_home.guidance import shape

    out = shape(
        "update_intake",
        {"status": "needs_caller_yes", "reason": "same_turn_as_address_save", "saved_other_changes": True,
         "state": {"revision": 2, "details": {"service_address": "1 Main St"}}},
    )
    assert "saved" in out["hint"]
