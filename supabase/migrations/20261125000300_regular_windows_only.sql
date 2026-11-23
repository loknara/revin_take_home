-- Scheduling (decision D1, finding O19).
--  * D1: there is no on-call person. Priority searches return regular windows only: no
--    hourly priority_only windows, and on-call technicians no longer cover off-shift times.
--    Priority still means the earliest regular window (30-minute lead instead of 2 hours)
--    plus the dispatcher alert.
--  * O19: demo/evaluation seeding skips Thanksgiving (4th Thursday of November) and
--    Christmas Day, and disables any holiday windows seeded earlier.

create function public.is_demo_holiday(d date) returns boolean
language sql immutable set search_path='' as $$
 select (extract(month from d)=11 and extract(isodow from d)=4 and extract(day from d) between 22 and 28)
     or (extract(month from d)=12 and extract(day from d)=25);
$$;
revoke all on function public.is_demo_holiday(date) from public,anon,authenticated,service_role;

-- Existing priority-only windows are retired, not deleted (appointments may reference them).
update public.appointment_slots set enabled=false where priority_only and enabled;
update public.appointment_slots set enabled=false
 where inventory_scope in ('demo','evaluation') and enabled
   and public.is_demo_holiday((starts_at at time zone 'America/New_York')::date);

-- Same as technician_dispatch, without the on-call bypass: everyone works regular shifts.
create or replace function public.technician_can_cover(t public.technicians,s public.appointment_slots,is_priority boolean)
 returns boolean language sql stable set search_path='' as $$
 select t.active and s.zone=any(t.zones) and s.service_type=any(t.skills) and not s.priority_only
  and extract(isodow from s.starts_at at time zone 'America/New_York')<>7
  and (s.starts_at at time zone 'America/New_York')::date=(s.ends_at at time zone 'America/New_York')::date
  and extract(hour from s.starts_at at time zone 'America/New_York')>=t.shift_start
  and (s.ends_at at time zone 'America/New_York')::time<=make_time(t.shift_end%24,0,0)
  and not exists(select 1 from public.dispatch_jobs j where j.technician_id=t.id
    and j.inventory_scope=s.inventory_scope
    and tstzrange(j.starts_at,j.ends_at+interval '30 minutes','[)') &&
        tstzrange(s.starts_at,s.ends_at+interval '30 minutes','[)'));
$$;
revoke all on function public.technician_can_cover(public.technicians,public.appointment_slots,boolean) from public,anon,authenticated,service_role;

create or replace function public.seed_demo_capacity(p_scope text) returns void
language plpgsql security definer set search_path='' as $$
begin
 insert into public.appointment_slots(zone,service_type,starts_at,ends_at,capacity,inventory_scope)
 select z.zone,t.kind,((current_timestamp at time zone 'America/New_York')::date+n.day+h.hour*interval '1 hour') at time zone 'America/New_York',
 ((current_timestamp at time zone 'America/New_York')::date+n.day+(h.hour+3)*interval '1 hour') at time zone 'America/New_York',40,p_scope
 from (select distinct zone from public.demo_service_areas where enabled) z
 cross join (values('repair'),('maintenance')) t(kind)
 cross join generate_series(1,14) n(day) cross join (values(9),(13)) h(hour)
 where extract(isodow from (current_timestamp at time zone 'America/New_York')::date+n.day)<>7
   and not public.is_demo_holiday((current_timestamp at time zone 'America/New_York')::date+n.day)
 on conflict(zone,service_type,starts_at,inventory_scope) do nothing;
 update public.appointment_slots set enabled=false
  where inventory_scope=p_scope and enabled and starts_at>clock_timestamp()
    and public.is_demo_holiday((starts_at at time zone 'America/New_York')::date);
end;
$$;
revoke all on function public.seed_demo_capacity(text) from public,anon,authenticated,service_role;

-- Same as technician_dispatch, except priority calls no longer seed or see on-call windows.
create or replace function public.search_slots_before_weather(p_call_id text,p_expected_revision integer) returns jsonb
language plpgsql security definer set search_path='' as $$
declare s public.call_intakes%rowtype; r jsonb; choices jsonb; search public.availability_reads%rowtype;
begin
 select * into s from public.call_intakes where call_id=p_call_id for update;
 if not found then return jsonb_build_object('status','needs_intake'); end if;
 if s.revision<>p_expected_revision then return jsonb_build_object('status','revision_conflict','current_revision',s.revision); end if;
 r:=public.intake_readiness(s.details);
 if r->>'scope'<>'eligible' or r->>'coverage'<>'covered' or coalesce(s.details->>'address_confirmed','false')<>'true'
    or s.details->>'service_type' not in ('repair','maintenance') or s.triage->>'level' not in ('routine','priority') then
   return jsonb_build_object('status','not_eligible','readiness',r); end if;
 if public.call_inventory_scope(p_call_id) in ('demo','evaluation') then
  perform public.seed_demo_capacity(public.call_inventory_scope(p_call_id));
 end if;
 select coalesce(jsonb_agg(option order by option->>'starts_at'),'[]') into choices from (
   select jsonb_build_object('slot_id',slot.id,'starts_at',slot.starts_at,'ends_at',slot.ends_at,
    'local_date',to_char(slot.starts_at at time zone 'America/New_York','YYYY-MM-DD'),
    'arrival_window',to_char(slot.starts_at at time zone 'America/New_York','FMDay, Month FMDD, YYYY FMHH12:MI AM')||' to '||to_char(slot.ends_at at time zone 'America/New_York','FMHH12:MI AM'),
    'timezone','America/New_York') as option
   from public.appointment_slots slot where slot.inventory_scope=public.call_inventory_scope(p_call_id) and slot.enabled and slot.zone=r->>'zone' and slot.service_type=s.details->>'service_type'
     and slot.starts_at>clock_timestamp()+(case when s.triage->>'level'='priority' then interval '30 minutes' else interval '2 hours' end) and slot.starts_at<clock_timestamp()+interval '15 days'
     and (s.details->>'preferred_date' is null or (slot.starts_at at time zone 'America/New_York')::date=(s.details->>'preferred_date')::date)
     and not slot.priority_only
     and exists(select 1 from public.technicians tech where public.technician_can_cover(tech,slot,s.triage->>'level'='priority'))
     and (select count(*) from public.appointments a where a.slot_id=slot.id)<slot.capacity
   order by slot.starts_at limit 3
 ) options;
 insert into public.availability_reads(call_id,dependency_snapshot,result)
   values(p_call_id,public.availability_dependencies(s.details),choices) returning * into search;
 return jsonb_build_object('status',case when choices='[]' then 'no_availability' else 'available' end,
   'search_id',search.id,'slots',choices,'expires_at',search.expires_at,'demo_only',true);
end;
$$;
