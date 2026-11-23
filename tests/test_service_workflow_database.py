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
    scope = "test_workflow_"+uuid4().hex
    class Workflow:
        def new(self):
            call = scope+'_'+uuid4().hex; calls.append(call); return call
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
    w=Workflow(); w.db=db; w.connect=connect; w.scope=scope; w.call_ids=calls
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


def test_confirmed_intake_no_longer_queues_membership(workflow):
    call, result = workflow.ready()
    assert result['status'] == 'saved'
    assert 'membership_lookup' not in result
    assert workflow.db.execute('select count(*) from public.enrichment_jobs where call_id=%s', (call,)).fetchone()[0] == 0


def test_background_lookup_deduplicates_and_ignores_unrelated_notes(workflow):
    call,_=workflow.ready()
    first=workflow.rpc('start_membership_check',call,2)
    assert first['status']=='pending'
    assert workflow.rpc('start_membership_check',call,2)['request_id']==first['request_id']
    workflow.update(call,2,{'access_notes':'Doorman'})
    job=workflow.rpc('claim_enrichment_job',call)
    assert str(job['id'])==first['request_id']
    assert workflow.rpc('finish_enrichment_job',job['id'],job['lease_token'])['status']=='ready'
    r=workflow.rpc('refresh_call_context',call)
    assert r['jobs'][0]['status']=='ready'
    assert r['jobs'][0]['result']['membership_status']=='unknown' # synthetic unmatched name
    assert r['state']['revision']==3


def test_late_property_result_is_superseded(workflow):
    call,_=workflow.ready();workflow.rpc('start_membership_check',call,2)
    job=workflow.rpc('claim_enrichment_job',call)
    workflow.update(call,2,{'service_unit':'5B'})
    assert workflow.rpc('finish_enrichment_job',job['id'],job['lease_token'])['status']=='superseded'
    r=workflow.rpc('refresh_call_context',call)
    assert r['jobs'][0]['result'] is None
    assert r['membership']['membership_status']=='unknown'


def test_gas_during_pending_lookup_supersedes_without_administration(workflow):
    call,_=workflow.ready();workflow.rpc('start_membership_check',call,2)
    job=workflow.rpc('claim_enrichment_job',call)
    r=workflow.update(call,2,{'gas_smell_reported':True})
    assert r['priority_request']['status']=='recorded'
    assert workflow.rpc('finish_enrichment_job',job['id'],job['lease_token'])['status']=='superseded'
    assert workflow.rpc('start_membership_check',call,3)['status']=='deferred_for_emergency'
    assert workflow.db.execute('select count(*) from public.call_membership_checks where call_id=%s',(call,)).fetchone()[0]==0


def test_worker_restart_reclaims_lease_and_rejects_old_completion(workflow):
    call,_=workflow.ready();workflow.rpc('start_membership_check',call,2)
    old=workflow.rpc('claim_enrichment_job',call)
    workflow.db.execute("update public.enrichment_jobs set lease_until=clock_timestamp()-interval '1 second' where id=%s",(old['id'],))
    new=workflow.rpc('claim_enrichment_job',call)
    assert old['lease_token']!=new['lease_token']
    assert workflow.rpc('finish_enrichment_job',old['id'],old['lease_token'])['status']=='lease_lost'
    assert workflow.rpc('finish_enrichment_job',new['id'],new['lease_token'])['status']=='ready'


def test_lookup_deadline_fails_unknown_not_absent(workflow):
    call,_=workflow.ready();workflow.rpc('start_membership_check',call,2)
    workflow.db.execute("update public.enrichment_jobs set deadline=clock_timestamp()-interval '1 second' where call_id=%s",(call,))
    r=workflow.rpc('refresh_call_context',call)
    assert r['jobs'][0]['status']=='timed_out'
    assert r['membership']['membership_status']=='unknown'


