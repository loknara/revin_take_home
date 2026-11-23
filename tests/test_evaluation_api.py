from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from revin_take_home.app import Settings, create_app
from revin_take_home.evaluation_api import EvaluationSession

SECRET = "evaluation-test-secret-0123456789abcdef"


class EvalStore:
    def __init__(self, closed=False, expired=False):
        sid = uuid4()
        self.session = EvaluationSession(
            id=sid,
            call_id="eval_" + str(sid),
            fixture_key="maya",
            originating_number="+12125550101",
            expires_at=datetime.now(timezone.utc)
            + timedelta(minutes=-1 if expired else 10),
            closed_at=datetime.now(timezone.utc) if closed else None,
        )

    async def get(self, sid):
        return self.session if sid == self.session.id else None

    async def close(self, sid):
        self.session.closed_at = datetime.now(timezone.utc)

    async def evidence(self, session):
        return {"call_intakes": []}


class Intake:
    def __init__(self):
        self.received = []

    async def update(self, *args):
        self.received.append(args)
        return {"status": "saved"}


def app(store, intake):
    return TestClient(
        create_app(
            Settings(
                assistant_id="c1d4d1d8-b2e4-48b9-9ffe-ca4bbea2dd43",
                webhook_secret=SECRET,
                enable_evaluation_api=True,
            ),
            evaluation_store=store,
            intake_store=intake,
        )
    )


def tool_body():
    return {
        "message": {
            "type": "tool-calls",
            "call": {
                "id": "pretend_real_call",
                "type": "webCall",
                "customer": {"number": "+12125550199"},
            },
            "toolCallList": [
                {
                    "id": "op",
                    "function": {
                        "name": "update_intake",
                        "arguments": {
                            "expected_revision": 0,
                            "caller_quote": "Test",
                            "changes": {"issue_summary": "Maintenance"},
                        },
                    },
                }
            ],
        }
    }


def test_simulated_metadata_cannot_choose_phone_or_real_call_scope():
    store = EvalStore()
    intake = Intake()
    client = app(store, intake)
    url = f"/evaluation/sessions/{store.session.id}/tools"
    assert client.post(url, json=tool_body()).status_code == 401
    r = client.post(
        url, json=tool_body(), headers={"Authorization": "Bearer " + SECRET}
    )
    assert r.status_code == 200
    assert intake.received[0][:3] == (store.session.call_id, "op", "+12125550101")
    assert (
        client.post(
            "/vapi/tools",
            json=tool_body(),
            headers={"Authorization": "Bearer " + SECRET},
        ).status_code
        == 422
    )


@pytest.mark.parametrize("closed,expired", [(True, False), (False, True)])
def test_closed_or_expired_sessions_cannot_write(closed, expired):
    store = EvalStore(closed, expired)
    intake = Intake()
    r = app(store, intake).post(
        f"/evaluation/sessions/{store.session.id}/tools",
        json=tool_body(),
        headers={"Authorization": "Bearer " + SECRET},
    )
    assert r.status_code == 410 and not intake.received


def test_model_cannot_create_arbitrary_identity_fixture():
    client = app(EvalStore(), Intake())
    r = client.post(
        "/evaluation/sessions",
        json={"fixture_key": "maya", "phone_number": "+12125550199"},
        headers={"Authorization": "Bearer " + SECRET},
    )
    assert r.status_code == 422


def test_evaluation_api_is_not_served_unless_enabled():
    app = TestClient(
        create_app(
            Settings(
                assistant_id="c1d4d1d8-b2e4-48b9-9ffe-ca4bbea2dd43",
                webhook_secret=SECRET,
            )
        )
    )
    r = app.post(
        "/evaluation/sessions",
        json={"fixture_key": "active_member"},
        headers={"Authorization": "Bearer " + SECRET},
    )
    assert r.status_code == 404
