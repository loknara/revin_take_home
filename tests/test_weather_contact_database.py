"""Isolated database tests only: weather fixtures roll back and never touch hosted data."""

import os
from urllib.parse import urlparse
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(
    urlparse(os.environ.get("TEST_DATABASE_URL", "")).hostname
    not in ("127.0.0.1", "localhost", "::1"),
    reason="Requires isolated TEST_DATABASE_URL",
)


@pytest.fixture
def flow():
    psycopg = pytest.importorskip("psycopg")
    from psycopg.types.json import Jsonb

    db = psycopg.connect(os.environ["TEST_DATABASE_URL"])

    class Flow:
        call = "test_flow_" + uuid4().hex
        rev = 0
        number = "+12125550101"

        def rpc(self, name, *args):
            return db.execute(
                "select public." + name + "(" + ",".join(["%s"] * len(args)) + ")",
                tuple(Jsonb(a) if isinstance(a, dict) else a for a in args),
            ).fetchone()[0]

        def save(self, changes, address=None, callback=None):
            r = self.rpc(
                "update_call_intake",
                self.call,
                uuid4().hex,
                self.number,
                self.rev,
                changes,
                "Synthetic caller statement",
                address,
                callback,
            )
            self.rev = r["state"]["revision"]
            return r

        def weather(self, hot=False, cold=False, expired=False, county=None):
            counties = [county] if county else ["New York", "Kings", "Queens"]
            for c in counties:
                db.execute(
                    """insert into public.service_weather values
                    (%s,now()-interval '1 minute',now()+%s*interval '1 minute',%s,%s,'{"fixture":true}')
                    on conflict(county) do update set fetched_at=excluded.fetched_at,
                    valid_until=excluded.valid_until,hot=excluded.hot,cold=excluded.cold,evidence=excluded.evidence""",
                    (c, -0.5 if expired else 29, hot, cold),
                )

        def ready(self):
            self.number = "+12125550199"
            self.save(
                {
                    "caller_name": "Test Caller",
                    "property_type": "residential",
                    "service_category": "hvac",
                    "service_type": "repair",
                    "cooling_unavailable": True,
                    "vulnerable_resident": False,
                    "issue_summary": "AC not cooling",
                    "service_address": "99 Example Street",
                    "service_unit": "none",
                    "service_city": "New York",
                    "service_state": "NY",
                    "service_zip": "10023",
                }
            )
            return self.save(
                {"address_confirmed": True, "callback_confirmed": True},
                "99 Example Street",
                self.number,
            )

    f = Flow()
    f.db = db
    db.execute("delete from public.service_weather")
    db.execute(
        "update public.service_locations set is_primary=true where id='demo_maya_home'"
    )
    try:
        yield f
    finally:
        db.rollback()
        db.close()


def test_name_phone_prefills_address_and_callback_but_confirms_neither(flow):
    result = flow.save(
        {
            "caller_name": "Maya Chen",
            "service_type": "maintenance",
            "service_category": "hvac",
            "issue_summary": "Annual AC maintenance",
        }
    )
    d = result["state"]["details"]
    assert d["service_address"] == "152 West 72nd Street" and d["service_unit"] == "4B"
    assert d["callback_number"] == flow.number
    assert d["address_confirmed"] is False and d["callback_confirmed"] is False
    assert result["membership"]["membership_status"] == "unknown"
    assert result["readiness"]["can_book"] is False
    confirmed = flow.save({"address_confirmed": True}, d["service_address"])
    membership = flow.rpc("resolve_call_membership", flow.call, flow.rev)
    assert membership["membership_status"] == "active"
    assert confirmed["readiness"]["can_book"] is False  # callback not yet confirmed
    assert flow.save({"callback_confirmed": True}, callback=flow.number)["readiness"][
        "can_book"
    ]
    assert (
        flow.db.execute(
            "select count(*) from public.contact_prefill_events where call_id=%s",
            (flow.call,),
        ).fetchone()[0]
        == 1
    )


@pytest.mark.parametrize(
    "name,number",
    [
        ("Wrong Name", "+12125550101"),
        ("Maya Chen", "+12125550999"),
        ("Maya Chen", None),
    ],
)
def test_unmatched_caller_gets_no_stored_address(flow, name, number):
    flow.number = number
    d = flow.save({"caller_name": name})["state"]["details"]
    assert "service_address" not in d


