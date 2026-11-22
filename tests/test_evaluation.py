import copy
import json
from uuid import uuid4

import httpx
import pytest
from revin_take_home.evaluation import build_payload, load_suite, selected, verify_database, main, Runner

SECRET='evaluation-test-secret-0123456789abcdef'


def test_paid_run_requires_explicit_flag_before_any_network(monkeypatch):
    monkeypatch.delenv('VAPI_API_KEY',raising=False)
    with pytest.raises(SystemExit) as e:main(['run'])
    assert e.value.code==2
    assert main(['plan','--case','gas_emergency'])==0


def test_payload_uses_transient_target_and_isolated_real_tools():
    suite=load_suite();case,profile=selected(suite,['active_member'],['hesitant'])[0]
    live={'model':{'provider':'openai','model':'gpt-4.1','toolIds':['live-tool']},
          'voice':{'provider':'vapi','voiceId':'Elliot'},'transcriber':{'provider':'soniox','model':'stt-rt-v5'},
          'hooks':[{'type':'anything'}],'server':{'url':'https://production.invalid'}}
    before=copy.deepcopy(live)
    payload=build_payload(case,profile,live,{'session_id':str(uuid4()),'caller_context':{'lookup_status':'no_match'}},
                          'https://demo.invalid','credential-id','voice',120)
    target=payload['target']['assistant']
    assert payload['iterations']==1 and payload['transport']['provider']=='vapi.websocket'
    assert 'assistantId' not in payload['target'] and 'hooks' not in target and 'server' not in target
    assert 'toolIds' not in target['model']
    assert all('/evaluation/sessions/' in t['server']['url'] for t in target['model']['tools'])
    assert live==before
    assert payload['simulations'][0]['personality']['assistant']['voice']['voiceId']=='Clara'


def test_missing_database_record_cannot_pass_even_if_ai_judge_passes():
    case=load_suite()['cases'][0]
    checks=verify_database(case,{'call_intakes':[]})
    assert not all(c['passed'] for c in checks)
    assert next(c for c in checks if c['name']=='one_saved_intake')['passed'] is False


def test_membership_from_old_address_cannot_pass():
    case={'expected':{'details':{},'membership':'active'}}
    evidence={'call_intakes':[{'details':{'service_address':'New address'}}],
              'call_membership_checks':[{'result':{'membership_status':'active'},'input_snapshot':{'street':'Old address'}}]}
    checks=verify_database(case,evidence)
    assert next(c for c in checks if c['name']=='membership_matches_final_facts')['passed'] is False


def test_late_priority_cannot_pass():
    case={'expected':{'details':{},'priority':'priority','priority_before_address':True}}
    evidence={'call_intakes':[{'details':{}}],'priority_requests':[{'current_level':'priority','notification_status':'not_connected'}],
              'intake_events':[{'operation_id':'a','applied_revision':1,'changes':{'service_address':'Somewhere'}},
                               {'operation_id':'b','applied_revision':2,'changes':{'cooling_unavailable':True}}]}
    assert not next(c for c in verify_database(case,evidence) if c['name']=='risk_saved_before_address')['passed']


def test_billing_failure_stops_without_retrying_start_and_closes_session(monkeypatch,tmp_path):
    monkeypatch.setenv('VAPI_API_KEY','test');monkeypatch.setenv('VAPI_WEBHOOK_SECRET',SECRET)
    calls=[];sid=str(uuid4())
    def handle(req):
        calls.append((req.method,req.url.path))
        if req.url.path=='/evaluation/sessions':return httpx.Response(200,json={'session_id':sid,'call_id':'eval_'+sid,'caller_context':{}})
        if req.url.path=='/eval/simulation/run':return httpx.Response(402,json={'message':'billing disabled'})
        if req.url.path.endswith('/close'):return httpx.Response(200,json={'status':'closed'})
        raise AssertionError('Unexpected request '+str(req.url))
    runner=Runner('https://demo.invalid',transport=httpx.MockTransport(handle))
    case,profile=selected(load_suite(),['active_member'],['calm'])[0]
    live={'model':{'provider':'openai','model':'gpt-4.1'},'transcriber':{'provider':'soniox'}}
    try:
        with pytest.raises(RuntimeError,match='402'):runner.run_one(case,profile,live,'credential','chat',60,tmp_path)
    finally:runner.close()
    assert calls.count(('POST','/eval/simulation/run'))==1
    assert calls[-1]==('POST',f'/evaluation/sessions/{sid}/close')


