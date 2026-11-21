import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from revin_take_home.app import Settings, create_app
from revin_take_home.membership import MembershipUnavailable, ResolveMembership, SupabaseMembershipStore
from revin_take_home.intake import UpdateIntake

SECRET = 'membership-test-secret-0123456789abcdef'


def envelope(args):
    return {'message': {'type': 'tool-calls', 'call': {'id': 'provider_call', 'type': 'inboundPhoneCall'},
        'toolCallList': [{'id': 'provider_operation', 'function': {'name': 'resolve_membership', 'arguments': args}}]}}


class Store:
    def __init__(self):
        self.received = []

    async def resolve(self, *args):
        self.received.append(args)
        return {'status': 'resolved', 'membership_status': 'active'}


def client(store=None):
    return TestClient(create_app(Settings(assistant_id='c1d4d1d8-b2e4-48b9-9ffe-ca4bbea2dd43',
                                         webhook_secret=SECRET), membership_store=store))


@pytest.mark.parametrize('extra', [{'customer_id': 'demo_maya'}, {'call_id': 'other'},
                                   {'phone_number': '+12125550101'}, {'service_address': 'other'},
                                   {'expected_revision': True}, {'expected_revision': -1}])
def test_resolution_cannot_override_saved_scope(extra):
    with pytest.raises(ValidationError):
        ResolveMembership.model_validate({'expected_revision': 2, **extra})


@pytest.mark.parametrize('as_string', [False, True])
def test_resolution_binds_to_authenticated_provider_call(as_string):
    store = Store()
    args = {'expected_revision': 2}
    response = client(store).post('/vapi/tools', json=envelope(json.dumps(args) if as_string else args),
                                  headers={'Authorization': 'Bearer '+SECRET})
    assert response.status_code == 200
    assert store.received[0][0] == 'provider_call'
    assert store.received[0][1].expected_revision == 2


def test_no_auth_or_fabricated_account_never_calls_store():
    store = Store()
    app = client(store)
    assert app.post('/vapi/tools', json=envelope({'expected_revision': 0})).status_code == 401
    r = app.post('/vapi/tools', json=envelope({'expected_revision': 0, 'customer_id': 'someone'}),
                 headers={'Authorization': 'Bearer '+SECRET})
    assert json.loads(r.json()['results'][0]['result'])['status'] == 'invalid_arguments'
    assert not store.received


def test_unconfigured_lookup_stays_unknown():
    r = client().post('/vapi/tools', json=envelope({'expected_revision': 0}),
                      headers={'Authorization': 'Bearer '+SECRET})
    result = json.loads(r.json()['results'][0]['result'])
    assert result['status'] == 'lookup_unavailable'
    assert result['membership_status'] == 'unknown'


@pytest.mark.parametrize('status,payload', [(500, {}), (200, []), (200, {'status': 'resolved'}),
                                          (200, {'status': 'resolved', 'membership_status': 'gold'})])
def test_failed_or_malformed_database_result_is_not_nonmembership(status, payload):
    async def run():
        def handler(req):
            assert json.loads(req.content) == {'p_call_id': 'provider_call', 'p_expected_revision': 2}
            return httpx.Response(status, json=payload)
        async with httpx.AsyncClient(base_url='https://test.supabase.co/rest/v1/',
                                     transport=httpx.MockTransport(handler)) as http:
            with pytest.raises(MembershipUnavailable):
                await SupabaseMembershipStore(http).resolve('provider_call', ResolveMembership(expected_revision=2))
    asyncio.run(run())


@pytest.mark.parametrize('patch', [{'service_zip': '1002'}, {'service_zip': 10023},
                                   {'service_unit': ''}, {'service_unit': '4B', 'address_confirmed': True},
                                   {'service_zip': '10023', 'address_confirmed': True},
                                   {'membership_claim': 'yes'}])
def test_location_and_claim_validation(patch):
    with pytest.raises(ValidationError):
        UpdateIntake.model_validate({'expected_revision': 1, 'caller_quote': 'Yes',
                                    'changes': patch, 'confirmation_address': '152 West 72nd Street'})
