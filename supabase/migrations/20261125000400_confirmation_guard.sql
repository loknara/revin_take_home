-- update_call_intake guard and address hygiene (findings O1, O33, O10).
--  * O1: a confirmation must be the caller's answer to a readback. address_confirmed /
--    callback_confirmed is refused with needs_caller_yes when the caller_quote is the
--    agent's own readback ("Just to confirm...", or it contains the full address/number),
--    or when it arrives within 3 s of that address/number being saved or prefilled: no
--    person can hear a readback and answer that fast, so it was confirmed in the same turn.
--    Tests may lower the gap with the session setting summit.confirmation_gap_ms; the
--    Data API cannot set it.
--  * O33: when the caller changes the street of a prefilled (account) address, the old
--    unit/ZIP/city/state from that account address are cleared, not silently kept.
--  * O10: a covered NYC ZIP fills a missing city and state (marked _derived_location), so
--    the agent never asks "what state?" for a Brooklyn ZIP. Changing the ZIP re-derives.
--  * Queued (not yet sent) dispatcher alerts are refreshed after each save.

create function public.quote_is_agent_readback(quote text, d jsonb, check_address boolean, check_callback boolean) returns boolean
language sql immutable set search_path='' as $$
 select public.lookup_text(quote) ~ ('^(ok(ay)?[ ,]+)?(so[ ,]+)?(and[ ,]+)?(just[ ,]+)?('
     'to confirm|let me (just )?(confirm|read|double)|confirming|i have (you|your|the)|i''ve got (you|your|the)'
     '|we have (you|your|the)|i see (you|your|the)|your address is|the address is|is (that|this|it) (right|correct)'
     '|can you confirm|could you confirm|does that sound)')
  or (check_address and nullif(d->>'service_zip','') is not null and nullif(trim(d->>'service_address'),'') is not null
      and position(public.street_lookup_key(d->>'service_address') in public.street_lookup_key(quote))>0
      and position(d->>'service_zip' in regexp_replace(quote,'\D','','g'))>0)
  or (check_callback and length(regexp_replace(coalesce(d->>'callback_number',''),'\D','','g'))>=10
      and position(right(regexp_replace(d->>'callback_number','\D','','g'),10) in regexp_replace(quote,'\D','','g'))>0);
$$;
revoke all on function public.quote_is_agent_readback(text,jsonb,boolean,boolean) from public,anon,authenticated,service_role;

-- When did this call last get a new value for these fields (caller save or prefill)?
create function public.last_field_change(p_call_id text, p_fields text[]) returns timestamptz
language sql stable security definer set search_path='' as $$
 select max(t) from (
   select created_at t from public.intake_events where call_id=p_call_id and changes ?| p_fields
   union all
   select created_at from public.contact_prefill_events where call_id=p_call_id and fields ?| p_fields
 ) x;
$$;
revoke all on function public.last_field_change(text,text[]) from public,anon,authenticated,service_role;

create function public.confirmation_guard(p_call_id text, d jsonb, p_changes jsonb, p_caller_quote text) returns text
language plpgsql stable security definer set search_path='' as $$
declare addr boolean := p_changes->>'address_confirmed'='true';
 cb boolean := p_changes->>'callback_confirmed'='true';
 gap interval := make_interval(secs => coalesce(nullif(current_setting('summit.confirmation_gap_ms',true),'')::numeric,3000)/1000);
begin
 if not (addr or cb) then return null; end if;
 if public.quote_is_agent_readback(p_caller_quote,d,addr,cb) then return 'quote_is_readback'; end if;
 if addr and public.last_field_change(p_call_id,array['service_address','service_unit','service_zip','service_city','service_state'])>clock_timestamp()-gap then
   return 'same_turn_as_address_save'; end if;
 if cb and public.last_field_change(p_call_id,array['callback_number'])>clock_timestamp()-gap then
   return 'same_turn_as_callback_save'; end if;
 return null;
end;
$$;
revoke all on function public.confirmation_guard(text,jsonb,jsonb,text) from public,anon,authenticated,service_role;

