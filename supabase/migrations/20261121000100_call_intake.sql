create table public.call_intakes (
    call_id text primary key,
    originating_number text,
    revision integer not null default 0 check (revision >= 0),
    details jsonb not null default '{"property_type":"unknown","address_confirmed":false}',
    updated_at timestamptz not null default now()
);

create table public.intake_events (
    call_id text not null references public.call_intakes(call_id) on delete cascade,
    operation_id text not null,
    expected_revision integer not null,
    applied_revision integer not null,
    changes jsonb not null,
    caller_quote text not null,
    confirmation_address text,
    source text not null default 'assistant_reported',
    created_at timestamptz not null default now(),
    primary key (call_id, operation_id)
);

alter table public.call_intakes enable row level security;
alter table public.intake_events enable row level security;
revoke all on public.call_intakes, public.intake_events from public, anon, authenticated, service_role;
grant select on public.call_intakes, public.intake_events to service_role;

-- One transaction locks a call, checks revisions and deduplicates tool retries.
-- Only this RPC may write; a model-supplied customer ID never links an account.
create function public.update_call_intake(
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
        where item.key not in ('caller_name','property_type','issue_summary','service_address','address_confirmed')
           or item.value = 'null'::jsonb
           or (item.key = 'address_confirmed' and jsonb_typeof(item.value) <> 'boolean')
           or (item.key <> 'address_confirmed' and
               (jsonb_typeof(item.value) <> 'string' or length(trim(item.value #>> '{}')) not between 1 and 500))
    ) or (p_changes ? 'property_type' and p_changes->>'property_type' not in ('residential','commercial','unknown')) then
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
            return jsonb_build_object('status','operation_conflict','state',to_jsonb(current_state));
        end if;
        -- Return the latest state, not an obsolete snapshot from an earlier revision.
        return jsonb_build_object('status','saved','replayed',true,'state',to_jsonb(current_state));
    end if;
    if current_state.revision <> p_expected_revision then
        return jsonb_build_object('status','revision_conflict','state',to_jsonb(current_state));
    end if;
    if p_changes->>'address_confirmed' = 'true' and (
        p_changes ? 'service_address'
        or current_state.details->>'service_address' is null
        or p_confirmation_address is distinct from current_state.details->>'service_address'
    ) then
        return jsonb_build_object('status','confirmation_mismatch','state',to_jsonb(current_state));
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
    return jsonb_build_object('status','saved','replayed',false,'state',to_jsonb(current_state));
end;
$$;

revoke all on function public.update_call_intake(text,text,text,integer,jsonb,text,text)
    from public, anon, authenticated;
grant execute on function public.update_call_intake(text,text,text,integer,jsonb,text,text)
    to service_role;

comment on table public.call_intakes is 'Demo intake only; not an appointment or dispatched service request.';
comment on table public.intake_events is 'Tool-reported quotes and changes for review; not independent verification of caller intent.';
