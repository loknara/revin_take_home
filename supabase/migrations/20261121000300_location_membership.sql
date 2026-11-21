-- Conversational demo matching only. This does not authenticate account ownership.
alter table public.customers add column full_name text
    check (length(trim(full_name)) between 1 and 200);

create function public.lookup_text(value text) returns text
language sql immutable set search_path = '' as $$
    select lower(regexp_replace(trim(value), '\s+', ' ', 'g'));
$$;
create function public.street_lookup_key(value text) returns text
language plpgsql immutable set search_path = '' as $$
declare result text := lower(regexp_replace(value, '[.,]', ' ', 'g'));
begin
    -- Deliberately limited normalization, not fuzzy matching or geocoding.
    result := regexp_replace(result, '\mw\M', 'west', 'g');
    result := regexp_replace(result, '\me\M', 'east', 'g');
    result := regexp_replace(result, '\mn\M', 'north', 'g');
    result := regexp_replace(result, '\ms\M', 'south', 'g');
    result := regexp_replace(result, '\mst\M', 'street', 'g');
    result := regexp_replace(result, '\mave\M', 'avenue', 'g');
    result := regexp_replace(result, '\mrd\M', 'road', 'g');
    return public.lookup_text(result);
end;
$$;
create function public.unit_lookup_key(value text) returns text
language sql immutable set search_path = '' as $$
    select public.lookup_text(regexp_replace(lower(trim(value)), '^(apartment|apt\.?|unit|#)\s*', ''));
$$;

create table public.service_locations (
    id text primary key default gen_random_uuid()::text,
    customer_id text not null references public.customers(id),
    street_address text not null check (length(trim(street_address)) between 1 and 500),
    unit text not null check (length(trim(unit)) between 1 and 50), -- 'none' = explicitly no unit
    postal_code text not null check (postal_code ~ '^[0-9]{5}$'),
    city text not null,
    state text not null,
    property_type text not null check (property_type in ('residential','commercial','unknown')),
    street_key text generated always as (public.street_lookup_key(street_address)) stored,
    unit_key text generated always as (public.unit_lookup_key(unit)) stored,
    unique(customer_id, street_key, unit_key, postal_code)
);
create index service_location_lookup on public.service_locations(postal_code, street_key, unit_key);

-- One current plan record per location for this demo; no billing/history integration.
create table public.memberships (
    location_id text primary key references public.service_locations(id),
    plan_name text not null,
    status text not null check (status in ('active','inactive')),
    effective_on date not null,
    expires_on date not null,
    check (expires_on >= effective_on)
);

create function public.membership_input_snapshot(details jsonb) returns jsonb
language sql immutable set search_path = '' as $$
    select jsonb_build_object(
        'name', details->'caller_name', 'street', details->'service_address',
        'unit', details->'service_unit', 'zip', details->'service_zip',
        'confirmed', details->'address_confirmed', 'property', details->'property_type',
        'claim', details->'membership_claim');
$$;
create table public.call_membership_checks (
    call_id text not null references public.call_intakes(call_id) on delete cascade,
    intake_revision integer not null,
    input_snapshot jsonb not null,
    customer_id text references public.customers(id),
    location_id text references public.service_locations(id),
    result jsonb not null,
    checked_at timestamptz not null default now(),
    primary key(call_id, intake_revision)
);

alter table public.service_locations enable row level security;
alter table public.memberships enable row level security;
alter table public.call_membership_checks enable row level security;
revoke all on public.service_locations, public.memberships, public.call_membership_checks
    from public, anon, authenticated, service_role;
grant select on public.service_locations, public.memberships, public.call_membership_checks to service_role;

create function public.current_call_membership(p_call_id text) returns jsonb
language sql volatile security definer set search_path = '' as $$
    select coalesce((
        select checks.result
        from public.call_membership_checks checks
        join public.call_intakes intake using (call_id)
        where checks.call_id = p_call_id
          and checks.input_snapshot = public.membership_input_snapshot(intake.details)
          and (checks.checked_at at time zone 'America/New_York')::date =
              (current_timestamp at time zone 'America/New_York')::date
        order by checks.intake_revision desc limit 1
    ), '{"status":"not_checked_for_current_details","membership_status":"unknown"}'::jsonb);