def test_shared_number_requires_unique_name_and_multiple_locations_require_primary(
    flow,
):
    flow.number = "+12125550102"
    assert (
        "service_address"
        not in flow.save({"caller_name": "Someone"})["state"]["details"]
    )
    assert (
        flow.save({"caller_name": "Leah Brooks"})["state"]["details"]["service_unit"]
        == "6C"
    )
    flow.db.execute(
        "update public.service_locations set is_primary=false where id='demo_maya_home'"
    )
    flow.number = "+12125550101"
    flow.call = "test_flow_" + uuid4().hex
    flow.rev = 0
    assert (
        "service_address"
        not in flow.save({"caller_name": "Maya Chen"})["state"]["details"]
    )


def test_partial_address_cannot_be_overwritten_by_account(flow):
    d = flow.save(
        {"caller_name": "Maya Chen", "service_address": "125 West 72nd Street"}
    )["state"]["details"]
    assert d["service_address"] == "125 West 72nd Street" and "service_unit" not in d


def test_wrong_name_correction_removes_prefilled_address(flow):
    flow.save({"caller_name": "Maya Chen"})
    d = flow.save({"caller_name": "Not Maya"})["state"]["details"]
    assert "service_address" not in d and "address_confirmed" not in d
    assert "callback_number" in d


def test_caller_address_correction_is_not_cleared_by_later_name_change(flow):
    flow.save({"caller_name": "Maya Chen"})
    flow.save({"service_address": "125 West 72nd Street"})
    d = flow.save({"caller_name": "Maya Chen Jr"})["state"]["details"]
    assert d["service_address"] == "125 West 72nd Street"
    assert d["address_confirmed"] is False


def test_prefill_does_not_mutate_permanent_account_or_restore_removed_phone(flow):
    before = flow.db.execute(
        "select * from public.customer_phone_numbers order by phone_number,customer_id"
    ).fetchall()
    flow.save({"caller_name": "Maya Chen"})
    flow.save({"callback_number": "+12125550177"})
    flow.save({"callback_confirmed": True}, callback="+12125550177")
    assert (
        flow.db.execute(
            "select * from public.customer_phone_numbers order by phone_number,customer_id"
        ).fetchall()
        == before
    )


def test_hot_region_flags_outage_before_name_or_address(flow):
    flow.weather(hot=True)
    r = flow.save(
        {
            "service_type": "repair",
            "cooling_unavailable": True,
            "issue_summary": "AC stopped",
        }
    )
    assert r["state"]["triage"]["level"] == "priority"
    assert r["state"]["triage"]["reasons"] == ["no_cooling_hot_weather"]
    assert r["priority_request"]["status"] == "recorded"
    assert "vulnerable_resident" not in r["state"]["details"]
    # Weather already made it priority, so the vulnerability question is a non-blocking hint.
    assert r["state"]["triage"]["priority_tier"] == "elevated"
    assert r["state"]["triage"]["ask_next"] == ["vulnerability_check"]
    assert (
        flow.db.execute(
            "select weather from public.call_weather_evidence where call_id=%s limit 1",
            (flow.call,),
        ).fetchone()[0]["scope"]
        == "region"
    )


def test_cold_county_only_prioritizes_heat_outage(flow):
    flow.weather()
    flow.weather(cold=True, county="New York")
    r = flow.save(
        {"service_type": "repair", "heating_unavailable": True, "service_zip": "10023"}
    )
    assert r["state"]["triage"]["reasons"] == ["no_heat_cold_weather"]
    r = flow.save(
        {
            "heating_unavailable": False,
            "cooling_unavailable": True,
            "vulnerable_resident": False,
        }
    )
    assert r["state"]["triage"]["level"] == "routine"
    assert r["priority_request"]["needs_review"] is True


