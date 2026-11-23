import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from revin_take_home.app import Settings, create_app
from revin_take_home.workflow import WorkflowStore, RevisionRequest
from test_intake import SECRET, envelope


def test_background_queue_returns_pending_and_wakes_worker():
    async def run():
        seen=[]
        def handler(request):
            seen.append((request.url.path,json.loads(request.content)))
            return httpx.Response(200,json={'status':'pending','request_id':'job'})
        async with httpx.AsyncClient(base_url='https://test/rest/v1/',transport=httpx.MockTransport(handler)) as client:
            store=WorkflowStore(client)
            result=await store.execute('start_membership_check','provider_call','op',RevisionRequest(expected_revision=2))
            assert result['status']=='pending' and store.wake.is_set()
            assert seen==[('/rest/v1/rpc/start_membership_check',{'p_call_id':'provider_call','p_expected_revision':2})]
    asyncio.run(run())


@pytest.mark.parametrize('name,args', [('refresh_context',{'call_id':'other'}),('search_slots',{'expected_revision':True})])
def test_workflow_tools_reject_scope_override(name,args):
    app=TestClient(create_app(Settings(assistant_id='c1d4d1d8-b2e4-48b9-9ffe-ca4bbea2dd43',webhook_secret=SECRET)))
    body=envelope(args);body['message']['toolCallList'][0]['function']['name']=name
    r=app.post('/vapi/tools',json=body,headers={'Authorization':'Bearer '+SECRET})
    assert json.loads(r.json()['results'][0]['result'])['status']=='invalid_arguments'


def test_storage_outage_never_fabricates_booking_or_empty_availability():
    def fail(request):raise httpx.ReadTimeout('test outage')
    store=WorkflowStore(httpx.AsyncClient(base_url='https://test/rest/v1/',transport=httpx.MockTransport(fail)))
    app=TestClient(create_app(Settings(assistant_id='c1d4d1d8-b2e4-48b9-9ffe-ca4bbea2dd43',webhook_secret=SECRET),workflow_store=store))
    from uuid import uuid4
    for name,args in [('search_slots',{'expected_revision':2}),('get_booking_status',{}),
        ('confirm_booking',{'expected_revision':2,'proposal_id':str(uuid4()),'decision':'accepted','caller_quote':'Yes'})]:
        body=envelope(args);body['message']['toolCallList'][0]['function']['name']=name
        result=json.loads(app.post('/vapi/tools',json=body,headers={'Authorization':'Bearer '+SECRET}).json()['results'][0]['result'])
        assert result['status']=='outcome_unconfirmed'
        assert 'slots' not in result and 'reference' not in result


def test_call_end_requires_auth_and_inbound_scope():
    app=TestClient(create_app(Settings(assistant_id='c1d4d1d8-b2e4-48b9-9ffe-ca4bbea2dd43',webhook_secret=SECRET)))
    body={'message':{'type':'end-of-call-report','call':{'id':'call','type':'inboundPhoneCall'},'endedReason':'customer-ended-call'}}
    assert app.post('/vapi/events',json=body).status_code==401
    assert app.post('/vapi/events',json=body,headers={'Authorization':'Bearer '+SECRET}).status_code==503
    body['message']['call']['type']='webCall'
    assert app.post('/vapi/events',json=body,headers={'Authorization':'Bearer '+SECRET}).status_code==422
