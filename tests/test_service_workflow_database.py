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