alter function public.update_call_intake(text,text,text,integer,jsonb,text,text,text) rename to update_call_intake_before_guard;
create function public.update_call_intake(
 p_call_id text,p_operation_id text,p_originating_number text,p_expected_revision integer,
 p_changes jsonb,p_caller_quote text,p_confirmation_address text default null,p_confirmation_callback text default null
) returns jsonb language plpgsql security definer set search_path='' as $$
declare s public.call_intakes%rowtype; before jsonb; r jsonb; reason text; d jsonb; k text;
 a public.demo_service_areas%rowtype; derived jsonb; city text;
begin
 select * into s from public.call_intakes where call_id=p_call_id for update;
 before := s.details;
 -- Only a fresh operation at the current revision is judged; replays and conflicts keep
 -- their existing answers.
 if found and s.revision=p_expected_revision and jsonb_typeof(p_changes)='object'
    and not exists(select 1 from public.intake_events where call_id=p_call_id and operation_id=p_operation_id) then
   reason := public.confirmation_guard(p_call_id,s.details,p_changes,p_caller_quote);
   if reason is not null then
     return jsonb_build_object('status','needs_caller_yes','reason',reason,
       'readiness',public.intake_readiness(s.details),'state',to_jsonb(s),
       'priority_request',public.priority_request_summary(p_call_id),'membership',public.current_call_membership(p_call_id));
   end if;
 end if;
 r := public.update_call_intake_before_guard(p_call_id,p_operation_id,p_originating_number,p_expected_revision,
        p_changes,p_caller_quote,p_confirmation_address,p_confirmation_callback);
 if r->>'status'='saved' and r->>'replayed'='false' then
   select * into s from public.call_intakes where call_id=p_call_id for update;
   d := s.details;
   -- O33: a new street replaces the whole account address unless the caller restated parts.
   if before ? '_prefill_location' and p_changes ? 'service_address'
      and public.street_lookup_key(p_changes->>'service_address') is distinct from public.street_lookup_key(before->>'service_address') then
     foreach k in array array['service_unit','service_zip','service_city','service_state'] loop
       if not p_changes ? k and d->k is not distinct from before->k then d := d-k; end if;
     end loop;
   end if;
   -- O10: re-derive city/state whenever the ZIP they came from changes.
   if d ? '_derived_location' and d->>'service_zip' is distinct from d->'_derived_location'->>'zip' then
     if not p_changes ? 'service_city' and d->>'service_city' is not distinct from d->'_derived_location'->>'city' then d := d-'service_city'; end if;
     if not p_changes ? 'service_state' and d->>'service_state' is not distinct from d->'_derived_location'->>'state' then d := d-'service_state'; end if;
     d := d-'_derived_location';
   end if;
   select * into a from public.demo_service_areas where postal_code=d->>'service_zip' and enabled;
   if found and not (d ? 'service_city' and d ? 'service_state') then
     derived := coalesce(d->'_derived_location','{}'::jsonb)||jsonb_build_object('zip',a.postal_code);
     if not d ? 'service_state' then
       d := d||jsonb_build_object('service_state',a.state); derived := derived||jsonb_build_object('state',a.state);
     end if;
     if not d ? 'service_city' then
       city := case a.zone when 'manhattan' then 'New York' when 'brooklyn' then 'Brooklyn' when 'queens' then 'Queens' end;
       if city is not null then
         d := d||jsonb_build_object('service_city',city); derived := derived||jsonb_build_object('city',city);
       end if;
     end if;
     d := d||jsonb_build_object('_derived_location',derived);
   end if;
   if d is distinct from s.details then
     update public.call_intakes set details=d, updated_at=now() where call_id=p_call_id;
   end if;
   perform public.refresh_dispatcher_alerts(p_call_id);
   select * into s from public.call_intakes where call_id=p_call_id;
   r := r||jsonb_build_object('state',to_jsonb(s),'readiness',public.intake_readiness(s.details),
     'priority_request',public.priority_request_summary(p_call_id),'membership',public.current_call_membership(p_call_id));
 end if;
 return r;
end;
$$;
revoke all on function public.update_call_intake_before_guard(text,text,text,integer,jsonb,text,text,text),
 public.update_call_intake(text,text,text,integer,jsonb,text,text,text) from public,anon,authenticated,service_role;
grant execute on function public.update_call_intake(text,text,text,integer,jsonb,text,text,text) to service_role;
