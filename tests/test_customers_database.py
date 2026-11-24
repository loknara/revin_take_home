"""A caller who books becomes a customer, and is recognized on their next call."""

import os
from urllib.parse import urlparse
from uuid import uuid4

import pytest

from test_dispatch_database import book as _book
from test_weather_contact_database import flow  # noqa: F401 (pytest fixture)

pytestmark = pytest.mark.skipif(
    urlparse(os.environ.get("TEST_DATABASE_URL", "")).hostname
    not in ("127.0.0.1", "localhost", "::1"),
    reason="Requires isolated TEST_DATABASE_URL",
)
NUMBER = "+12125550199"  # flow.ready()'s caller: "Test Caller", 99 Example Street, no unit


def agent_customers(flow):  # noqa: F811
    """Customers on the test number that this test's own calls created (other tests may
    commit bookings on the same number)."""
    return flow.db.execute(
        "select c.id, c.first_name, c.full_name, c.source from public.customers c "
        "join public.customer_phone_numbers p on p.customer_id=c.id "
        "where p.phone_number=%s and c.created_by_call = any(%s)",
        (NUMBER, CALLS),
    ).fetchall()


CALLS = []


def book(flow):  # noqa: F811
    CALLS.append(flow.call)
    return _book(flow)


def real_call(flow):  # noqa: F811
    """Test-prefixed calls are skipped by the trigger, so use a real-looking call id."""
    flow.call, flow.rev = "cust_" + uuid4().hex, 0


def test_a_booked_caller_is_saved_and_recognized_next_time(flow):  # noqa: F811
    real_call(flow)
    assert agent_customers(flow) == []
    book(flow)
    [(customer_id, first, full, source)] = agent_customers(flow)
    assert (first, full, source) == ("Test", "Test Caller", "phone_agent")
    location = flow.db.execute(
        "select street_address, unit, postal_code, is_primary from public.service_locations where customer_id=%s",
        (customer_id,),
    ).fetchall()
    assert location == [("99 Example Street", "none", "10023", True)]
    # Next call from the same number: the name matches, so the saved address is offered.
    real_call(flow)
    r = flow.save({"caller_name": "Test Caller", "service_type": "maintenance", "service_category": "hvac",
                   "issue_summary": "Annual tune-up"})
    details = r["state"]["details"]
    assert details["service_address"] == "99 Example Street" and details["address_confirmed"] is False
    assert r["membership"]["membership_status"] in ("unknown", "none")  # a customer, not a member


def test_booking_again_does_not_duplicate_the_customer(flow):  # noqa: F811
    real_call(flow)
    book(flow)
    real_call(flow)
    book(flow)
    assert len(agent_customers(flow)) == 1


@pytest.mark.parametrize("prefix", ["eval_", "test_flow_"])
def test_test_calls_are_not_saved_as_customers(flow, prefix):  # noqa: F811
    flow.call, flow.rev = prefix + uuid4().hex, 0
    book(flow)
    assert agent_customers(flow) == []
