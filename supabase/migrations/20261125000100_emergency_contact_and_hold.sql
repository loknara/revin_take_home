-- Emergencies: reachable records and held appointments (findings O7, decision D3).
--  * O7: an emergency record now gets the caller-ID callback and, when caller ID matches
--    exactly one customer, that customer's address on file (unconfirmed, never read back).
--    The dispatcher_alerts ROW stores the callback number so the dispatcher can reach the
--    caller; the pushed alert TEXT still has no name, address or phone number.
--  * D3: an emergency reported after a booking in the same call puts that appointment on
--    hold for dispatcher review; the alert says so and the tool returns appointment_on_hold.
--  * Alert text is computed by one function and refreshed while an alert is still queued,
--    so a page queued mid-save reflects the contact/location prefill from the same save.

alter table public.appointments drop constraint appointments_status_check;
alter table public.appointments add constraint appointments_status_check check (status in ('booked','on_hold'));
alter table public.appointments add column hold_reason text, add column held_at timestamptz;
alter table public.dispatcher_alerts add column callback_number text;

-- Follow-up reasons gain technical_issue (repeated tool failures); see the next migration.
create or replace function public.dispatcher_alert_text(p_call_id text, p_kind text) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare d jsonb; t jsonb; county text; reasons text; held boolean; followup jsonb;
begin
 select details, triage into d, t from public.call_intakes where call_id=p_call_id;
 select a.county into county from public.demo_service_areas a where a.postal_code=d->>'service_zip';
 select string_agg(public.alert_reason_label(value),'; ') into reasons from jsonb_array_elements_text(t->'reasons');
 held := exists(select 1 from public.appointments where call_id=p_call_id and status='on_hold');
 select f.reasons into followup from public.followup_requests f where f.call_id=p_call_id;
 return jsonb_build_object(
  'title', case p_kind
    when 'emergency' then 'EMERGENCY: '||coalesce(reasons,'safety hazard')
    when 'urgent' then 'URGENT priority: '||coalesce(reasons,'priority repair')
    when 'elevated' then 'Priority: '||coalesce(reasons,'priority repair')
    else case when coalesce(followup ? 'caller_requested', true) then 'Callback requested'
              else 'Follow-up needed: technical problem on call' end end,
  'body', concat_ws(' | ',
    case when county is not null then county||' County' else 'Location not collected yet' end,
    case when p_kind='emergency' then 'Caller told to evacuate and call 911; '
           ||case when held then 'appointment booked earlier in this call is ON HOLD for your review'
                  else 'no booking made' end
         when p_kind='callback_requested' then
           case when coalesce(followup ? 'caller_requested', true) then 'Caller asked to speak with a person'
                else 'Tools failed repeatedly; caller was told the team will follow up' end
         else 'Service: '||coalesce(d->>'service_type','unknown') end,
    case when p_kind='emergency' then
      case when d->>'callback_number' is not null then 'Callback number on file' else 'No callback number on file' end end,
    'Call ref '||left(p_call_id,8)));
end;
$$;
revoke all on function public.dispatcher_alert_text(text,text) from public,anon,authenticated,service_role;

create or replace function public.queue_dispatcher_alert(p_call_id text, p_kind text) returns void
language plpgsql security definer set search_path='' as $$
declare m jsonb := public.dispatcher_alert_text(p_call_id,p_kind);
begin
 insert into public.dispatcher_alerts(call_id,kind,title,body,callback_number)
 values(p_call_id,p_kind,m->>'title',m->>'body',
   case when p_kind='emergency' then (select details->>'callback_number' from public.call_intakes where call_id=p_call_id) end)
 on conflict (call_id,kind) do nothing;
end;
$$;
revoke all on function public.queue_dispatcher_alert(text,text) from public,anon,authenticated,service_role;

-- Only alerts nobody has claimed yet are rewritten; a sent page is never edited.
create function public.refresh_dispatcher_alerts(p_call_id text) returns void
language plpgsql security definer set search_path='' as $$
declare a record; m jsonb;
begin
 for a in select id, kind from public.dispatcher_alerts
   where call_id=p_call_id and status='queued' and attempts=0 for update loop
   m := public.dispatcher_alert_text(p_call_id,a.kind);
   update public.dispatcher_alerts set title=m->>'title', body=m->>'body',
     callback_number=case when a.kind='emergency'
       then (select details->>'callback_number' from public.call_intakes where call_id=p_call_id) end
    where id=a.id;
 end loop;
end;
$$;
revoke all on function public.refresh_dispatcher_alerts(text) from public,anon,authenticated,service_role;

-- Same rules as contact_prefill, plus an emergency path: the callback from caller ID, and
-- the address on file when caller ID alone identifies exactly one customer (no name needed:
-- the caller is told to leave, so the dispatcher needs the location now).
create or replace function public.prefill_call_contact(p_call_id text) returns void
language plpgsql security definer set search_path='' as $$
declare s public.call_intakes%rowtype; d jsonb; loc public.service_locations%rowtype;
 customers text[]; locations text[]; patch jsonb := '{}'; src text; source_location text;
 emergency boolean;
