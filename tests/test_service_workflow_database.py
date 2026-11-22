"""Real transaction tests, isolated by random call IDs; opt-in and self-cleaning."""
import os
from pathlib import Path
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(os.environ.get('RUN_DATABASE_TESTS') != '1', reason='Live database checks are opt-in')


@pytest.fixture
def workflow():
    psycopg = pytest.importorskip('psycopg')
    from psycopg.types.json import Jsonb
    url = Path('supabase/.temp/pooler-url').read_text().strip()
    def connect():
        return psycopg.connect(url, password=os.environ['SUPABASE_DB_PASSWORD'], sslmode='require', connect_timeout=15, autocommit=True)
    db = connect()
    calls = []
    class Workflow:
        def new(self):
            call = 'test_workflow_'+uuid4().hex; calls.append(call); return call
        def rpc(self, name, *args):
            values = tuple(Jsonb(x) if isinstance(x, dict) else x for x in args)
            return db.execute('select public.'+name+'('+','.join(['%s']*len(args))+')', values).fetchone()[0]
        def update(self, call, revision, changes, address=None, callback=None):
            return self.rpc('update_call_intake',call,uuid4().hex,'+12125550199',revision,changes,'Synthetic caller quote',address,callback)
        def ready(self, **overrides):
            call = self.new()
            self.update(call,0,dict(caller_name='Test Caller', service_category='hvac',service_type='maintenance',
                property_type='residential', issue_summary='Annual AC maintenance',service_address='152 West 72nd Street',
                service_unit='4B',service_city='New York',service_state='NY',service_zip='10023',callback_number='+12125550188',**overrides))
            return call, self.update(call,1,{'address_confirmed':True,'callback_confirmed':True},'152 West 72nd Street','+12125550188')
    w=Workflow(); w.db=db; w.connect=connect
    try: yield w
    finally:
        db.execute('delete from public.call_intakes where call_id=any(%s)',(calls,));db.close()


def test_callback_is_separate_confirmed_and_corrections_invalidate(workflow):
    call,r=workflow.ready()
    assert r['readiness']['can_book']
    assert r['state']['originating_number']=='+12125550199'
    r=workflow.update(call,2,{'callback_number':'+12125550177'})
    assert not r['state']['details']['callback_confirmed']
    assert not r['readiness']['can_book']
    assert workflow.update(call,3,{'callback_confirmed':True},callback='+12125550188')['status']=='confirmation_mismatch'
    assert workflow.update(call,3,{'callback_confirmed':True},callback='+12125550177')['readiness']['can_book']


@pytest.mark.parametrize('patch,expected',[
    ({'property_type':'commercial'},'out_of_scope'),({'service_category':'other'},'out_of_scope'),
    ({'service_zip':'90210'},'out_of_area'),({'service_state':'CA'},'out_of_area'),
    ({'service_city':'Los Angeles'},'needs_city_clarification'),
])
def test_eligibility_is_enforced_not_model_decided(workflow,patch,expected):
    call,_=workflow.ready();r=workflow.update(call,2,patch)
    assert not r['readiness']['can_book']
    assert expected in (r['readiness']['scope'],r['readiness']['coverage'])


def test_city_change_invalidates_address_confirmation(workflow):
    call,_=workflow.ready();r=workflow.update(call,2,{'service_city':'Manhattan'})
    assert r['state']['details']['address_confirmed'] is False
    assert not r['readiness']['can_book']


def test_optional_equipment_and_access_are_not_booking_blockers(workflow):
    call,r=workflow.ready();assert r['readiness']['can_book']
    r=workflow.update(call,2,{'access_notes':'Doorman','equipment_notes':'Unknown model'})
    assert r['readiness']['can_book']


def test_background_lookup_deduplicates_and_ignores_unrelated_notes(workflow):
    call,_=workflow.ready()
    first=workflow.rpc('start_membership_check',call,2)
    assert first['status']=='pending'
    assert workflow.rpc('start_membership_check',call,2)['request_id']==first['request_id']
    workflow.update(call,2,{'access_notes':'Doorman'})
    job=workflow.rpc('claim_enrichment_job')
    assert str(job['id'])==first['request_id']
    assert workflow.rpc('finish_enrichment_job',job['id'],job['lease_token'])['status']=='ready'
    r=workflow.rpc('refresh_call_context',call)
    assert r['jobs'][0]['status']=='ready'
    assert r['jobs'][0]['result']['membership_status']=='unknown' # synthetic unmatched name
    assert r['state']['revision']==3


def test_late_property_result_is_superseded(workflow):
    call,_=workflow.ready();workflow.rpc('start_membership_check',call,2)
    job=workflow.rpc('claim_enrichment_job')
    workflow.update(call,2,{'service_unit':'5B'})
    assert workflow.rpc('finish_enrichment_job',job['id'],job['lease_token'])['status']=='superseded'
    r=workflow.rpc('refresh_call_context',call)
    assert r['jobs'][0]['result'] is None
    assert r['membership']['membership_status']=='unknown'


def test_gas_during_pending_lookup_supersedes_without_administration(workflow):
    call,_=workflow.ready();workflow.rpc('start_membership_check',call,2)
    job=workflow.rpc('claim_enrichment_job')
    r=workflow.update(call,2,{'gas_smell_reported':True})
    assert r['priority_request']['status']=='recorded'
    assert workflow.rpc('finish_enrichment_job',job['id'],job['lease_token'])['status']=='superseded'
    assert workflow.rpc('start_membership_check',call,3)['status']=='deferred_for_emergency'
    assert workflow.db.execute('select count(*) from public.call_membership_checks where call_id=%s',(call,)).fetchone()[0]==0


def test_worker_restart_reclaims_lease_and_rejects_old_completion(workflow):
    call,_=workflow.ready();workflow.rpc('start_membership_check',call,2)
    old=workflow.rpc('claim_enrichment_job')
    workflow.db.execute("update public.enrichment_jobs set lease_until=clock_timestamp()-interval '1 second' where id=%s",(old['id'],))
    new=workflow.rpc('claim_enrichment_job')
    assert old['lease_token']!=new['lease_token']
    assert workflow.rpc('finish_enrichment_job',old['id'],old['lease_token'])['status']=='lease_lost'
    assert workflow.rpc('finish_enrichment_job',new['id'],new['lease_token'])['status']=='ready'


def test_lookup_deadline_fails_unknown_not_absent(workflow):
    call,_=workflow.ready();workflow.rpc('start_membership_check',call,2)
    workflow.db.execute("update public.enrichment_jobs set deadline=clock_timestamp()-interval '1 second' where call_id=%s",(call,))
    r=workflow.rpc('refresh_call_context',call)
    assert r['jobs'][0]['status']=='timed_out'
    assert r['membership']['membership_status']=='unknown'
