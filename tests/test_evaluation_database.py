"""No-model test of the simulation bridge against the demo database."""
import json
import os
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from revin_take_home.app import create_app

pytestmark=pytest.mark.skipif(os.environ.get('RUN_DATABASE_TESTS')!='1',reason='Live database checks are opt-in')


def test_session_identity_real_tools_evidence_and_close():
    psycopg=pytest.importorskip('psycopg')
    session=None
    with psycopg.connect(Path('supabase/.temp/pooler-url').read_text().strip(),password=os.environ['SUPABASE_DB_PASSWORD'],
                          sslmode='require',connect_timeout=15,autocommit=True) as db:
        try:
            with TestClient(create_app()) as client:
                headers={'Authorization':'Bearer '+os.environ['VAPI_WEBHOOK_SECRET']}
                r=client.post('/evaluation/sessions',headers=headers,json={'fixture_key':'maya'});assert r.status_code==200,r.text
                session=r.json()
                assert session['call_id']=='eval_'+session['session_id']
                assert session['caller_context']['candidate']['first_name']=='Maya'
                path='/evaluation/sessions/'+session['session_id']
                def tool(name,args):
                    body={'message':{'type':'tool-calls','toolCallList':[{'id':uuid4().hex,'function':{'name':name,'arguments':args}}]}}
                    r=client.post(path+'/tools',json=body,headers=headers);assert r.status_code==200,r.text
                    return json.loads(r.json()['results'][0]['result'])
                r=tool('update_intake',{'expected_revision':0,'caller_quote':'Synthetic property details','changes':{
                    'caller_name':'Maya','service_address':'152 West 72nd Street','service_unit':'4B','service_zip':'10023','property_type':'residential','service_type':'maintenance'}})
                assert r['status']=='saved'
                r=tool('update_intake',{'expected_revision':1,'caller_quote':'Yes','changes':{'address_confirmed':True},'confirmation_address':'152 West 72nd Street'})
                assert r['status']=='saved'
                assert tool('resolve_membership',{'expected_revision':2})['membership_status']=='active'
                evidence=client.get(path,headers=headers).json()['evidence']
                assert len(evidence['call_intakes'])==1
                assert evidence['call_membership_checks'][0]['result']['membership_status']=='active'
                assert client.post(path+'/close',headers=headers).status_code==200
                body={'message':{'type':'tool-calls','toolCallList':[{'id':'late','function':{'name':'resolve_membership','arguments':{'expected_revision':2}}}]}}
                assert client.post(path+'/tools',headers=headers,json=body).status_code==410
            for role in ('anon','authenticated'):
                assert not db.execute("select has_table_privilege(%s,'public.evaluation_sessions','SELECT')",(role,)).fetchone()[0]
                assert not db.execute("select has_function_privilege(%s,'public.create_evaluation_session(text)','EXECUTE')",(role,)).fetchone()[0]
        finally:
            if session:
                db.execute('delete from public.call_intakes where call_id=%s',(session['call_id'],))
                db.execute('delete from public.evaluation_sessions where id=%s',(session['session_id'],))