def test_mild_weather_outage_asks_one_vulnerability_question_before_booking(flow):
    flow.weather()
    flow.number = "+12125550199"
    r = flow.save({"service_type": "repair", "cooling_unavailable": True})
    assert r["state"]["triage"]["level"] == "needs_assessment"
    assert r["state"]["triage"]["missing_risk_fields"] == ["vulnerability_check"]
    assert (
        flow.save({"vulnerable_resident": "unknown"})["state"]["triage"]["level"]
        == "routine"
    )
    assert flow.ready()["readiness"]["can_book"] is True


@pytest.mark.parametrize("outage", ["heating_unavailable", "cooling_unavailable"])
def test_vulnerable_resident_is_urgent_priority_in_any_weather(flow, outage):
    flow.weather()
    r = flow.save({"service_type": "repair", outage: True, "vulnerable_resident": True})
    assert r["state"]["triage"]["level"] == "priority"
    assert r["state"]["triage"]["priority_tier"] == "urgent"
    assert r["priority_request"]["priority_tier"] == "urgent"


def test_tier_only_escalates(flow):
    flow.weather(cold=True)
    r = flow.save({"service_type": "repair", "heating_unavailable": True})
    assert r["priority_request"]["priority_tier"] == "elevated"
    r = flow.save({"vulnerable_resident": True})
    assert r["priority_request"]["priority_tier"] == "urgent"
    r = flow.save({"vulnerable_resident": False})
    assert r["state"]["triage"]["priority_tier"] == "elevated"
    assert r["priority_request"]["priority_tier"] == "urgent"


@pytest.mark.parametrize("expired", [False, True])
def test_missing_stale_weather_is_unknown_and_one_neutral_answer_unblocks(
    flow, expired
):
    if expired:
        flow.weather(expired=True)
    r = flow.save({"cooling_unavailable": True, "service_type": "repair"})
    assert r["state"]["triage"]["missing_risk_fields"] == [
        "vulnerability_check",
        "temperature_concern",
    ]
    r = flow.save({"vulnerable_resident": False})
    assert r["state"]["triage"]["missing_risk_fields"] == ["temperature_concern"]
    assert flow.save({"unsafe_to_wait": False})["state"]["triage"]["level"] == "routine"


def test_volunteered_inability_to_wait_overrides_mild_weather(flow):
    flow.weather()
    r = flow.save(
        {"service_type": "repair", "cooling_unavailable": True, "unsafe_to_wait": True}
    )
    assert r["state"]["triage"]["reasons"] == ["cannot_safely_wait"]


def test_maintenance_does_not_become_priority_due_to_weather_alone(flow):
    flow.weather(hot=True)
    assert (
        flow.save({"service_type": "maintenance"})["state"]["triage"]["level"]
        == "routine"
    )


def test_gas_stays_emergency_and_keeps_contact_unconfirmed(flow):
    flow.weather(hot=True)
    r = flow.save({"caller_name": "Maya Chen", "gas_smell_reported": True})
    assert r["state"]["triage"]["level"] == "emergency"
    details = r["state"]["details"]
    # O7: the dispatcher can reach and locate the caller; nothing is confirmed.
    assert details["callback_number"] and details["callback_confirmed"] is False
    assert details["service_zip"] and details["address_confirmed"] is False


def test_address_change_rechecks_weather_and_membership(flow):
    flow.weather()
    flow.weather(hot=True, county="New York")
    flow.save(
        {
            "caller_name": "Maya Chen",
            "cooling_unavailable": True,
            "service_type": "repair",
            "vulnerable_resident": False,
        }
    )
    r = flow.save({"service_zip": "11201", "service_city": "Brooklyn"})
    assert r["state"]["triage"]["level"] == "routine"
    assert r["state"]["details"]["address_confirmed"] is False
    assert r["membership"]["membership_status"] == "unknown"


def test_weather_transition_blocks_old_search_and_refresh_returns_new_priority(flow):
    flow.weather()
    flow.ready()
    old = flow.rev
    flow.weather(hot=True)
    r = flow.rpc("search_slots", flow.call, old)
    assert r["status"] == "revision_conflict"
    r = flow.rpc("refresh_call_context", flow.call)
    assert r["state"]["revision"] == old + 1
    assert r["state"]["triage"]["level"] == "priority"
    assert r["priority_request"]["status"] == "recorded"


