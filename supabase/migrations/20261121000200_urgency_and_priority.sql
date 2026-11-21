-- Demo policy v1: caller-reported facts, not inferred weather or medical diagnosis.
create function public.assess_intake_risk(facts jsonb) returns jsonb
language plpgsql immutable set search_path = '' as $$
declare
    reasons jsonb := '[]';
    missing jsonb := '[]';
    risk_level text := 'routine';
begin
    if facts->>'gas_smell_reported' = 'true' then
        reasons := reasons || '"gas_smell"'::jsonb;
    end if;
    if facts->>'heating_unavailable' = 'true' and facts->>'cold_conditions_reported' = 'true' then
        reasons := reasons || '"no_heat_cold"'::jsonb;
    end if;
    if facts->>'cooling_unavailable' = 'true' and facts->>'vulnerable_resident' = 'true' then
        reasons := reasons || '"no_cooling_vulnerable"'::jsonb;
    end if;
    if reasons ? 'gas_smell' then
        risk_level := 'emergency';
    elsif jsonb_array_length(reasons) > 0 then
        risk_level := 'priority';
    else
        if facts->>'heating_unavailable' = 'true'
           and coalesce(facts->>'cold_conditions_reported','unknown') not in ('true','false') then
            missing := missing || '"cold_conditions_reported"'::jsonb;
        end if;
        if facts->>'cooling_unavailable' = 'true'
           and coalesce(facts->>'vulnerable_resident','unknown') not in ('true','false') then
            missing := missing || '"vulnerable_resident"'::jsonb;
        end if;
        if coalesce(facts->>'heating_unavailable','unknown') = 'unknown'
           and coalesce(facts->>'cooling_unavailable','unknown') = 'unknown' then
            if coalesce(facts->>'service_type','unknown') = 'unknown' then
                missing := missing || '"service_type"'::jsonb;
            elsif facts->>'service_type' = 'repair' then
                missing := missing || '"outage_status"'::jsonb;
            end if;
        end if;
        if jsonb_array_length(missing) > 0 then risk_level := 'needs_assessment'; end if;
    end if;
    return jsonb_build_object('level',risk_level,'reasons',reasons,
                             'missing_risk_fields',missing,'policy_version','demo-v1');
end;
$$;

alter table public.call_intakes add column triage jsonb not null
    default '{"level":"needs_assessment","reasons":[],"missing_risk_fields":["service_type"],"policy_version":"demo-v1"}';
update public.call_intakes set triage = public.assess_intake_risk(details);

