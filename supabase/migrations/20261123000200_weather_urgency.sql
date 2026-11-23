-- Add a volunteered inability-to-wait fact to the existing intake validator.
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
        where item.key not in ('caller_name','property_type','issue_summary','service_address','service_unit','service_zip','service_city','service_state','address_confirmed','service_type','heating_unavailable','cooling_unavailable','cold_conditions_reported','vulnerable_resident','unsafe_to_wait','gas_smell_reported','membership_claim','service_city','service_state','service_category','callback_number','callback_confirmed','issue_duration','equipment_notes','access_notes','onsite_contact','preferred_date')
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


-- Cached public conditions contain no caller addresses or personal data.
create table public.service_weather (
 county text primary key check(county in ('New York','Kings','Queens')),
 fetched_at timestamptz not null,
 valid_until timestamptz not null,
 hot boolean not null,
 cold boolean not null,
 evidence jsonb not null,
 check(valid_until>fetched_at and valid_until<=fetched_at+interval '30 minutes')
);
alter table public.service_weather enable row level security;
revoke all on public.service_weather from public,anon,authenticated,service_role;
grant select,insert,update on public.service_weather to service_role;

create function public.weather_for_intake(d jsonb) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare county_name text; rows_count integer; hot boolean; cold boolean; evidence jsonb;
begin
 if d ? 'service_state' and d->>'service_state'<>'NY' then return '{"status":"out_of_area"}'; end if;
 if d ? 'service_zip' then
   select a.county into county_name from public.demo_service_areas a
     where a.postal_code=d->>'service_zip' and a.enabled;
   if county_name is null then return '{"status":"out_of_area"}'; end if;
   select jsonb_build_object('status','current','scope','county','county',w.county,
      'hot',w.hot,'cold',w.cold,'valid_until',w.valid_until,'evidence',w.evidence)
    into evidence from public.service_weather w where w.county=county_name
       and w.fetched_at<=now() and w.valid_until>now();
   return coalesce(evidence,'{"status":"unavailable"}'::jsonb);
 end if;
 -- Before location is known, only a condition shared by all configured counties
 -- may trigger regional priority. A mixed/missing result needs location or caller input.
 select count(*),bool_and(w.hot),bool_and(w.cold),jsonb_agg(w.evidence order by w.county)
 into rows_count,hot,cold,evidence from public.service_weather w
 where w.fetched_at<=now() and w.valid_until>now();
 if rows_count<>3 then return '{"status":"unavailable","scope":"region"}'; end if;
 return jsonb_build_object('status','current','scope','region','hot',hot,'cold',cold,'evidence',evidence);
end;
$$;
revoke all on function public.weather_for_intake(jsonb) from public,anon,authenticated,service_role;

create or replace function public.assess_intake_risk(facts jsonb) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare reasons jsonb:='[]'; missing jsonb:='[]'; level text:='routine'; w jsonb;
 outage boolean := facts->>'heating_unavailable'='true' or facts->>'cooling_unavailable'='true';
begin
 if facts->>'gas_smell_reported'='true' then reasons:=reasons||'"gas_smell"'::jsonb; end if;
 if facts->>'heating_unavailable'='true' and facts->>'cold_conditions_reported'='true' then reasons:=reasons||'"no_heat_cold"'::jsonb; end if;
 if facts->>'cooling_unavailable'='true' and facts->>'vulnerable_resident'='true' then reasons:=reasons||'"no_cooling_vulnerable"'::jsonb; end if;
 if facts->>'unsafe_to_wait'='true' and (outage or facts->>'service_type'='repair') then reasons:=reasons||'"cannot_safely_wait"'::jsonb; end if;
 if outage and not (reasons ? 'gas_smell') then
   w:=public.weather_for_intake(facts);
   if facts->>'heating_unavailable'='true' and w->>'cold'='true' then reasons:=reasons||'"no_heat_cold_weather"'::jsonb; end if;
   if facts->>'cooling_unavailable'='true' and w->>'hot'='true' then reasons:=reasons||'"no_cooling_hot_weather"'::jsonb; end if;
 end if;
 if reasons ? 'gas_smell' then level:='emergency';
 elsif jsonb_array_length(reasons)>0 then level:='priority';
 else
   -- Do not require a medical/demographic screening answer. Unknown weather must
   -- not be interpreted as mild weather: ask one neutral comfort/waiting question.
   if outage and coalesce(w->>'status','unavailable')='unavailable'
      and coalesce(facts->>'unsafe_to_wait','unknown') not in ('true','false') then
      missing:=missing||'"temperature_concern"'::jsonb;
   end if;
   if coalesce(facts->>'heating_unavailable','unknown')='unknown'
      and coalesce(facts->>'cooling_unavailable','unknown')='unknown' then
      if coalesce(facts->>'service_type','unknown')='unknown' then missing:=missing||'"service_type"'::jsonb;
      elsif facts->>'service_type'='repair' then missing:=missing||'"outage_status"'::jsonb; end if;
   end if;
   if jsonb_array_length(missing)>0 then level:='needs_assessment'; end if;
 end if;
 return jsonb_build_object('level',level,'reasons',reasons,'missing_risk_fields',missing,
   'policy_version','weather-v1');
