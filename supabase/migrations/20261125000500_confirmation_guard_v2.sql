-- Confirmation guard v2 (round 2 voice findings R1, R2).
--  * R1: the confirmation flags were read as `p_changes->>'address_confirmed'='true'`, which
--    is NULL when the flag is absent, so `not (addr or cb)` never exited and every save was
--    judged. A caller saying "The address is 99 Example Street..." matched the readback
--    pattern and the whole address save was refused. Flags now coalesce to false.
--  * R2: callers naturally repeat their details when they say yes ("Yes, 410 East 89th
--    Street, apartment 2C", "you can reach me at 212-555-0188"). Containing the saved
--    address or number is no longer treated as the agent's readback, and caller-natural
--    openings ("the address is ...") are dropped from the agent-phrasing pattern. A quote
--    that opens like a readback ("Just to confirm ...") is still refused.
--  * Confirming in the same update that gives a new address or number is refused as
--    `same_turn_as_address_save` / `same_turn_as_callback_save`: the caller hasn't heard it
--    read back yet. The 3-second rule after a save or prefill stays.
--  * When only the confirmation is refused, the other facts in that update are still saved
--    (the address used to be thrown away with it), and the result says so.

create or replace function public.quote_is_agent_readback(quote text, d jsonb, check_address boolean, check_callback boolean) returns boolean
language sql immutable set search_path='' as $$
 select coalesce(public.lookup_text(quote),'') ~ ('^(ok(ay)?[ ,]+)?(so[ ,]+)?(and[ ,]+)?(just[ ,]+)?('
     'to confirm|let me (just )?(confirm|read|double)|confirming|i have (you|your)|i''ve got (you|your)'
     '|we have (you|your)|i see (you|your)|your address is|is (that|this|it) (right|correct)'
     '|can you confirm|could you confirm|does that sound)');
$$;
revoke all on function public.quote_is_agent_readback(text,jsonb,boolean,boolean) from public,anon,authenticated,service_role;

create or replace function public.confirmation_guard(p_call_id text, d jsonb, p_changes jsonb, p_caller_quote text) returns text
language plpgsql stable security definer set search_path='' as $$
declare addr boolean := coalesce(p_changes->>'address_confirmed','')='true';
 cb boolean := coalesce(p_changes->>'callback_confirmed','')='true';
 gap interval := make_interval(secs => coalesce(nullif(current_setting('summit.confirmation_gap_ms',true),'')::numeric,3000)/1000);
 k text;
begin
 if not (addr or cb) then return null; end if;
 if public.quote_is_agent_readback(p_caller_quote,d,addr,cb) then return 'quote_is_readback'; end if;
 if addr then
   foreach k in array array['service_address','service_unit','service_zip'] loop
     if p_changes ? k and p_changes->>k is distinct from d->>k then return 'same_turn_as_address_save'; end if;
   end loop;
   if public.last_field_change(p_call_id,array['service_address','service_unit','service_zip','service_city','service_state'])>clock_timestamp()-gap then
     return 'same_turn_as_address_save'; end if;
 end if;
 if cb then
   if p_changes ? 'callback_number'
      and right(regexp_replace(p_changes->>'callback_number','\D','','g'),10)
          is distinct from right(regexp_replace(coalesce(d->>'callback_number',''),'\D','','g'),10) then
     return 'same_turn_as_callback_save'; end if;
   if public.last_field_change(p_call_id,array['callback_number'])>clock_timestamp()-gap then
     return 'same_turn_as_callback_save'; end if;
 end if;
 return null;
end;
$$;
revoke all on function public.confirmation_guard(text,jsonb,jsonb,text) from public,anon,authenticated,service_role;

alter function public.update_call_intake(text,text,text,integer,jsonb,text,text,text) rename to update_call_intake_before_guard_v2;
revoke all on function public.update_call_intake_before_guard_v2(text,text,text,integer,jsonb,text,text,text) from public,anon,authenticated,service_role;

create function public.update_call_intake(
 p_call_id text,p_operation_id text,p_originating_number text,p_expected_revision integer,
 p_changes jsonb,p_caller_quote text,p_confirmation_address text default null,p_confirmation_callback text default null
) returns jsonb language plpgsql security definer set search_path='' as $$
declare s public.call_intakes%rowtype; reason text; facts jsonb; r jsonb;
begin
 select * into s from public.call_intakes where call_id=p_call_id;
 -- Judge fresh operations at the current revision, including a call's first save.
 if ((found and s.revision=p_expected_revision) or (not found and p_expected_revision=0))
    and jsonb_typeof(p_changes)='object'
    and not exists(select 1 from public.intake_events where call_id=p_call_id and operation_id=p_operation_id) then
   reason := public.confirmation_guard(p_call_id,coalesce(s.details,'{}'::jsonb),p_changes,p_caller_quote);
   facts := p_changes-'address_confirmed'-'callback_confirmed';
   if reason is not null and facts<>'{}'::jsonb then
     -- Keep what the caller said; refuse only the confirmation.
     r := public.update_call_intake_before_guard_v2(p_call_id,p_operation_id,p_originating_number,
            p_expected_revision,facts,p_caller_quote,null,null);
     if r->>'status'='saved' then
       return r||jsonb_build_object('status','needs_caller_yes','reason',reason,'saved_other_changes',true);
     end if;
     return r;
   end if;
 end if;
 return public.update_call_intake_before_guard_v2(p_call_id,p_operation_id,p_originating_number,p_expected_revision,
          p_changes,p_caller_quote,p_confirmation_address,p_confirmation_callback);
end;
$$;
revoke all on function public.update_call_intake(text,text,text,integer,jsonb,text,text,text) from public,anon,authenticated;
grant execute on function public.update_call_intake(text,text,text,integer,jsonb,text,text,text) to service_role;
