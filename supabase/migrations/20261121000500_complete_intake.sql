-- Full technician intake and deliberately narrow, configurable DEMO coverage.
-- The customer's real three counties/hours remain an owner decision.
create table public.demo_service_areas (
    postal_code text primary key check (postal_code ~ '^[0-9]{5}$'),
    zone text not null,
    county text not null,
    cities text[] not null,
    state text not null default 'NY',
    enabled boolean not null default true
);
insert into public.demo_service_areas(postal_code,zone,county,cities) values
('10023','manhattan','New York',array['new york','new york city','manhattan']),
('10024','manhattan','New York',array['new york','new york city','manhattan']),
('10025','manhattan','New York',array['new york','new york city','manhattan']),
('10075','manhattan','New York',array['new york','new york city','manhattan']),
('11201','brooklyn','Kings',array['brooklyn','new york','new york city']),
('11375','queens','Queens',array['forest hills','queens','new york','new york city']);
alter table public.demo_service_areas enable row level security;
revoke all on public.demo_service_areas from public,anon,authenticated,service_role;
grant select on public.demo_service_areas to service_role;

create function public.intake_readiness(d jsonb) returns jsonb
language plpgsql stable security definer set search_path = '' as $$
declare a public.demo_service_areas%rowtype; missing jsonb := '[]'; k text; coverage text; scope text := 'eligible'; risk jsonb := public.assess_intake_risk(d);
begin
    if d->>'property_type' = 'commercial' or d->>'service_category' = 'other' or d->>'service_type' = 'other' then scope := 'out_of_scope';
    elsif coalesce(d->>'property_type','unknown') <> 'residential' or coalesce(d->>'service_category','unknown') <> 'hvac' then scope := 'needs_details'; end if;
    select * into a from public.demo_service_areas where postal_code=d->>'service_zip' and enabled;
    if d->>'service_zip' is null or d->>'service_state' is null or d->>'service_city' is null then coverage := 'needs_details';
    elsif a.postal_code is null or d->>'service_state' <> a.state then coverage := 'out_of_area';
    elsif not (public.lookup_text(d->>'service_city') = any(a.cities)) then coverage := 'needs_city_clarification';
    else coverage := 'covered'; end if;
    foreach k in array array['caller_name','callback_number','service_address','service_unit','service_city','service_state','service_zip','issue_summary'] loop
        if nullif(trim(d->>k),'') is null then missing := missing || to_jsonb(k); end if;
    end loop;
    foreach k in array array['address_confirmed','callback_confirmed'] loop
        if coalesce(d->>k,'false') <> 'true' then missing := missing || to_jsonb(k); end if;
    end loop;
    if coalesce(d->>'service_type','unknown') not in ('repair','maintenance') then missing := missing || '"service_type"'; end if;
    if coalesce(d->>'property_type','unknown') <> 'residential' then missing := missing || '"property_type"'; end if;
    if coalesce(d->>'service_category','unknown') <> 'hvac' then missing := missing || '"service_category"'; end if;
    return jsonb_build_object('scope',scope,'coverage',coverage,'zone',a.zone,'policy','demo-coverage-v1',
        'missing_fields',missing,'risk',risk,
        'can_book',scope='eligible' and coverage='covered' and missing='[]' and risk->>'level'='routine',
        'next_required',case when risk->>'level' in ('emergency','priority') then risk->>'level'
            when risk->>'level'='needs_assessment' then risk->'missing_risk_fields'->>0
            when scope='out_of_scope' then 'explain_scope' when coverage='out_of_area' then 'explain_coverage'
            when coverage='needs_city_clarification' then 'clarify_city' else missing->>0 end);
end;
$$;
revoke all on function public.intake_readiness(jsonb) from public,anon,authenticated,service_role;

alter table public.intake_events add column confirmation_callback text;
drop function public.update_call_intake(text,text,text,integer,jsonb,text,text);
create or replace function public.update_call_intake(
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
        where item.key not in ('caller_name','property_type','issue_summary','service_address','service_unit','service_zip','service_city','service_state','address_confirmed','service_type','heating_unavailable','cooling_unavailable','cold_conditions_reported','vulnerable_resident','gas_smell_reported','membership_claim','service_city','service_state','service_category','callback_number','callback_confirmed','issue_duration','equipment_notes','access_notes','onsite_contact','preferred_date')
           or item.value = 'null'::jsonb
           or (item.key in ('address_confirmed','callback_confirmed') and jsonb_typeof(item.value) <> 'boolean')
           or (item.key in ('heating_unavailable','cooling_unavailable','cold_conditions_reported','vulnerable_resident','gas_smell_reported','membership_claim') and jsonb_typeof(item.value) <> 'boolean' and item.value <> '"unknown"'::jsonb)
           or (item.key not in ('address_confirmed','callback_confirmed','heating_unavailable','cooling_unavailable','cold_conditions_reported','vulnerable_resident','gas_smell_reported','membership_claim') and
               (jsonb_typeof(item.value) <> 'string' or length(trim(item.value #>> '{}')) not between 1 and 500))
    ) or (p_changes ? 'property_type' and p_changes->>'property_type' not in ('residential','commercial','unknown')) or (p_changes ? 'service_type' and p_changes->>'service_type' not in ('repair','maintenance','other','unknown')) or (p_changes ? 'service_zip' and p_changes->>'service_zip' !~ '^[0-9]{5}$')
       or (p_changes ? 'service_state' and p_changes->>'service_state' !~ '^[A-Z]{2}$')
       or (p_changes ? 'callback_number' and p_changes->>'callback_number' !~ '^\+[1-9][0-9]{7,14}$')
       or (p_changes ? 'service_category' and p_changes->>'service_category' not in ('hvac','other','unknown'))
       or (p_changes ? 'preferred_date' and (p_changes->>'preferred_date')::date is null)
       or (p_changes ? 'service_unit' and length(p_changes->>'service_unit') > 50) then
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


revoke all on function public.update_call_intake(text,text,text,integer,jsonb,text,text,text) from public,anon,authenticated,service_role;
grant execute on function public.update_call_intake(text,text,text,integer,jsonb,text,text,text) to service_role;