def test_runner_requires_judge_and_database_and_collects_evidence(monkeypatch,tmp_path):
    monkeypatch.setenv('VAPI_API_KEY','test');monkeypatch.setenv('VAPI_WEBHOOK_SECRET',SECRET)
    sid=str(uuid4());run_id=str(uuid4());item_id=str(uuid4());requests=[]
    evidence={'call_intakes':[{'revision':1,'details':{'issue_summary':'Maintenance'}}],
              'intake_events':[{'operation_id':'op','applied_revision':1,'changes':{'issue_summary':'Maintenance'}}],
              'priority_requests':[],'call_membership_checks':[]}
    def handle(req):
        requests.append((req.method,req.url.path))
        if req.url.path=='/evaluation/sessions':return httpx.Response(200,json={'session_id':sid,'call_id':'eval_'+sid,'caller_context':{}})
        if req.url.path=='/eval/simulation/run':
            body=json.loads(req.content)
            assert body['iterations']==1 and 'assistantId' not in body['target']
            return httpx.Response(200,json={'id':run_id,'status':'ended'})
        if req.url.path==f'/eval/simulation/run/{run_id}/item':return httpx.Response(200,json=[{'id':item_id}])
        if req.url.path==f'/eval/simulation/run/{run_id}/item/{item_id}':return httpx.Response(200,json={
            'status':'passed','results':{'passed':True,'evaluations':[{'passed':True,'extractedValue':True}]},
            'metadata':{'call':{'transcript':'AI: Saved the demo intake. No appointment was booked.'}}})
        if req.url.path==f'/evaluation/sessions/{sid}':return httpx.Response(200,json={'evidence':evidence})
        if req.url.path.endswith('/close'):return httpx.Response(200,json={'status':'closed'})
        raise AssertionError('Unexpected request '+str(req.url))
    runner=Runner('https://demo.invalid',transport=httpx.MockTransport(handle))
    case={'id':'test','fixture':'maya','instructions':'Test scenario','expected':{'details':{'issue_summary':'Maintenance'}}}
    profile=load_suite()['profiles'][0]
    live={'model':{'provider':'openai','model':'gpt-4.1'},'transcriber':{'provider':'soniox'}}
    try:result=runner.run_one(case,profile,live,'credential','chat',60,tmp_path)
    finally:runner.close()
    assert result['status']=='passed' and all(c['passed'] for c in result['checks'])
    assert requests[-1]==('POST',f'/evaluation/sessions/{sid}/close')
    assert (tmp_path/'last-run.json').exists() and not (tmp_path/'active-run.json').exists()


def test_conversation_checks_catch_unsafe_batch_and_gas_delay():
    from revin_take_home.evaluation import verify_conversation
    case={'expected':{'safety_first':True}}
    messages=[{'role':'user','message':'I smell gas'},
        {'role':'tool_calls','toolCalls':[{'function':{'name':'update_intake'}},{'function':{'name':'start_membership_check'}}]},
        {'role':'bot','message':'Leave and call 911'}]
    assert all(not c['passed'] for c in verify_conversation(case,{'messages':messages}))
    messages[1],messages[2]=messages[2],messages[1]
    assert next(c for c in verify_conversation(case,{'messages':messages}) if c['name']=='gas_guidance_precedes_backend_result')['passed']


def test_gas_action_requires_observed_pre_execution_guidance():
    from revin_take_home.evaluation import verify_conversation
    tool=json.loads(__import__('pathlib').Path('tools/report_gas_emergency.json').read_text())
    assert tool['async'] is False and tool['messages'][0]['blocking'] is True
    case={'expected':{'safety_first':True}}
    messages=[{'role':'user','message':'I smell gas'},
        {'role':'tool_calls','toolCalls':[{'function':{'name':'report_gas_emergency'}}]},
        {'role':'bot','message':tool['messages'][0]['content']},
        {'role':'tool_call_result','result':'{}'}]
    assert all(c['passed'] for c in verify_conversation(case,{'messages':messages}))
    messages[2],messages[3]=messages[3],messages[2]
    assert not all(c['passed'] for c in verify_conversation(case,{'messages':messages}))