$$;

create function public.resolve_call_membership(p_call_id text, p_expected_revision integer) returns jsonb
language plpgsql security definer set search_path = '' as $$
declare
    intake public.call_intakes%rowtype;
    location public.service_locations%rowtype;
    plan public.memberships%rowtype;
    matches text[];
    d jsonb;
    result jsonb;
    missing jsonb := '[]';
    resolution text;
    member_status text := 'unknown';
    claim text;
    disputed boolean := false;
    today date := (current_timestamp at time zone 'America/New_York')::date;
begin
    if nullif(trim(p_call_id),'') is null or p_expected_revision is null or p_expected_revision < 0 then
        raise exception 'Invalid membership request';
    end if;
    -- Serialize with intake corrections, so this result describes a real revision.
    select * into intake from public.call_intakes where call_id=p_call_id for update;
    if not found then
        return '{"status":"needs_intake","membership_status":"unknown"}'::jsonb;
    end if;
    if intake.revision <> p_expected_revision then
        return jsonb_build_object('status','revision_conflict','membership_status','unknown',
                                  'current_revision',intake.revision);
    end if;
    d := intake.details;
    claim := coalesce(d->>'membership_claim','unknown');
    if intake.triage->>'level' = 'emergency' then
        resolution := 'deferred_for_emergency';
    elsif d->>'property_type' = 'commercial' then
        resolution := 'out_of_scope';
    else
        select coalesce(jsonb_agg(field), '[]'::jsonb) into missing
        from unnest(array['caller_name','service_address','service_unit','service_zip']) field
        where nullif(trim(d->>field),'') is null;
        if jsonb_array_length(missing) > 0 then
            resolution := 'needs_details';
        elsif d->>'address_confirmed' is distinct from 'true' then
            resolution := 'needs_address_confirmation';
        else
            select array_agg(id) into matches from (
                select loc.id
                from public.service_locations loc join public.customers customer on customer.id=loc.customer_id
                where loc.street_key=public.street_lookup_key(d->>'service_address')
                  and loc.unit_key=public.unit_lookup_key(d->>'service_unit')
                  and loc.postal_code=d->>'service_zip'
                  and (
                      public.lookup_text(customer.full_name)=public.lookup_text(d->>'caller_name')
                      or (public.lookup_text(customer.first_name)=public.lookup_text(d->>'caller_name')
                          and exists (select 1 from public.customer_phone_numbers phone
                                      where phone.customer_id=customer.id
                                        and phone.phone_number=intake.originating_number))
                  )
                order by loc.id limit 2
            ) candidates;
            if coalesce(cardinality(matches),0) = 0 then
                resolution := case when position(' ' in public.lookup_text(d->>'caller_name'))=0
                                   then 'needs_full_name' else 'no_match' end;
            elsif cardinality(matches)>1 then
                resolution := 'ambiguous';
            else
                select * into location from public.service_locations where id=matches[1];
                resolution := 'resolved';
                select * into plan from public.memberships where location_id=location.id;
                if not found then member_status := 'none';
                elsif plan.status='inactive' then member_status := 'inactive';
                elsif plan.expires_on < today then member_status := 'expired';
                elsif plan.effective_on > today then member_status := 'not_started';
                else member_status := 'active';
                end if;
                disputed := (claim='true' and member_status<>'active') or
                            (claim='false' and member_status='active');
            end if;
        end if;
    end if;
    result := jsonb_build_object(
        'status',resolution, 'membership_status',member_status,
        'intake_revision',intake.revision, 'missing_fields',missing,
        'caller_claim',claim, 'disputed',disputed,
        'verification_required',member_status='unknown' or disputed,
        'identity_assurance','conversational_demo_only',
        'checked_at',current_timestamp, 'approved_benefits','[]'::jsonb);
    if resolution='resolved' and plan.location_id is not null then
        result := result || jsonb_build_object('plan_name',plan.plan_name,
                    'effective_on',plan.effective_on,'expires_on',plan.expires_on);
    end if;
    insert into public.call_membership_checks(call_id,intake_revision,input_snapshot,customer_id,location_id,result)
        values(p_call_id,intake.revision,public.membership_input_snapshot(d),location.customer_id,location.id,result)
        on conflict(call_id,intake_revision) do update set
            input_snapshot=excluded.input_snapshot, customer_id=excluded.customer_id,
            location_id=excluded.location_id, result=excluded.result, checked_at=now();
    return result;