@pytest.fixture
def booking(workflow):
    # Dedicated capacity row; never modify a demo/customer slot.
    slot_id=str(uuid4())
    workflow.db.execute("""insert into public.appointment_slots(id,zone,service_type,starts_at,ends_at,capacity,inventory_scope)
        values(%s,'manhattan','maintenance',
          ((current_timestamp at time zone 'America/New_York')::date+1+interval '8 hours'+%s*interval '1 microsecond') at time zone 'America/New_York',
          ((current_timestamp at time zone 'America/New_York')::date+1+interval '11 hours') at time zone 'America/New_York',1,%s)""",(slot_id,uuid4().int%1000000,workflow.scope))
    class Booking:
        def offer(self, call=None, revision=2):
            if call is None:call,_=workflow.ready()
            found=workflow.rpc('search_slots',call,revision)
            assert found['status']=='available'
            assert any(s['slot_id']==slot_id for s in found['slots'])
            p=workflow.rpc('propose_booking',call,revision,found['search_id'],slot_id)
            assert p['status']=='offered'
            return call,p
        def confirm(self,call,p,revision=2,decision='accepted'):
            return workflow.rpc('confirm_booking',call,revision,p['proposal_id'],decision,'Yes, that window works' if decision=='accepted' else 'No')
    b=Booking();b.slot_id=slot_id;b.w=workflow
    try:yield b
    finally:
        # Remove only this fixture's dependents before deleting its capacity.
        workflow.db.execute('delete from public.call_intakes where call_id=any(%s)',(workflow.call_ids,))
        workflow.db.execute('delete from public.appointment_slots where id=%s',(slot_id,))


def test_booking_requires_offer_then_agreement_and_is_idempotent(booking):
    call,p=booking.offer(); w=booking.w
    assert w.rpc('get_booking_status',call)['status']=='not_booked'
    result=booking.confirm(call,p)
    assert result['status']=='booked' and result['dispatch_status']=='demo_only'
    assert result['timezone']=='America/New_York'
    assert 'membership' in result
    retry=booking.confirm(call,p)
    assert retry['reference']==result['reference']
    assert w.rpc('get_booking_status',call)['reference']==result['reference']
    assert w.db.execute('select count(*) from public.booking_agreements where call_id=%s',(call,)).fetchone()[0]==1


def test_two_callers_last_slot_race_has_one_winner(booking):
    from concurrent.futures import ThreadPoolExecutor
    a,pa=booking.offer();b,pb=booking.offer()
    def confirm(pair):
        call,p=pair
        with booking.w.connect() as db:
            return db.execute('select public.confirm_booking(%s,2,%s,%s,%s)',(call,p['proposal_id'],'accepted','Yes')).fetchone()[0]
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(confirm,[(a,pa),(b,pb)]))
    assert sorted(r['status'] for r in results)==['booked','slot_conflict']
    assert booking.w.db.execute('select count(*) from public.appointments where slot_id=%s',(booking.slot_id,)).fetchone()[0]==1


@pytest.mark.parametrize('patch',[{'service_address':'154 West 72nd Street'},{'callback_number':'+12125550177'},
    {'gas_smell_reported':True},{'preferred_date':'2026-12-31'},{'property_type':'commercial'}])
def test_material_correction_invalidates_offer(booking,patch):
    call,p=booking.offer();booking.w.update(call,2,patch)
    assert booking.confirm(call,p,3)['status']=='stale_proposal'
    assert booking.w.rpc('get_booking_status',call)['status']=='not_booked'


def test_correction_back_to_original_does_not_revive_old_agreement(booking):
    call,p=booking.offer();booking.w.update(call,2,{'issue_summary':'Different job'})
    booking.w.update(call,3,{'issue_summary':'Annual AC maintenance'})
    assert booking.confirm(call,p,4)['status']=='stale_proposal'


def test_access_notes_preserve_offer_and_are_saved_in_appointment(booking):
    call,p=booking.offer();booking.w.update(call,2,{'access_notes':'Use side gate'})
    r=booking.confirm(call,p,3)
    assert r['status']=='booked' and r['details']['access_notes']=='Use side gate'


def test_decline_or_expiration_never_books(booking):
    call,p=booking.offer();assert booking.confirm(call,p,decision='declined')['status']=='declined'
    assert booking.confirm(call,p)['status']=='stale_proposal'
    c,p2=booking.offer();booking.w.db.execute("update public.booking_proposals set expires_at=clock_timestamp()-interval '1 second' where id=%s",(p2['proposal_id'],))
    assert booking.confirm(c,p2)['status']=='stale_proposal'


def test_slot_cannot_be_invented_or_borrowed_from_another_call(booking):
    call,p=booking.offer();other,_=booking.w.ready()
    assert booking.w.rpc('confirm_booking',other,2,p['proposal_id'],'accepted','Yes')['status']=='unknown_proposal'
    search=booking.w.rpc('search_slots',call,2)
    assert booking.w.rpc('propose_booking',other,2,search['search_id'],booking.slot_id)['status']=='stale_availability'
    assert booking.w.rpc('propose_booking',call,2,search['search_id'],str(uuid4()))['status']=='invalid_slot'