begin
 select * into s from public.call_intakes where call_id=p_call_id for update;
 if not found then return; end if;
 emergency := s.triage->>'level'='emergency';
 d:=s.details;
 -- A changed name must not retain an address supplied solely by the old match.
 if not emergency and d ? '_prefill_name' and d->>'_prefill_name' is distinct from public.lookup_text(d->>'caller_name') then
   d:=d-array['service_address','service_unit','service_city','service_state','service_zip',
              'address_confirmed','confirmation_quote','_prefill_name','_prefill_location'];
   if d ? '_prefill_property' then d:=d-array['property_type','_prefill_property']; end if;
   src:='name_correction';
 end if;
 if not (d ? 'callback_number') and s.originating_number ~ '^\+[1-9][0-9]{7,14}$' then
   patch:=patch||jsonb_build_object('callback_number',s.originating_number,'callback_confirmed',false);
   src:=coalesce(src,'provider_caller_id');
 end if;
 -- Never complete or replace a partially caller-supplied address with account data.
 if not (d ?| array['service_address','service_unit','service_city','service_state','service_zip'])
    and (emergency or nullif(trim(d->>'caller_name'),'') is not null)
    and s.originating_number is not null then
   select array_agg(c.id) into customers from public.customers c
   join public.customer_phone_numbers p on p.customer_id=c.id
   where p.phone_number=s.originating_number and (emergency or
    public.lookup_text(c.full_name)=public.lookup_text(d->>'caller_name')
      or public.lookup_text(c.first_name)=public.lookup_text(d->>'caller_name'));
   if cardinality(customers)=1 then
     select array_agg(id order by id) into locations from public.service_locations
       where customer_id=customers[1];
     if cardinality(locations)=1 then
       select * into loc from public.service_locations where id=locations[1];
     else
       select * into loc from public.service_locations where customer_id=customers[1] and is_primary;
     end if;
     if loc.id is not null then
       patch:=patch||jsonb_build_object('service_address',loc.street_address,'service_unit',loc.unit,
         'service_city',loc.city,'service_state',loc.state,'service_zip',loc.postal_code,
         'address_confirmed',false,'_prefill_location',loc.id)
         ||case when emergency then '{"_prefill_emergency":true}'::jsonb
                else jsonb_build_object('_prefill_name',public.lookup_text(d->>'caller_name')) end;
       -- A caller's explicit commercial/residential answer wins over the record.
       if coalesce(d->>'property_type','unknown')='unknown' then patch:=patch||jsonb_build_object('property_type',loc.property_type,'_prefill_property',true); end if;
       src:='matched_service_location';source_location:=loc.id;
     end if;
   end if;
 end if;
 if d||patch is distinct from s.details then
   update public.call_intakes set details=d||patch, updated_at=now()
     where call_id=p_call_id returning * into s;
   insert into public.contact_prefill_events(call_id,intake_revision,source,location_id,fields)
    values(p_call_id,s.revision,src,source_location,patch);
 end if;
end;
$$;
revoke all on function public.prefill_call_contact(text) from public,anon,authenticated,service_role;

-- Booking status reports a held appointment honestly.
create or replace function public.get_booking_status(p_call_id text) returns jsonb
language sql security definer set search_path='' as $$
 select public.booking_status_record(p_call_id)||jsonb_build_object('membership',
   coalesce((select membership_snapshot from public.appointments where call_id=p_call_id),public.current_call_membership(p_call_id)),
   'dispatch',(select jsonb_build_object('job_id',j.id,'status',j.status,'technician_name',t.display_name,
      'technician_id',t.id,'synthetic_roster',t.is_synthetic,'priority',j.priority,'assigned_at',j.assigned_at)
      from public.dispatch_jobs j join public.technicians t on t.id=j.technician_id where j.call_id=p_call_id))
  ||coalesce((select jsonb_build_object('status','on_hold','hold_reason',a.hold_reason,'held_at',a.held_at,
      'message','This appointment is on hold for dispatcher review after a safety emergency. Do not tell the caller it is confirmed.')
    from public.appointments a where a.call_id=p_call_id and a.status='on_hold'),'{}'::jsonb);
$$;

-- The caller-ID number is passed so an emergency that is the call's first tool call still
-- gets a reachable callback (it used to create the record with no number at all).
alter function public.report_safety_emergency(text,text,text,text) rename to report_safety_emergency_before_hold;
create function public.report_safety_emergency(p_call_id text,p_operation_id text,p_hazard text,p_caller_quote text,
  p_originating_number text default null) returns jsonb
language plpgsql security definer set search_path='' as $$
declare r jsonb; held boolean;
begin
 if nullif(trim(p_call_id),'') is null then raise exception 'Invalid call scope'; end if;
 insert into public.call_intakes(call_id,originating_number) values(p_call_id,p_originating_number)
 on conflict(call_id) do update set originating_number=excluded.originating_number
  where public.call_intakes.originating_number is null and excluded.originating_number is not null;
 update public.appointments set status='on_hold',hold_reason='safety_emergency',held_at=clock_timestamp()
  where call_id=p_call_id and status='booked';
 r := public.report_safety_emergency_before_hold(p_call_id,p_operation_id,p_hazard,p_caller_quote);
 perform public.refresh_dispatcher_alerts(p_call_id);
 held := exists(select 1 from public.appointments where call_id=p_call_id and status='on_hold');
 -- The agent must not read an on-file address back during an evacuation; keep it for the dispatcher.
 if r->'state'->'details' ? '_prefill_emergency' then
   r := jsonb_set(r,'{state,details}',(r->'state'->'details')
        -array['service_address','service_unit','service_city','service_state','service_zip']);
 end if;
 return r||jsonb_build_object('appointment_on_hold',held)
   ||case when held then jsonb_build_object('message',
       'The appointment booked earlier in this call is now on hold for dispatcher review. Do not rebook or reassure.')
     else '{}'::jsonb end;
end;
$$;
revoke all on function public.report_safety_emergency_before_hold(text,text,text,text),
 public.report_safety_emergency(text,text,text,text,text) from public,anon,authenticated,service_role;
grant execute on function public.report_safety_emergency(text,text,text,text,text) to service_role;
