-- Triage v2
--  * "No heat + vulnerable resident" is priority (previously only "no AC + vulnerable" was).
--  * Every heating/cooling outage gets ONE neutral question about who is at home
--    (vulnerability_check). It blocks routine booking until answered (an "unknown" answer
--    counts), and is a non-blocking hint when weather already made the call priority.
--  * Priority is tiered: urgent (a person at risk / caller says they can't safely wait /
--    reported cold indoors) ranks above elevated (outage in qualifying weather only),
--    so a cold snap does not turn the whole queue into one undifferentiated "priority".
--  * Carbon monoxide alarms, smoke/burning smells and electrical sparking are emergencies,
--    handled exactly like a gas smell (no booking, evacuation guidance first).

-- The intake validator accepts the backend-only safety_hazard field. The model cannot
-- send it through update_intake; only report_safety_emergency sets it.
create or replace function public.save_call_intake(
    p_call_id text,
    p_operation_id text,
    p_originating_number text,
    p_expected_revision integer,
    p_changes jsonb,
    p_caller_quote text,
    p_confirmation_address text default null,
    p_confirmation_callback text default null
) returns jsonb
language plpgsql security definer set search_path = '' as $$
declare
    current_state public.call_intakes%rowtype;
    prior public.intake_events%rowtype;
    merged jsonb;