def test_out_of_area_does_not_use_nyc_weather(flow):
    flow.weather(hot=True)
    r = flow.save(
        {
            "service_state": "TX",
            "service_zip": "75028",
            "cooling_unavailable": True,
            "service_type": "repair",
            "vulnerable_resident": False,
        }
    )
    assert r["state"]["triage"]["level"] == "routine"
    assert r["readiness"]["coverage"] == "out_of_area"


def test_new_commercial_answer_survives_name_correction(flow):
    flow.save({"caller_name": "Maya Chen"})
    d = flow.save({"caller_name": "Different Person", "property_type": "commercial"})[
        "state"
    ]["details"]
    assert d["property_type"] == "commercial" and "service_address" not in d


def test_changed_address_does_not_inherit_unconfirmed_property_classification(flow):
    flow.save({"caller_name": "Maya Chen"})
    d = flow.save({"service_address": "99 Different Street"})["state"]["details"]
    assert d.get("property_type", "unknown") == "unknown"


def test_weather_change_between_proposal_and_agreement_cannot_book(flow):
    flow.weather()
    flow.ready()
    search = flow.rpc("search_slots", flow.call, flow.rev)
    assert search["status"] == "available"
    proposal = flow.rpc(
        "propose_booking",
        flow.call,
        flow.rev,
        search["search_id"],
        search["slots"][0]["slot_id"],
    )
    assert proposal["status"] == "offered"
    flow.weather(hot=True)
    result = flow.rpc(
        "confirm_booking",
        flow.call,
        flow.rev,
        proposal["proposal_id"],
        "accepted",
        "Yes, reserve it",
    )
    assert result["status"] == "revision_conflict"
    assert flow.rpc("get_booking_status", flow.call)["status"] == "not_booked"
    assert (
        flow.rpc("refresh_call_context", flow.call)["priority_request"]["status"]
        == "recorded"
    )


def test_contact_prefill_and_weather_metadata_are_not_model_writable(flow):
    from pydantic import ValidationError
    from revin_take_home.intake import UpdateIntake

    for field in ("_prefill_location", "_prefill_name", "weather", "hot", "cold"):
        with pytest.raises(ValidationError):
            UpdateIntake.model_validate(
                {
                    "expected_revision": 0,
                    "caller_quote": "Hi",
                    "changes": {field: "forged"},
                }
            )
    for role in ("anon", "authenticated"):
        assert not flow.db.execute(
            "select has_table_privilege(%s,'public.service_weather','UPDATE')", (role,)
        ).fetchone()[0]
        assert not flow.db.execute(
            "select has_function_privilege(%s,'public.prefill_call_contact(text)','EXECUTE')",
            (role,),
        ).fetchone()[0]


