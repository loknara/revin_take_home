-- Fix for 20261125000700 (found in a live chat test, 2026-11-23).
-- change_appointment(reschedule) saved the visit's details, then confirmed them through
-- update_call_intake_before_guard_v2, which still applies the confirmation guard's 3-second
-- rule: the confirmation was refused ("same turn as the address save"), no pending change
-- was recorded, and the new booking left the old visit in place (two visits).
--  * The confirmation step now goes through update_call_intake_before_guard, which has no
--    guard: the caller has just said yes to the visit read back to them.
--  * The pending change is recorded as soon as the details are saved, so any booking made
--    later in this call releases the old visit, even if the confirmation step fails.

create or replace function public.change_appointment(
 p_call_id text, p_operation_id text, p_originating_number text, p_expected_revision integer,
 p_appointment_id uuid, p_action text, p_caller_quote text
) returns jsonb language plpgsql security definer set search_path='' as $$
declare mine jsonb; d jsonb; changes jsonb := '{}'::jsonb; k text; r jsonb; old jsonb;
 copied text[] := array['caller_name','property_type','issue_summary','service_address','service_unit','service_zip',
   'service_city','service_state','service_type','service_category','heating_unavailable','cooling_unavailable',
   'cold_conditions_reported','vulnerable_resident','unsafe_to_wait','membership_claim','callback_number',
   'equipment_notes','access_notes','onsite_contact'];
begin
 if p_action not in ('reschedule','cancel') then
   return jsonb_build_object('status','invalid_arguments','errors',jsonb_build_array(
     jsonb_build_object('field',jsonb_build_array('action'),'message','action must be reschedule or cancel')));
 end if;
 select x into mine from jsonb_array_elements(public.caller_upcoming_appointments(p_call_id,p_originating_number)) x
  where x->>'appointment_id'=p_appointment_id::text;
 if mine is null then return jsonb_build_object('status','not_found'); end if;
 if p_action='cancel' then
   old := public.retire_appointment(p_appointment_id,'cancelled',p_call_id,p_caller_quote);
   return jsonb_build_object('status','cancelled','cancelled',old);
 end if;
 select a.intake_snapshot into d from public.appointments a where a.id=p_appointment_id;
 foreach k in array copied loop
   if d ? k and jsonb_typeof(d->k)<>'null' then changes := changes||jsonb_build_object(k,d->k); end if;
 end loop;
 r := public.update_call_intake_before_guard_v2(p_call_id,p_operation_id||':details',p_originating_number,
        p_expected_revision,changes,p_caller_quote,null,null);
 if r->>'status'<>'saved' then return r; end if;
 insert into public.appointment_changes(call_id,appointment_id,caller_quote) values(p_call_id,p_appointment_id,p_caller_quote)
 on conflict(call_id) do update set appointment_id=excluded.appointment_id,caller_quote=excluded.caller_quote,
   status='pending',completed_at=null;
 r := public.update_call_intake_before_guard(p_call_id,p_operation_id||':confirm',p_originating_number,
        (r->'state'->>'revision')::integer,'{"address_confirmed":true,"callback_confirmed":true}'::jsonb,
        p_caller_quote,d->>'service_address',d->>'callback_number');
 if r->>'status'<>'saved' then return r; end if;
 return r||jsonb_build_object('rescheduling',mine);
end;
$$;
revoke all on function public.change_appointment(text,text,text,integer,uuid,text,text) from public,anon,authenticated;
grant execute on function public.change_appointment(text,text,text,integer,uuid,text,text) to service_role;