begin
    if nullif(trim(p_call_id), '') is null or nullif(trim(p_operation_id), '') is null
       or p_expected_revision is null or p_expected_revision < 0
       or nullif(trim(p_caller_quote), '') is null
       or p_changes is null or jsonb_typeof(p_changes) <> 'object' or p_changes = '{}'::jsonb then
        raise exception 'Invalid intake update';
    end if;
    if exists (
        select 1 from jsonb_each(p_changes) as item
        where item.key not in ('caller_name','property_type','issue_summary','service_address','service_unit','service_zip','service_city','service_state','address_confirmed','service_type','heating_unavailable','cooling_unavailable','cold_conditions_reported','vulnerable_resident','unsafe_to_wait','gas_smell_reported','membership_claim','service_city','service_state','service_category','callback_number','callback_confirmed','issue_duration','equipment_notes','access_notes','onsite_contact','preferred_date','safety_hazard')
           or item.value = 'null'::jsonb
           or (item.key in ('address_confirmed','callback_confirmed') and jsonb_typeof(item.value) <> 'boolean')
           or (item.key in ('heating_unavailable','cooling_unavailable','cold_conditions_reported','vulnerable_resident','unsafe_to_wait','gas_smell_reported','membership_claim') and jsonb_typeof(item.value) <> 'boolean' and item.value <> '"unknown"'::jsonb)
           or (item.key not in ('address_confirmed','callback_confirmed','heating_unavailable','cooling_unavailable','cold_conditions_reported','vulnerable_resident','unsafe_to_wait','gas_smell_reported','membership_claim') and
               (jsonb_typeof(item.value) <> 'string' or length(trim(item.value #>> '{}')) not between 1 and 500))
    ) or (p_changes ? 'property_type' and p_changes->>'property_type' not in ('residential','commercial','unknown')) or (p_changes ? 'service_type' and p_changes->>'service_type' not in ('repair','maintenance','other','unknown')) or (p_changes ? 'service_zip' and p_changes->>'service_zip' !~ '^[0-9]{5}$')
       or (p_changes ? 'service_state' and p_changes->>'service_state' !~ '^[A-Z]{2}$')
       or (p_changes ? 'callback_number' and p_changes->>'callback_number' !~ '^\+[1-9][0-9]{7,14}$')
       or (p_changes ? 'service_category' and p_changes->>'service_category' not in ('hvac','other','unknown'))
       or (p_changes ? 'preferred_date' and (p_changes->>'preferred_date')::date is null)
       or (p_changes ? 'service_unit' and length(p_changes->>'service_unit') > 50)
       or (p_changes ? 'safety_hazard' and p_changes->>'safety_hazard' not in ('carbon_monoxide_alarm','smoke_or_burning_smell','electrical_sparking')) then
        raise exception 'Invalid intake fields';
    end if;

    insert into public.call_intakes(call_id, originating_number)
        values (p_call_id, p_originating_number) on conflict (call_id) do nothing;
    select * into current_state from public.call_intakes where call_id = p_call_id for update;
    select * into prior from public.intake_events
        where call_id = p_call_id and operation_id = p_operation_id;
    if found then
        if prior.changes <> p_changes or prior.expected_revision <> p_expected_revision
           or prior.caller_quote <> p_caller_quote
           or prior.confirmation_address is distinct from p_confirmation_address
           or prior.confirmation_callback is distinct from p_confirmation_callback then
            return jsonb_build_object('status','operation_conflict','readiness',public.intake_readiness(current_state.details), 'state',to_jsonb(current_state),'priority_request',public.priority_request_summary(p_call_id),'membership',public.current_call_membership(p_call_id));
        end if;
        -- Return the latest state, not an obsolete snapshot from an earlier revision.
        return jsonb_build_object('status','saved','replayed',true,'readiness',public.intake_readiness(current_state.details), 'state',to_jsonb(current_state),'priority_request',public.priority_request_summary(p_call_id),'membership',public.current_call_membership(p_call_id));
    end if;
    if current_state.revision <> p_expected_revision then
        return jsonb_build_object('status','revision_conflict','readiness',public.intake_readiness(current_state.details), 'state',to_jsonb(current_state),'priority_request',public.priority_request_summary(p_call_id),'membership',public.current_call_membership(p_call_id));
    end if;
    if p_changes->>'address_confirmed' = 'true' and (
        p_changes ?| array['service_address','service_unit','service_zip','service_city','service_state']
        or current_state.details->>'service_address' is null
        or p_confirmation_address is distinct from current_state.details->>'service_address'
    ) then
        return jsonb_build_object('status','confirmation_mismatch','readiness',public.intake_readiness(current_state.details), 'state',to_jsonb(current_state),'priority_request',public.priority_request_summary(p_call_id),'membership',public.current_call_membership(p_call_id));
    end if;

    if p_changes->>'callback_confirmed' = 'true' and (
        p_changes ? 'callback_number' or current_state.details->>'callback_number' is null
        or p_confirmation_callback is distinct from current_state.details->>'callback_number'
    ) then
        return jsonb_build_object('status','confirmation_mismatch','readiness',public.intake_readiness(current_state.details), 'state',to_jsonb(current_state));
    end if;
    merged := current_state.details || p_changes;
    if p_changes ? 'callback_number' and p_changes->'callback_number' is distinct from current_state.details->'callback_number' then
        merged := (merged - 'callback_confirmation_quote') || '{"callback_confirmed":false}';
    end if;
    if p_changes->>'callback_confirmed' = 'true' then
        merged := merged || jsonb_build_object('callback_confirmation_quote',p_caller_quote);
    elsif p_changes->>'callback_confirmed' = 'false' then
        merged := merged - 'callback_confirmation_quote';
    end if;
    if exists (select 1 from jsonb_each(p_changes) change
               where change.key in ('service_address','service_unit','service_zip','service_city','service_state')
                 and change.value is distinct from current_state.details->change.key) then
        merged := merged || '{"address_confirmed":false}'::jsonb;
        merged := merged - 'confirmation_quote';
    end if;
    if p_changes->>'address_confirmed' = 'true' then
        merged := merged || jsonb_build_object('confirmation_quote',p_caller_quote);
    elsif p_changes->>'address_confirmed' = 'false' then
        merged := merged - 'confirmation_quote';
    end if;
    update public.call_intakes set details = merged,
        revision = revision + 1, updated_at = now()
        where call_id = p_call_id returning * into current_state;
    insert into public.intake_events(
        call_id, operation_id, expected_revision, applied_revision,
        changes, caller_quote, confirmation_address, confirmation_callback
    ) values (
        p_call_id, p_operation_id, p_expected_revision, current_state.revision,
        p_changes, p_caller_quote, p_confirmation_address, p_confirmation_callback
    );
    return jsonb_build_object('status','saved','replayed',false,'readiness',public.intake_readiness(current_state.details), 'state',to_jsonb(current_state),'priority_request',public.priority_request_summary(p_call_id),'membership',public.current_call_membership(p_call_id));
end;
$$;

create or replace function public.assess_intake_risk(facts jsonb) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare
 reasons jsonb := '[]'; missing jsonb := '[]'; ask jsonb := '[]';
 level text := 'routine'; tier text; w jsonb;
 heat_out boolean := coalesce(facts->>'heating_unavailable'='true',false);
 cool_out boolean := coalesce(facts->>'cooling_unavailable'='true',false);
 outage boolean := coalesce(facts->>'heating_unavailable'='true',false) or coalesce(facts->>'cooling_unavailable'='true',false);
 vulnerable boolean := coalesce(facts->>'vulnerable_resident'='true',false);
begin
 if facts->>'gas_smell_reported'='true' then reasons := reasons||'"gas_smell"'::jsonb; end if;
 if facts ? 'safety_hazard' then reasons := reasons||to_jsonb(facts->>'safety_hazard'); end if;
 if jsonb_array_length(reasons)>0 then
   return jsonb_build_object('level','emergency','priority_tier','emergency','reasons',reasons,
     'missing_risk_fields','[]'::jsonb,'ask_next','[]'::jsonb,'policy_version','triage-v2');
 end if;

 -- Urgent: a person is at risk, or the caller reports conditions they can't safely wait in.
 if heat_out and facts->>'cold_conditions_reported'='true' then reasons := reasons||'"no_heat_cold"'::jsonb; end if;
 if heat_out and vulnerable then reasons := reasons||'"no_heat_vulnerable"'::jsonb; end if;
 if cool_out and vulnerable then reasons := reasons||'"no_cooling_vulnerable"'::jsonb; end if;
 if facts->>'unsafe_to_wait'='true' and (outage or facts->>'service_type'='repair') then
   reasons := reasons||'"cannot_safely_wait"'::jsonb;
 end if;
 if jsonb_array_length(reasons)>0 then tier := 'urgent'; end if;

 if outage then
   -- Elevated: the outage coincides with qualifying cold/heat in the forecast or alerts.
   w := public.weather_for_intake(facts);
   if heat_out and w->>'cold'='true' then reasons := reasons||'"no_heat_cold_weather"'::jsonb; end if;
   if cool_out and w->>'hot'='true' then reasons := reasons||'"no_cooling_hot_weather"'::jsonb; end if;
   if tier is null and jsonb_array_length(reasons)>0 then tier := 'elevated'; end if;
   if not (facts ? 'vulnerable_resident') and tier is distinct from 'urgent' then
     if tier is null then missing := missing||'"vulnerability_check"'::jsonb;
     else ask := ask||'"vulnerability_check"'::jsonb; end if;
   end if;
 end if;

 if tier is not null then
   level := 'priority';
 else
   -- Unknown weather is not mild weather: one neutral comfort/waiting question.
   if outage and coalesce(w->>'status','unavailable')='unavailable' and not vulnerable
      and coalesce(facts->>'unsafe_to_wait','unknown') not in ('true','false') then
     missing := missing||'"temperature_concern"'::jsonb;
   end if;
   if coalesce(facts->>'heating_unavailable','unknown')='unknown'
      and coalesce(facts->>'cooling_unavailable','unknown')='unknown' then
     if coalesce(facts->>'service_type','unknown')='unknown' then missing := missing||'"service_type"'::jsonb;
     elsif facts->>'service_type'='repair' then missing := missing||'"outage_status"'::jsonb; end if;
   end if;
   if jsonb_array_length(missing)>0 then level := 'needs_assessment'; end if;
 end if;
 return jsonb_build_object('level',level,'priority_tier',tier,'reasons',reasons,
   'missing_risk_fields',missing,'ask_next',ask,'policy_version','triage-v2');
end;
$$;


-- One safety action for every hazard. Vapi speaks the fixed evacuation message before
-- this RPC runs; recording is monotonic, so it serializes on current state rather than
-- letting a stale model revision delay it (same pattern as report_gas_emergency).
create function public.report_safety_emergency(p_call_id text,p_operation_id text,p_hazard text,p_caller_quote text) returns jsonb
language plpgsql security definer set search_path='' as $$
declare revision integer; prior_revision integer; changes jsonb;
begin
 if nullif(trim(p_call_id),'') is null or nullif(trim(p_operation_id),'') is null then raise exception 'Invalid call scope'; end if;
 changes := case
   when p_hazard='gas_smell' then '{"gas_smell_reported":true}'::jsonb
   when p_hazard in ('carbon_monoxide_alarm','smoke_or_burning_smell','electrical_sparking')
     then jsonb_build_object('safety_hazard',p_hazard)
 end;
 if changes is null then raise exception 'Invalid hazard'; end if;
 insert into public.call_intakes(call_id) values(p_call_id) on conflict(call_id) do nothing;
 select i.revision into revision from public.call_intakes i where call_id=p_call_id for update;
 select expected_revision into prior_revision from public.intake_events where call_id=p_call_id and operation_id=p_operation_id;
 return public.update_call_intake(p_call_id,p_operation_id,null,coalesce(prior_revision,revision),
  changes,p_caller_quote,null,null);
end;
$$;
revoke all on function public.report_safety_emergency(text,text,text,text) from public,anon,authenticated,service_role;
grant execute on function public.report_safety_emergency(text,text,text,text) to service_role;


-- Persist the highest tier a call ever reached, so a dispatcher can sort the queue.
alter table public.priority_requests add column priority_tier text
  check (priority_tier in ('emergency','urgent','elevated'));

create function public.priority_tier_rank(t text) returns integer
language sql immutable set search_path='' as $$
 select case t when 'emergency' then 3 when 'urgent' then 2 when 'elevated' then 1 else 0 end;
$$;
revoke all on function public.priority_tier_rank(text) from public,anon,authenticated,service_role;

create or replace function public.record_priority_request() returns trigger
language plpgsql security definer set search_path = '' as $$
begin
    if new.triage->>'level' in ('priority','emergency') then
        insert into public.priority_requests (
            call_id, highest_level, current_level, observed_reasons,
            current_reasons, latest_intake_revision, priority_tier
        ) values (
            new.call_id, new.triage->>'level', new.triage->>'level',
            new.triage->'reasons', new.triage->'reasons', new.revision, new.triage->>'priority_tier'
        ) on conflict (call_id) do update set
            highest_level = case when public.priority_requests.highest_level = 'emergency'
                                     or excluded.highest_level = 'emergency'
                                 then 'emergency' else 'priority' end,
            priority_tier = case when public.priority_tier_rank(excluded.priority_tier)
                                      > public.priority_tier_rank(public.priority_requests.priority_tier)
                                 then excluded.priority_tier else public.priority_requests.priority_tier end,
            current_level = excluded.current_level,
            observed_reasons = (select jsonb_agg(reason order by reason) from (
                select distinct value as reason from jsonb_array_elements(
                    public.priority_requests.observed_reasons || excluded.observed_reasons
                )
            ) combined),
            needs_review = public.priority_requests.needs_review
                or not (public.priority_requests.current_reasons <@ excluded.current_reasons),
            current_reasons = excluded.current_reasons,
            latest_intake_revision = excluded.latest_intake_revision,
            updated_at = now();
    else
        -- A correction never silently closes a previously recorded urgent case.
        update public.priority_requests set
            current_level = new.triage->>'level', current_reasons = new.triage->'reasons',
            latest_intake_revision = new.revision, needs_review = true, updated_at = now()
            where call_id = new.call_id;
    end if;
    return new;
end;
$$;


create or replace function public.priority_request_summary(p_call_id text) returns jsonb
language sql volatile security definer set search_path = '' as $$
    select jsonb_build_object('id', id, 'highest_level', highest_level, 'priority_tier', priority_tier,
                             'current_level', current_level, 'needs_review', needs_review,
                             'status', status, 'notification_status', notification_status)
    from public.priority_requests where call_id = p_call_id;
$$;