def test_http_tool_sequence_returns_active_membership_and_books_from_saved_contact(
    flow,
):
    import json
    import httpx
    from fastapi.testclient import TestClient
    from revin_take_home.app import create_app, Settings
    from revin_take_home.intake import SupabaseIntakeStore
    from revin_take_home.workflow import WorkflowStore
    from revin_take_home.demo import DemoDirectory

    def database(request):
        """Real SQL behind a local HTTP transport; no Vapi/model calls or hosted mutations."""
        name = request.url.path.rsplit("/", 1)[-1]
        args = json.loads(request.content)
        from psycopg import sql
        from psycopg.types.json import Jsonb

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
    secret = "isolated-fixture-only-not-a-real-secret"
    app = create_app(
        Settings(
            assistant_id="c1d4d1d8-b2e4-48b9-9ffe-ca4bbea2dd43", webhook_secret=secret
        ),
        directory=DemoDirectory(),
        intake_store=SupabaseIntakeStore(db),
        workflow_store=WorkflowStore(db),
    )
    with TestClient(app) as api:

        def tool(name, args):
            r = api.post(
                "/vapi/tools",
                headers={"Authorization": "Bearer " + secret},
                json={
                    "message": {
                        "type": "tool-calls",
                        "call": {
                            "type": "inboundPhoneCall",
                            "id": flow.call,
                            "customer": {"number": flow.number},
                        },
                        "toolCallList": [
                            {
                                "id": uuid4().hex,
                                "function": {"name": name, "arguments": args},
                            }
                        ],
                    }
                },
            )
            assert r.status_code == 200
            return json.loads(r.json()["results"][0]["result"])

        r = tool(
            "update_intake",
            {
                "expected_revision": 0,
                "caller_quote": "Maya Chen, annual AC maintenance",
                "changes": {
                    "caller_name": "Maya Chen",
                    "service_type": "maintenance",
                    "service_category": "hvac",
                    "issue_summary": "Annual AC maintenance",
                },
            },
        )
        assert r["state"]["details"]["service_address"] == "152 West 72nd Street"
        r = tool(
            "update_intake",
            {
                "expected_revision": r["state"]["revision"],
                "caller_quote": "Yes, that is the address",
                "changes": {"address_confirmed": True},
                "confirmation_address": "152 West 72nd Street",
            },
        )
        assert r["membership"]["membership_status"] == "active"
        r = tool(
            "update_intake",
            {
                "expected_revision": r["state"]["revision"],
                "caller_quote": "Yes, this number is best",
                "changes": {"callback_confirmed": True},
                "confirmation_callback": flow.number,
            },
        )
        revision = r["state"]["revision"]
        slots = tool("search_slots", {"expected_revision": revision})
        assert slots["status"] == "available"
        proposal = tool(
            "propose_booking",
            {
                "expected_revision": revision,
                "search_id": slots["search_id"],
                "slot_id": slots["slots"][0]["slot_id"],
            },
        )
        assert proposal["status"] == "offered"
        booked = tool(
            "confirm_booking",
            {
                "expected_revision": revision,
                "proposal_id": proposal["proposal_id"],
                "decision": "accepted",
                "caller_quote": "Yes, reserve that window",
            },
        )
        assert (
            booked["status"] == "booked"
            and booked["membership"]["membership_status"] == "active"
        )
        assert booked["dispatch_status"] == "assigned"


@pytest.mark.parametrize(
    "hazard",
    [
        "carbon_monoxide_alarm",
        "smoke_or_burning_smell",
        "electrical_sparking",
        "gas_smell",
    ],
)
def test_every_safety_hazard_is_an_emergency_that_cannot_book(flow, hazard):
    flow.weather()
    flow.ready()
    r = flow.rpc(
        "report_safety_emergency",
        flow.call,
        "hazard-op",
        hazard,
        "My detector is going off",
    )
    assert r["state"]["triage"]["level"] == "emergency"
    assert r["priority_request"]["priority_tier"] == "emergency"
    replay = flow.rpc(
        "report_safety_emergency",
        flow.call,
        "hazard-op",
        hazard,
        "My detector is going off",
    )
    assert replay["replayed"] is True
    assert (
        flow.rpc("search_slots", flow.call, r["state"]["revision"])["status"]
        == "not_eligible"
    )


def test_unknown_hazard_is_rejected(flow):
    import psycopg

    with pytest.raises(psycopg.Error):
        flow.rpc(
            "report_safety_emergency",
            flow.call,
            "hazard-op",
            "spilled_coffee",
            "Something spilled",
        )
    flow.db.rollback()


def alerts(flow):
    return flow.db.execute(
        "select kind,title,body,status from public.dispatcher_alerts where call_id=%s order by created_at",
        (flow.call,),
    ).fetchall()


def test_urgent_priority_queues_one_dispatcher_alert_without_personal_details(flow):
    flow.weather()
    flow.save(
        {
            "caller_name": "Maya Chen",
            "service_type": "repair",
            "heating_unavailable": True,
            "vulnerable_resident": True,
            "service_address": "152 West 72nd Street",
            "service_zip": "10023",
        }
    )
    r = flow.save({"issue_summary": "Furnace off, elderly mother at home"})
    rows = alerts(flow)
    assert [(k, s) for k, _, _, s in rows] == [("urgent", "queued")]
    title, body = rows[0][1], rows[0][2]
    assert "no heat, vulnerable person at home" in title and "New York County" in body
    for private in ("Maya", "Chen", "152", "72nd", flow.number, flow.number[-4:]):
        assert private not in title + body
    assert r["priority_request"]["dispatcher_alert"] == "queued"


