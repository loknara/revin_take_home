"""Opt-in checks using isolated synthetic customers and call records, removed afterward."""

import os
from pathlib import Path
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DATABASE_TESTS") != "1",
    reason="Live database checks are opt-in",
)


@pytest.fixture
def directory():
    psycopg = pytest.importorskip("psycopg")
    from psycopg.types.json import Jsonb

    prefix = "test_membership_" + uuid4().hex
    with psycopg.connect(
        (
            os.environ.get("TEST_DATABASE_URL")
            or Path("supabase/.temp/pooler-url").read_text().strip()
        ),
        password=os.environ.get("SUPABASE_DB_PASSWORD", ""),
        sslmode="disable" if os.environ.get("TEST_DATABASE_URL") else "require",
        connect_timeout=15,
        autocommit=True,
    ) as db:
        db.execute("set time zone 'America/New_York'")
        customer_id, location_id, call_id = (
            prefix + "_customer",
            prefix + "_location",
            prefix + "_call",
        )
        ids = [customer_id]
        db.execute(
            "insert into public.customers(id,first_name,full_name) values(%s,%s,%s)",
            (customer_id, "Maya", "Maya Test " + prefix),
        )
        db.execute(
            "insert into public.customer_phone_numbers(phone_number,customer_id) values(%s,%s)",
            ("+12125550188", customer_id),
        )
        db.execute(
            "insert into public.service_locations(id,customer_id,street_address,unit,postal_code,city,state,property_type) values(%s,%s,'152 West 72nd Street','4B','10023','New York','NY','residential')",
            (location_id, customer_id),
        )
        db.execute(
            "insert into public.memberships values(%s,'Demo plan','active',current_date-10,current_date+10)",
            (location_id,),
        )

        class Directory:
            revision = 0
            name = "Maya Test " + prefix

            def update(self, patch, number="+12125550188", confirmation=None):
                result = db.execute(
                    "select public.update_call_intake(%s,%s,%s,%s,%s,%s,%s)",
                    (
                        call_id,
                        uuid4().hex,
                        number,
                        self.revision,
                        Jsonb(patch),
                        "Synthetic test utterance",
                        confirmation,
                    ),
                ).fetchone()[0]
                if result["status"] == "saved":
                    self.revision = result["state"]["revision"]
                return result

            def ready(self, number="+12125550188", name="Maya"):
                self.update(
                    {
                        "caller_name": name,
                        "service_address": "152 W. 72nd St.",
                        "service_unit": "Apt 4B",
                        "service_zip": "10023",
                        "property_type": "residential",
                    },
                    number,
                )
                self.update({"address_confirmed": True}, number, "152 W. 72nd St.")

            def resolve(self, revision=None):
                return db.execute(
                    "select public.resolve_call_membership(%s,%s)",
                    (call_id, self.revision if revision is None else revision),
                ).fetchone()[0]

            def change_plan(self, state):
                if state == "none":
                    db.execute(
                        "delete from public.memberships where location_id=%s",
                        (location_id,),
                    )
                elif state == "expired":
                    db.execute(
                        "update public.memberships set effective_on=current_date-20, expires_on=current_date-1 where location_id=%s",
                        (location_id,),
                    )
                elif state == "not_started":
                    db.execute(
                        "update public.memberships set effective_on=current_date+1 where location_id=%s",
                        (location_id,),
                    )
                elif state == "inactive":
                    db.execute(
                        "update public.memberships set status='inactive' where location_id=%s",
                        (location_id,),
                    )

            def ambiguous(self):
                other = prefix + "_other"
                ids.append(other)
                db.execute(
                    "insert into public.customers(id,first_name,full_name) values(%s,%s,%s)",
                    (other, "Maya", self.name),
                )
                db.execute(
                    "insert into public.service_locations(id,customer_id,street_address,unit,postal_code,city,state,property_type) values(%s,%s,'152 West 72nd Street','4B','10023','New York','NY','residential')",
                    (prefix + "_other_location", other),
                )

            def count_checks(self):
                return db.execute(
                    "select count(*) from public.call_membership_checks where call_id=%s",
                    (call_id,),
                ).fetchone()[0]

        try:
            yield Directory()
        finally:
            db.execute("delete from public.call_intakes where call_id=%s", (call_id,))
            db.execute(
                "delete from public.memberships where location_id in (select id from public.service_locations where customer_id=any(%s))",
                (ids,),
            )
            db.execute(
                "delete from public.service_locations where customer_id=any(%s)", (ids,)
            )
            db.execute(
                "delete from public.customer_phone_numbers where customer_id=any(%s)",
                (ids,),
            )
            db.execute("delete from public.customers where id=any(%s)", (ids,))


