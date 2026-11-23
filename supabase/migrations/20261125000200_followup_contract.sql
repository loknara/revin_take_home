-- request_followup contract (findings O21, O22, O24, O9).
--  * Never returns needs_intake: the call record is created from caller ID when missing,
--    so "the team will follow up" is never said over an unrecorded request (O21).
--  * caller_requested pages a person, so it needs a name and a confirmed callback
--    (needs_details + missing) and real heating/cooling service facts; a sales or vendor
--    call gets out_of_scope instead of a 3 AM page (O22, O24).
--  * New reason technical_issue: recorded after repeated tool failures (the backend returns
--    stop_retrying), needs nothing else, and queues a callback alert for the dispatcher.

-- Base function: same body as followup_and_audit, with the technical_issue reason added.
create or replace function public.request_followup_before_alerts(p_call_id text,p_expected_revision integer,p_reason text,p_caller_quote text) returns jsonb
language plpgsql security definer set search_path='' as $$
declare s public.call_intakes%rowtype; f public.followup_requests%rowtype;
begin
 if p_reason is null or p_reason not in ('no_availability','booking_problem','priority','membership_verification','incomplete_intake','booking_change','caller_requested','technical_issue')
 or p_caller_quote is null or length(trim(p_caller_quote)) not between 1 and 1000 then raise exception 'Invalid follow-up'; end if;
 select * into s from public.call_intakes where call_id=p_call_id for update;
 if not found then return jsonb_build_object('status','needs_intake'); end if;
 if s.revision<>p_expected_revision then return jsonb_build_object('status','revision_conflict','current_revision',s.revision); end if;
 if s.triage->>'level'='emergency' then return jsonb_build_object('status','emergency_guidance_required','priority_request',public.priority_request_summary(p_call_id)); end if;
 insert into public.followup_requests(call_id,reasons,details,caller_quote) values(p_call_id,jsonb_build_array(p_reason),s.details,p_caller_quote)
 on conflict(call_id) do update set
  reasons=case when public.followup_requests.reasons ? p_reason then public.followup_requests.reasons else public.followup_requests.reasons||to_jsonb(p_reason) end,
  details=excluded.details,caller_quote=excluded.caller_quote,updated_at=clock_timestamp() returning * into f;
 return jsonb_build_object('status','recorded','reference',f.id,'notification_status',f.notification_status,
 'callback_confirmed',coalesce(s.details->>'callback_confirmed','false')='true',
 'message','A demo follow-up record exists. No one has been notified; no callback or arrival is guaranteed.');
end;
$$;

create or replace function public.alert_on_callback_request() returns trigger
language plpgsql security definer set search_path='' as $$
begin
 if new.reasons ?| array['caller_requested','technical_issue'] then
   perform public.queue_dispatcher_alert(new.call_id,'callback_requested');
   perform public.refresh_dispatcher_alerts(new.call_id);
 end if;
 return new;
end;
$$;
revoke all on function public.alert_on_callback_request() from public,anon,authenticated,service_role;

-- Heating/cooling service the caller actually described; commercial or non-HVAC never counts.
create function public.has_hvac_service_facts(d jsonb) returns boolean
language sql immutable set search_path='' as $$
 select coalesce(d->>'property_type','unknown')<>'commercial'
  and coalesce(d->>'service_category','unknown')<>'other'
  and coalesce(d->>'service_type','unknown')<>'other'
  and coalesce(nullif(trim(d->>'issue_summary'),'') is not null
       or d->>'service_type' in ('repair','maintenance')
       or d->>'service_category'='hvac'
       or d->>'heating_unavailable'='true' or d->>'cooling_unavailable'='true', false);
$$;
revoke all on function public.has_hvac_service_facts(jsonb) from public,anon,authenticated,service_role;

alter function public.request_followup(text,integer,text,text) rename to request_followup_before_intake;
create function public.request_followup(p_call_id text,p_expected_revision integer,p_reason text,p_caller_quote text,
  p_originating_number text default null) returns jsonb
language plpgsql security definer set search_path='' as $$
declare s public.call_intakes%rowtype; created boolean := false; missing jsonb := '[]'; r jsonb; revision integer := p_expected_revision;
begin
 if p_reason is null or p_reason not in ('no_availability','booking_problem','priority','membership_verification','incomplete_intake','booking_change','caller_requested','technical_issue')
 or p_caller_quote is null or length(trim(p_caller_quote)) not between 1 and 1000 then raise exception 'Invalid follow-up'; end if;
 select * into s from public.call_intakes where call_id=p_call_id for update;
 if not found then
   insert into public.call_intakes(call_id,originating_number) values(p_call_id,p_originating_number)
   on conflict(call_id) do nothing;
   perform public.prefill_call_contact(p_call_id);
   select * into s from public.call_intakes where call_id=p_call_id for update;
   -- The agent never saw a revision for a record it didn't know existed.
   created := true; revision := s.revision;
 end if;
 if s.revision<>revision then return jsonb_build_object('status','revision_conflict','current_revision',s.revision); end if;
 if p_reason='caller_requested' and s.triage->>'level' is distinct from 'emergency' then
   if not public.has_hvac_service_facts(s.details) then
     return jsonb_build_object('status','out_of_scope','missing',jsonb_build_array('issue_summary'),
       'current_revision',s.revision,'created_call_record',created,
       'message','A person is paged only for customers who need home heating or cooling service.');
   end if;
   if nullif(trim(s.details->>'caller_name'),'') is null then missing := missing||'"caller_name"'; end if;
   if s.details->>'callback_number' is null then missing := missing||'"callback_number"';
   elsif coalesce(s.details->>'callback_confirmed','false')<>'true' then missing := missing||'"callback_confirmed"'; end if;
   if missing<>'[]' then
     return jsonb_build_object('status','needs_details','missing',missing,'current_revision',s.revision,
       'created_call_record',created,
       'expected',case when s.details->>'callback_number' is not null
         then jsonb_build_object('confirmation_callback',s.details->>'callback_number') else '{}'::jsonb end);
   end if;
 end if;
 r := public.request_followup_before_intake(p_call_id,revision,p_reason,p_caller_quote);
 if r->>'status'='recorded' and p_reason='technical_issue' then
   r := r||jsonb_build_object('dispatcher_alert',public.dispatcher_alert_status(p_call_id,'callback_requested'),
     'message','Follow-up recorded and a dispatcher alert was queued. No callback time is guaranteed.');
 end if;
 return r||jsonb_build_object('created_call_record',created);
end;
$$;
revoke all on function public.request_followup_before_intake(text,integer,text,text),
 public.request_followup(text,integer,text,text,text) from public,anon,authenticated,service_role;
grant execute on function public.request_followup(text,integer,text,text,text) to service_role;