end;
$$;

revoke all on function public.lookup_text(text), public.street_lookup_key(text),
    public.unit_lookup_key(text), public.membership_input_snapshot(jsonb),
    public.current_call_membership(text), public.resolve_call_membership(text,integer)
    from public, anon, authenticated, service_role;
grant execute on function public.resolve_call_membership(text,integer) to service_role;
comment on table public.call_membership_checks is
    'Demo conversational resolution, not account authentication. Results bind to supplied person/property; phone links are never changed.';

-- Preserve intake retry/urgency behavior; extend location facts and invalidate dependent membership.
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
        where item.key not in ('caller_name','property_type','issue_summary','service_address','service_unit','service_zip','address_confirmed','service_type','heating_unavailable','cooling_unavailable','cold_conditions_reported','vulnerable_resident','gas_smell_reported','membership_claim')
           or item.value = 'null'::jsonb
           or (item.key = 'address_confirmed' and jsonb_typeof(item.value) <> 'boolean')
           or (item.key in ('heating_unavailable','cooling_unavailable','cold_conditions_reported','vulnerable_resident','gas_smell_reported','membership_claim') and jsonb_typeof(item.value) <> 'boolean' and item.value <> '"unknown"'::jsonb)
           or (item.key not in ('address_confirmed','heating_unavailable','cooling_unavailable','cold_conditions_reported','vulnerable_resident','gas_smell_reported','membership_claim') and
               (jsonb_typeof(item.value) <> 'string' or length(trim(item.value #>> '{}')) not between 1 and 500))
    ) or (p_changes ? 'property_type' and p_changes->>'property_type' not in ('residential','commercial','unknown')) or (p_changes ? 'service_type' and p_changes->>'service_type' not in ('repair','maintenance','other','unknown')) or (p_changes ? 'service_zip' and p_changes->>'service_zip' !~ '^[0-9]{5}$')
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
           or prior.confirmation_address is distinct from p_confirmation_address then
            return jsonb_build_object('status','operation_conflict','state',to_jsonb(current_state),'priority_request',public.priority_request_summary(p_call_id),'membership',public.current_call_membership(p_call_id));
        end if;
        -- Return the latest state, not an obsolete snapshot from an earlier revision.
        return jsonb_build_object('status','saved','replayed',true,'state',to_jsonb(current_state),'priority_request',public.priority_request_summary(p_call_id),'membership',public.current_call_membership(p_call_id));
    end if;
    if current_state.revision <> p_expected_revision then
        return jsonb_build_object('status','revision_conflict','state',to_jsonb(current_state),'priority_request',public.priority_request_summary(p_call_id),'membership',public.current_call_membership(p_call_id));
    end if;
    if p_changes->>'address_confirmed' = 'true' and (
        p_changes ?| array['service_address','service_unit','service_zip']
        or current_state.details->>'service_address' is null
        or p_confirmation_address is distinct from current_state.details->>'service_address'
    ) then
        return jsonb_build_object('status','confirmation_mismatch','state',to_jsonb(current_state),'priority_request',public.priority_request_summary(p_call_id),'membership',public.current_call_membership(p_call_id));
    end if;

    merged := current_state.details || p_changes;
    if exists (select 1 from jsonb_each(p_changes) change
               where change.key in ('service_address','service_unit','service_zip')
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
        changes, caller_quote, confirmation_address
    ) values (
        p_call_id, p_operation_id, p_expected_revision, current_state.revision,
        p_changes, p_caller_quote, p_confirmation_address
    );
    return jsonb_build_object('status','saved','replayed',false,'state',to_jsonb(current_state),'priority_request',public.priority_request_summary(p_call_id),'membership',public.current_call_membership(p_call_id));
end;
$$;