@pytest.mark.parametrize(
    "status", ["active", "inactive", "expired", "not_started", "none"]
)
def test_status_is_property_specific_and_date_aware(directory, status):
    directory.ready()
    directory.change_plan(status)
    result = directory.resolve()
    assert result["status"] == "resolved"
    assert result["membership_status"] == status
    assert result["approved_benefits"] == []
    assert "customer_id" not in result and "location_id" not in result


def test_unknown_number_can_resolve_full_name_and_location(directory):
    directory.ready(number="+12125550189", name=directory.name)
    assert directory.resolve()["membership_status"] == "active"


def test_unknown_number_first_name_does_not_resolve(directory):
    directory.ready(number="+12125550189")
    r = directory.resolve()
    assert r["status"] == "needs_full_name" and r["membership_status"] == "unknown"


def test_ambiguous_match_does_not_disclose_candidates(directory):
    directory.ambiguous()
    directory.ready(number="+12125550189", name=directory.name)
    r = directory.resolve()
    assert r["status"] == "ambiguous" and r["membership_status"] == "unknown"
    assert "plan_name" not in r and "candidates" not in r


def test_claim_does_not_override_plan(directory):
    directory.ready()
    directory.change_plan("expired")
    directory.update({"membership_claim": True})
    r = directory.resolve()
    assert r["membership_status"] == "expired" and r["disputed"] is True
    assert r["caller_claim"] == "true" and r["verification_required"] is True


@pytest.mark.parametrize(
    "patch",
    [
        {"service_unit": "5B"},
        {"service_zip": "10024"},
        {"service_address": "240 West 95th Street"},
    ],
)
def test_location_correction_invalidates_confirmation_and_membership(directory, patch):
    directory.ready()
    directory.resolve()
    r = directory.update(patch)
    assert r["state"]["details"]["address_confirmed"] is False
    assert r["membership"]["membership_status"] == "unknown"
    assert directory.resolve()["status"] == "needs_address_confirmation"
    street = r["state"]["details"]["service_address"]
    directory.update({"address_confirmed": True}, confirmation=street)
    directory.update({"caller_name": directory.name})
    assert directory.resolve()["status"] == "no_match"


def test_unrelated_intake_change_retains_membership_context(directory):
    directory.ready()
    directory.resolve()
    r = directory.update({"issue_summary": "AC not cooling"})
    assert r["membership"]["membership_status"] == "active"
    assert directory.resolve(revision=1)["status"] == "revision_conflict"


def test_missing_fields_and_confirmation_do_not_disclose_plan(directory):
    directory.update({"caller_name": "Maya"})
    r = directory.resolve()
    assert (
        r["status"] == "needs_address_confirmation"
        and r["membership_status"] == "unknown"
    )
    assert r["membership_status"] == "unknown"


def test_repeated_resolution_does_not_duplicate_check_or_link_phone(directory):
    directory.ready(number="+12125550189", name=directory.name)
    directory.resolve()
    directory.resolve()
    assert directory.count_checks() == 1
    directory.update({"caller_name": "Maya"})
    assert directory.resolve()["status"] == "needs_full_name"


@pytest.mark.parametrize(
    "patch,status",
    [
        ({"gas_smell_reported": True}, "deferred_for_emergency"),
        ({"property_type": "commercial"}, "out_of_scope"),
    ],
)
def test_hazard_and_commercial_do_not_trigger_administrative_resolution(
    directory, patch, status
):
    directory.ready()
    directory.update(patch)
    r = directory.resolve()
    assert r["status"] == status and r["membership_status"] == "unknown"