create table public.priority_requests (
    id uuid primary key default gen_random_uuid(),
    call_id text not null unique references public.call_intakes(call_id) on delete cascade,
    highest_level text not null check (highest_level in ('priority','emergency')),
    current_level text not null check (current_level in ('routine','needs_assessment','priority','emergency')),
    observed_reasons jsonb not null,
    current_reasons jsonb not null,
    latest_intake_revision integer not null,
    needs_review boolean not null default false,
    status text not null default 'recorded' check (status = 'recorded'),
    notification_status text not null default 'not_connected' check (notification_status = 'not_connected'),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
alter table public.priority_requests enable row level security;
revoke all on public.priority_requests from public, anon, authenticated, service_role;
grant select on public.priority_requests to service_role;

create function public.set_intake_triage() returns trigger
language plpgsql set search_path = '' as $$
begin
    new.triage := public.assess_intake_risk(new.details);
    return new;
end;
$$;
create trigger compute_intake_triage before insert or update of details
    on public.call_intakes for each row execute function public.set_intake_triage();

-- Runs in the SAME transaction as the intake change; partial urgent cases persist.
create function public.record_priority_request() returns trigger
language plpgsql security definer set search_path = '' as $$
begin
    if new.triage->>'level' in ('priority','emergency') then
        insert into public.priority_requests (
            call_id, highest_level, current_level, observed_reasons,
            current_reasons, latest_intake_revision
        ) values (
            new.call_id, new.triage->>'level', new.triage->>'level',
            new.triage->'reasons', new.triage->'reasons', new.revision
        ) on conflict (call_id) do update set
            highest_level = case when public.priority_requests.highest_level = 'emergency'
                                     or excluded.highest_level = 'emergency'
                                 then 'emergency' else 'priority' end,
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
create trigger persist_priority_request after insert or update of details
    on public.call_intakes for each row execute function public.record_priority_request();

create function public.priority_request_summary(p_call_id text) returns jsonb
language sql volatile security definer set search_path = '' as $$
    select jsonb_build_object('id', id, 'highest_level', highest_level,
                             'current_level', current_level, 'needs_review', needs_review,
                             'status', status, 'notification_status', notification_status)
    from public.priority_requests where call_id = p_call_id;
$$;
revoke all on function public.assess_intake_risk(jsonb), public.set_intake_triage(),
    public.record_priority_request(), public.priority_request_summary(text)
    from public, anon, authenticated;

comment on table public.priority_requests is
    'Demo priority queue only. Recorded is not notified, dispatched or acknowledged. Corrections require human review.';

create or replace function public.update_call_intake(
    p_call_id text,
    p_operation_id text,
    p_originating_number text,
    p_expected_revision integer,
    p_changes jsonb,
    p_caller_quote text,
    p_confirmation_address text default null
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
        where item.key not in ('caller_name','property_type','issue_summary','service_address','address_confirmed','service_type','heating_unavailable','cooling_unavailable','cold_conditions_reported','vulnerable_resident','gas_smell_reported')
           or item.value = 'null'::jsonb
           or (item.key = 'address_confirmed' and jsonb_typeof(item.value) <> 'boolean')
           or (item.key in ('heating_unavailable','cooling_unavailable','cold_conditions_reported','vulnerable_resident','gas_smell_reported') and jsonb_typeof(item.value) <> 'boolean' and item.value <> '"unknown"'::jsonb)
           or (item.key not in ('address_confirmed','heating_unavailable','cooling_unavailable','cold_conditions_reported','vulnerable_resident','gas_smell_reported') and
               (jsonb_typeof(item.value) <> 'string' or length(trim(item.value #>> '{}')) not between 1 and 500))
    ) or (p_changes ? 'property_type' and p_changes->>'property_type' not in ('residential','commercial','unknown')) or (p_changes ? 'service_type' and p_changes->>'service_type' not in ('repair','maintenance','other','unknown')) then
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
           or prior.confirmation_address is distinct from p_confirmation_address then
            return jsonb_build_object('status','operation_conflict','state',to_jsonb(current_state),'priority_request',public.priority_request_summary(p_call_id));
        end if;
        -- Return the latest state, not an obsolete snapshot from an earlier revision.
        return jsonb_build_object('status','saved','replayed',true,'state',to_jsonb(current_state),'priority_request',public.priority_request_summary(p_call_id));
    end if;
    if current_state.revision <> p_expected_revision then
        return jsonb_build_object('status','revision_conflict','state',to_jsonb(current_state),'priority_request',public.priority_request_summary(p_call_id));
    end if;
    if p_changes->>'address_confirmed' = 'true' and (
        p_changes ? 'service_address'
        or current_state.details->>'service_address' is null
        or p_confirmation_address is distinct from current_state.details->>'service_address'
    ) then
        return jsonb_build_object('status','confirmation_mismatch','state',to_jsonb(current_state),'priority_request',public.priority_request_summary(p_call_id));
    end if;

    merged := current_state.details || p_changes;
    if p_changes ? 'service_address' and
       p_changes->>'service_address' is distinct from current_state.details->>'service_address' then
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
        changes, caller_quote, confirmation_address
    ) values (
        p_call_id, p_operation_id, p_expected_revision, current_state.revision,
        p_changes, p_caller_quote, p_confirmation_address
    );
    return jsonb_build_object('status','saved','replayed',false,'state',to_jsonb(current_state),'priority_request',public.priority_request_summary(p_call_id));
end;
$$;