def test_escalation_alerts_again_but_correction_does_not(flow):
    flow.weather(cold=True)
    flow.save({"service_type": "repair", "heating_unavailable": True})
    flow.save({"vulnerable_resident": True})
    flow.save({"vulnerable_resident": False})
    assert [k for k, *_ in alerts(flow)] == ["elevated", "urgent"]


def test_hazard_alert_is_an_emergency(flow):
    flow.rpc(
        "report_safety_emergency",
        flow.call,
        "hazard-op",
        "carbon_monoxide_alarm",
        "The CO alarm is beeping",
    )
    kind, title, body, _ = alerts(flow)[0]
    assert kind == "emergency" and "carbon monoxide" in title and "evacuate" in body


def test_callback_request_alerts_dispatcher(flow):
    flow.weather()
    flow.ready()
    r = flow.rpc(
        "request_followup",
        flow.call,
        flow.rev,
        "caller_requested",
        "Can I talk to a real person?",
    )
    assert r["status"] == "recorded" and r["dispatcher_alert"] == "queued"
    assert [k for k, *_ in alerts(flow)] == ["callback_requested"]


def test_alert_lease_retries_then_fails_and_never_double_claims(flow):
    flow.rpc(
        "report_safety_emergency", flow.call, "hazard-op", "gas_smell", "I smell gas"
    )
    flow.db.execute(
        "update public.dispatcher_alerts set status='sent' where call_id<>%s and status in ('queued','sending')",
        (flow.call,),
    )
    for attempt in range(3):
        job = flow.rpc("claim_dispatcher_alert")
        assert job["call_id"] == flow.call and job["attempts"] == attempt + 1
        assert flow.rpc("claim_dispatcher_alert") is None
        assert (
            flow.rpc(
                "finish_dispatcher_alert",
                job["id"],
                job["lease_token"],
                "queued",
                "timeout",
            )
            is True
        )
        assert (
            flow.rpc(
                "finish_dispatcher_alert", job["id"], job["lease_token"], "sent", None
            )
            is False
        )
    assert alerts(flow)[0][3] == "failed"
    assert flow.rpc("claim_dispatcher_alert") is None


@pytest.mark.parametrize('readback,ok', [
    ('99 Example Street', True),
    ('99 Example Street, New York, NY 1 0 0 2 3', True),
    ('99 Example St, New York, NY 10023', True),
    ('990 Example Street, New York, NY 10023', False),
    ('99 Example Street, New York, NY 10024', False),
])
def test_address_confirmation_accepts_the_spoken_readback(flow, readback, ok):
    flow.weather()
    flow.save({'caller_name':'Test Caller','property_type':'residential','service_category':'hvac',
               'service_type':'maintenance','service_address':'99 Example Street','service_unit':'none',
               'service_city':'New York','service_state':'NY','service_zip':'10023'})
    r=flow.save({'address_confirmed':True}, readback)
    assert (r['status']=='saved' and r['state']['details']['address_confirmed'] is True) is ok or \
        (not ok and r['status']=='confirmation_mismatch')
    if not ok:
        assert r['status']=='confirmation_mismatch'


def test_apartment_must_be_in_the_readback(flow):
    flow.weather()
    flow.save({'service_address':'455 Ocean Parkway','service_unit':'2D','service_city':'Brooklyn',
               'service_state':'NY','service_zip':'11218'})
    assert flow.save({'address_confirmed':True},'455 Ocean Parkway, Brooklyn, NY 11218')['status']=='confirmation_mismatch'
    r=flow.save({'address_confirmed':True},'455 Ocean Parkway, apartment 2D, Brooklyn, NY 11218')
    assert r['status']=='saved' and r['state']['details']['address_confirmed'] is True


@pytest.mark.parametrize('readback,ok', [('(212) 555-0199', True), ('212 555 0199', True), ('+1 212-555-0199', True), ('212 555 0198', False)])
def test_callback_confirmation_accepts_formatted_numbers(flow, readback, ok):
    flow.number = '+12125550199'
    flow.save({'issue_summary':'AC out'})
    r = flow.save({'callback_confirmed':True}, None, readback)
    assert r['status'] == ('saved' if ok else 'confirmation_mismatch')
