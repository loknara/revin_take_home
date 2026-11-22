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


@pytest.mark.parametrize('name,args', [('refresh_context',{'call_id':'other'}),('start_membership_check',{'expected_revision':True})])
def test_workflow_tools_reject_scope_override(name,args):
    app=TestClient(create_app(Settings(assistant_id='c1d4d1d8-b2e4-48b9-9ffe-ca4bbea2dd43',webhook_secret=SECRET)))
    body=envelope(args);body['message']['toolCallList'][0]['function']['name']=name
    r=app.post('/vapi/tools',json=body,headers={'Authorization':'Bearer '+SECRET})
    assert json.loads(r.json()['results'][0]['result'])['status']=='invalid_arguments'