end;
$$;
-- This dependency now reads the weather cache, so must not be constant-folded.
alter function public.availability_dependencies(jsonb) stable;

-- Re-evaluate on recovery/search/proposal/commit, including weather expiring or
-- changing mid-call. A changed decision invalidates the previous intake revision.
create function public.sync_call_weather(p_call_id text) returns void
language plpgsql security definer set search_path='' as $$
declare s public.call_intakes%rowtype;
begin
 select * into s from public.call_intakes where call_id=p_call_id for update;
 if found and s.triage is distinct from public.assess_intake_risk(s.details) then
   update public.call_intakes set details=details,revision=revision+1,updated_at=now() where call_id=p_call_id;
 end if;
end;
$$;
revoke all on function public.sync_call_weather(text) from public,anon,authenticated,service_role;

alter function public.refresh_call_context(text) rename to context_before_weather;
create function public.refresh_call_context(p_call_id text) returns jsonb
language plpgsql security definer set search_path='' as $$
begin
 perform public.sync_call_weather(p_call_id);
 return public.context_before_weather(p_call_id)||jsonb_build_object('weather',
   public.weather_for_intake((select details from public.call_intakes where call_id=p_call_id)));
end;
$$;

alter function public.search_slots(text,integer) rename to search_slots_before_weather;
create function public.search_slots(p_call_id text,p_expected_revision integer) returns jsonb
language plpgsql security definer set search_path='' as $$
begin
 perform public.sync_call_weather(p_call_id);
 return public.search_slots_before_weather(p_call_id,p_expected_revision);
end;
$$;
alter function public.propose_booking(text,integer,uuid,uuid) rename to propose_booking_before_weather;
create function public.propose_booking(p_call_id text,p_expected_revision integer,p_search_id uuid,p_slot_id uuid) returns jsonb
language plpgsql security definer set search_path='' as $$
begin
 perform public.sync_call_weather(p_call_id);
 return public.propose_booking_before_weather(p_call_id,p_expected_revision,p_search_id,p_slot_id);
end;
$$;
alter function public.confirm_booking(text,integer,uuid,text,text) rename to confirm_booking_before_weather;
create function public.confirm_booking(p_call_id text,p_expected_revision integer,p_proposal_id uuid,p_decision text,p_caller_quote text) returns jsonb
language plpgsql security definer set search_path='' as $$
begin
 perform public.sync_call_weather(p_call_id);
 return public.confirm_booking_before_weather(p_call_id,p_expected_revision,p_proposal_id,p_decision,p_caller_quote);
end;
$$;
revoke all on function public.context_before_weather(text),public.search_slots_before_weather(text,integer),
 public.propose_booking_before_weather(text,integer,uuid,uuid),public.confirm_booking_before_weather(text,integer,uuid,text,text),
 public.refresh_call_context(text),public.search_slots(text,integer),public.propose_booking(text,integer,uuid,uuid),
 public.confirm_booking(text,integer,uuid,text,text) from public,anon,authenticated,service_role;
grant execute on function public.refresh_call_context(text),public.search_slots(text,integer),
 public.propose_booking(text,integer,uuid,uuid),public.confirm_booking(text,integer,uuid,text,text) to service_role;

-- Evidence is stored per save, so operators can explain the automatic decision.
create table public.call_weather_evidence (
 id uuid primary key default gen_random_uuid(),
 call_id text not null references public.call_intakes(call_id) on delete cascade,
 intake_revision integer not null, weather jsonb not null, triage jsonb not null,
 created_at timestamptz not null default now()
);
alter table public.call_weather_evidence enable row level security;
revoke all on public.call_weather_evidence from public,anon,authenticated,service_role;
grant select on public.call_weather_evidence to service_role;
create function public.record_call_weather() returns trigger
language plpgsql security definer set search_path='' as $$
begin
 if new.details->>'heating_unavailable'='true' or new.details->>'cooling_unavailable'='true' then
   insert into public.call_weather_evidence(call_id,intake_revision,weather,triage)
    values(new.call_id,new.revision,public.weather_for_intake(new.details),new.triage);
 end if;
 return new;
end;
$$;
revoke all on function public.record_call_weather() from public,anon,authenticated,service_role;
create trigger record_weather after insert or update of details on public.call_intakes
 for each row execute function public.record_call_weather();