def test_hangup_or_post_commit_correction_does_not_cancel_or_duplicate(booking):
    call,p=booking.offer();r=booking.confirm(call,p)
    booking.w.update(call,2,{'service_unit':'5B'})
    status=booking.w.rpc('get_booking_status',call)
    assert status['reference']==r['reference'] and status['change_requires_followup']
    assert booking.confirm(call,p,3)['reference']==r['reference']
    assert status['details']['service_unit']=='4B'


def test_no_availability_is_distinct_from_failure(booking):
    call,_=booking.w.ready();booking.w.update(call,2,{'preferred_date':'2000-01-01'})
    r=booking.w.rpc('search_slots',call,3)
    assert r['status']=='no_availability' and r['slots']==[]


def test_risk_blocks_search_even_if_membership_pending(booking):
    call,_=booking.w.ready();booking.w.update(call,2,{'cooling_unavailable':True,'vulnerable_resident':True})
    assert booking.w.rpc('search_slots',call,3)['status']=='not_eligible'


def test_followup_is_durable_deduplicated_and_not_notified(workflow):
    call,_=workflow.ready()
    a=workflow.rpc('request_followup',call,2,'no_availability','Can you help?')
    b=workflow.rpc('request_followup',call,2,'no_availability','Can you help?')
    assert a['reference']==b['reference'] and a['notification_status']=='not_connected'
    assert workflow.rpc('call_disposition',call)['outcome']=='followup_recorded'
    assert workflow.rpc('request_followup',call,1,'booking_problem','Help')['status']=='revision_conflict'


def test_call_end_does_not_cancel_booking_and_persists_outcome(booking):
    call,p=booking.offer();booking.confirm(call,p)
    try:
        booking.w.rpc('record_call_event',call,{'version':'test'},'customer-ended-call')
        row=booking.w.db.execute('select final_disposition from public.call_audit where call_id=%s',(call,)).fetchone()[0]
        assert row['outcome']=='booked' and row['booking']['status']=='booked'
        assert booking.w.rpc('get_booking_status',call)['status']=='booked'
    finally:booking.w.db.execute('delete from public.call_audit where call_id=%s',(call,))


def test_expired_membership_cache_is_unknown(workflow):
    call,_=workflow.ready();workflow.rpc('resolve_call_membership',call,2)
    workflow.db.execute("update public.call_membership_checks set checked_at=clock_timestamp()-interval '6 minutes' where call_id=%s",(call,))
    result=workflow.rpc('refresh_call_context',call)
    assert result['membership']['membership_status']=='unknown'
    assert result['membership']['status']=='not_checked_for_current_details'


def test_test_capacity_cannot_leak_into_another_call_inventory(booking):
    w=booking.w;original,r=w.ready()
    other='test_workflow_'+uuid4().hex+'_'+uuid4().hex;w.call_ids.append(other)
    details={k:v for k,v in r['state']['details'].items() if k not in ('address_confirmed','callback_confirmed','confirmation_quote','callback_confirmation_quote')}
    w.update(other,0,details)
    w.update(other,1,{'address_confirmed':True,'callback_confirmed':True},'152 West 72nd Street','+12125550188')
    assert w.rpc('search_slots',other,2)['status']=='no_availability'
    assert w.rpc('search_slots',original,2)['slots'][0]['slot_id']==booking.slot_id
    assert w.rpc('call_inventory_scope','eval_'+str(uuid4()))=='evaluation'
    assert w.rpc('call_inventory_scope',str(uuid4()))=='demo'


def test_gas_action_records_immediately_and_replays_idempotently(workflow):
    call=workflow.new();op=uuid4().hex
    first=workflow.rpc('report_gas_emergency',call,op,'I smell gas')
    assert first['state']['triage']['level']=='emergency'
    assert first['priority_request']['status']=='recorded'
    second=workflow.rpc('report_gas_emergency',call,op,'I smell gas')
    assert second['replayed'] and second['state']['revision']==1
    assert workflow.rpc('search_slots',call,1)['status']=='not_eligible'


def test_end_report_before_commit_is_reconciled_by_late_booking(booking):
    call,p=booking.offer()
    try:
        booking.w.rpc('record_call_event',call,{'version':'test'},'customer-ended-call')
        booked=booking.confirm(call,p)
        final=booking.w.db.execute('select final_disposition from public.call_audit where call_id=%s',(call,)).fetchone()[0]
        assert final['outcome']=='booked'
        assert final['booking']['reference']==booked['reference']
    finally:booking.w.db.execute('delete from public.call_audit where call_id=%s',(call,))
