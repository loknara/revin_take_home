create extension if not exists btree_gist;

-- Operational scheduling engine with an explicitly synthetic technician roster.
create table public.technicians (
 id uuid primary key default gen_random_uuid(), code text not null unique,
 display_name text not null, zones text[] not null,
 skills text[] not null default array['repair','maintenance'],
 active boolean not null default true, is_synthetic boolean not null default true,
 on_call boolean not null default false,
 shift_start integer not null default 8 check(shift_start between 0 and 23),
 shift_end integer not null default 18 check(shift_end between 1 and 24),
 check(shift_end>shift_start)
);
insert into public.technicians(code,display_name,zones,on_call)
 select 'synthetic-'||n,
  (array['Alex','Jordan','Taylor','Morgan','Casey','Riley','Jamie','Sam'])[1+(n-1)%8]||' '||
  (array['Reed','Brooks','Parker','Hayes','Bennett'])[1+(n-1)/8],
  array['manhattan','brooklyn','queens'],n%5=0 from generate_series(1,40) n;
alter table public.appointment_slots add column priority_only boolean not null default false;
alter table public.appointments drop constraint appointments_dispatch_status_check;
alter table public.appointments add constraint appointments_dispatch_status_check check(dispatch_status in ('demo_only','assigned'));

create table public.dispatch_jobs (
 id uuid primary key default gen_random_uuid(),
 appointment_id uuid not null unique references public.appointments(id) on delete cascade,
 call_id text not null unique references public.call_intakes(call_id) on delete cascade,
 technician_id uuid not null references public.technicians(id),
 inventory_scope text not null, starts_at timestamptz not null, ends_at timestamptz not null,
 busy_until timestamptz not null,
 priority text not null check(priority in ('routine','priority')),
 status text not null default 'assigned' check(status='assigned'),
 assigned_at timestamptz not null default clock_timestamp(),
 check(ends_at>starts_at and busy_until>=ends_at),
 exclude using gist (inventory_scope with =,technician_id with =,tstzrange(starts_at,busy_until,'[)') with &&)
);
create index technician_schedule on public.dispatch_jobs(technician_id,inventory_scope,starts_at,ends_at);
-- Writes go through the booking transaction; assigned does not mean en route.
do $$ declare t text; begin
 foreach t in array array['technicians','dispatch_jobs'] loop
  execute format('alter table public.%I enable row level security',t);
  execute format('revoke all on public.%I from public,anon,authenticated,service_role',t);
  execute format('grant select on public.%I to service_role',t);
 end loop;
end $$;

create function public.technician_can_cover(t public.technicians,s public.appointment_slots,is_priority boolean)
 returns boolean language sql stable set search_path='' as $$
 select t.active and s.zone=any(t.zones) and s.service_type=any(t.skills)
  and ((is_priority and t.on_call) or (
    extract(isodow from s.starts_at at time zone 'America/New_York')<>7
    and (s.starts_at at time zone 'America/New_York')::date=(s.ends_at at time zone 'America/New_York')::date
    and extract(hour from s.starts_at at time zone 'America/New_York')>=t.shift_start
    and (s.ends_at at time zone 'America/New_York')::time<=make_time(t.shift_end%24,0,0)))
  and not exists(select 1 from public.dispatch_jobs j where j.technician_id=t.id
    and j.inventory_scope=s.inventory_scope
    and tstzrange(j.starts_at,j.ends_at+interval '30 minutes','[)') &&
        tstzrange(s.starts_at,s.ends_at+interval '30 minutes','[)'));
$$;
revoke all on function public.technician_can_cover(public.technicians,public.appointment_slots,boolean) from public,anon,authenticated,service_role;

create or replace function public.seed_demo_capacity(p_scope text) returns void
language sql security definer set search_path='' as $$
 insert into public.appointment_slots(zone,service_type,starts_at,ends_at,capacity,inventory_scope)
 select z.zone,t.kind,((current_timestamp at time zone 'America/New_York')::date+n.day+h.hour*interval '1 hour') at time zone 'America/New_York',
 ((current_timestamp at time zone 'America/New_York')::date+n.day+(h.hour+3)*interval '1 hour') at time zone 'America/New_York',40,p_scope
 from (select distinct zone from public.demo_service_areas where enabled) z
 cross join (values('repair'),('maintenance')) t(kind)
 cross join generate_series(1,14) n(day) cross join (values(9),(13)) h(hour)
 where extract(isodow from (current_timestamp at time zone 'America/New_York')::date+n.day)<>7
 on conflict(zone,service_type,starts_at,inventory_scope) do nothing;
$$;

update public.appointment_slots set capacity=40 where inventory_scope in ('demo','evaluation') and starts_at>now() and not priority_only;

create function public.seed_priority_capacity(p_scope text) returns void
language sql security definer set search_path='' as $$
 insert into public.appointment_slots(zone,service_type,starts_at,ends_at,capacity,inventory_scope,priority_only)
 select z.zone,'repair',date_trunc('hour',clock_timestamp())+h*interval '1 hour',
 date_trunc('hour',clock_timestamp())+(h+2)*interval '1 hour',8,p_scope,true
 from (select distinct zone from public.demo_service_areas where enabled) z cross join generate_series(1,24) h
 on conflict(zone,service_type,starts_at,inventory_scope) do nothing;
$$;
revoke all on function public.seed_priority_capacity(text) from public,anon,authenticated,service_role;

alter function public.intake_readiness(jsonb) rename to intake_readiness_before_dispatch;
create function public.intake_readiness(d jsonb) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare r jsonb:=public.intake_readiness_before_dispatch(d);
begin
 if r->'risk'->>'level'='priority' then
  r:=r||jsonb_build_object('can_book',r->>'scope'='eligible' and r->>'coverage'='covered' and r->'missing_fields'='[]'::jsonb,
    'next_required',r->'missing_fields'->>0);
 end if;
 return r;
end;
$$;
revoke all on function public.intake_readiness_before_dispatch(jsonb),public.intake_readiness(jsonb) from public,anon,authenticated,service_role;

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
  if s.triage->>'level'='priority' then perform public.seed_priority_capacity(public.call_inventory_scope(p_call_id)); end if;
 end if;
 select coalesce(jsonb_agg(option order by option->>'starts_at'),'[]') into choices from (
   select jsonb_build_object('slot_id',slot.id,'starts_at',slot.starts_at,'ends_at',slot.ends_at,
    'local_date',to_char(slot.starts_at at time zone 'America/New_York','YYYY-MM-DD'),
    'arrival_window',to_char(slot.starts_at at time zone 'America/New_York','FMDay, Month FMDD, YYYY FMHH12:MI AM')||' to '||to_char(slot.ends_at at time zone 'America/New_York','FMHH12:MI AM'),
    'timezone','America/New_York') as option
   from public.appointment_slots slot where slot.inventory_scope=public.call_inventory_scope(p_call_id) and slot.enabled and slot.zone=r->>'zone' and slot.service_type=s.details->>'service_type'
     and slot.starts_at>clock_timestamp()+(case when s.triage->>'level'='priority' then interval '30 minutes' else interval '2 hours' end) and slot.starts_at<clock_timestamp()+interval '15 days'
     and (s.details->>'preferred_date' is null or (slot.starts_at at time zone 'America/New_York')::date=(s.details->>'preferred_date')::date)
     and (not slot.priority_only or s.triage->>'level'='priority')
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

create or replace function public.confirm_booking_before_weather(p_call_id text,p_expected_revision integer,p_proposal_id uuid,p_decision text,p_caller_quote text) returns jsonb
language plpgsql security definer set search_path='' as $$
declare s public.call_intakes%rowtype; p public.booking_proposals%rowtype; slot public.appointment_slots%rowtype; readiness jsonb; tech public.technicians%rowtype; appointment_id uuid;
begin
 if p_decision not in ('accepted','declined') or p_decision is null or length(trim(p_caller_quote)) not between 1 and 1000 or p_caller_quote is null then raise exception 'Invalid agreement'; end if;
 select * into s from public.call_intakes where call_id=p_call_id for update;
 if not found then return jsonb_build_object('status','needs_intake'); end if;
 select * into p from public.booking_proposals where id=p_proposal_id and call_id=p_call_id for update;
 if not found then return jsonb_build_object('status','unknown_proposal'); end if;
 -- Reconcile any existing commit before retrying, even if details changed afterward.
 if exists(select 1 from public.appointments where call_id=p_call_id) then return public.get_booking_status(p_call_id); end if;
 if s.revision<>p_expected_revision then return jsonb_build_object('status','revision_conflict','current_revision',s.revision); end if;
 if p.status<>'offered' or p.expires_at<=clock_timestamp() or p.material_revision<>s.material_revision then return jsonb_build_object('status','stale_proposal'); end if;
 if p_decision='declined' then
   insert into public.booking_agreements(proposal_id,call_id,intake_revision,caller_quote,decision) values(p.id,p_call_id,s.revision,p_caller_quote,p_decision);
   update public.booking_proposals set status='declined' where id=p.id;
   return jsonb_build_object('status','declined');
 end if;
 readiness:=public.intake_readiness(s.details);
 if readiness->>'can_book'<>'true' then return jsonb_build_object('status','not_eligible','readiness',readiness); end if;
 select * into slot from public.appointment_slots where id=p.slot_id for update;
 if slot.inventory_scope<>public.call_inventory_scope(p_call_id) or not slot.enabled or slot.starts_at<=clock_timestamp()+(case when s.triage->>'level'='priority' then interval '30 minutes' else interval '2 hours' end) or slot.zone<>readiness->>'zone' or slot.service_type<>s.details->>'service_type' or (slot.priority_only and s.triage->>'level'<>'priority') then
   return jsonb_build_object('status','slot_unavailable'); end if;
 if (select count(*) from public.appointments where slot_id=slot.id)>=slot.capacity then
   update public.booking_proposals set status='invalidated' where id=p.id;
   return jsonb_build_object('status','slot_conflict','next_action','Search for fresh alternatives and obtain new agreement.'); end if;
 -- Lock one eligible technician across all overlapping slots/job types/zones.
 -- SKIP LOCKED means another booking can choose a different technician.
 select t.* into tech from public.technicians t
 where public.technician_can_cover(t,slot,s.triage->>'level'='priority')
 order by (select count(*) from public.dispatch_jobs j where j.technician_id=t.id
   and j.inventory_scope=slot.inventory_scope and j.starts_at::date=slot.starts_at::date),t.code
 limit 1 for update of t skip locked;
 if not found then return jsonb_build_object('status','technician_unavailable','next_action','Search again and obtain fresh agreement.'); end if;
 insert into public.booking_agreements(proposal_id,call_id,intake_revision,caller_quote,decision) values(p.id,p_call_id,s.revision,p_caller_quote,p_decision);
 insert into public.appointments(call_id,proposal_id,slot_id,intake_snapshot,membership_snapshot,dispatch_status)
   values(p_call_id,p.id,slot.id,s.details,public.current_call_membership(p_call_id),'assigned') returning id into appointment_id;
 insert into public.dispatch_jobs(appointment_id,call_id,technician_id,inventory_scope,starts_at,ends_at,busy_until,priority)
   values(appointment_id,p_call_id,tech.id,slot.inventory_scope,slot.starts_at,slot.ends_at,slot.ends_at+interval '30 minutes',s.triage->>'level');
 update public.booking_proposals set status='booked' where id=p.id;
 return public.get_booking_status(p_call_id);
exception when exclusion_violation then
 return jsonb_build_object('status','technician_unavailable','next_action','Search again and obtain fresh agreement.');
end;
$$;

create or replace function public.get_booking_status(p_call_id text) returns jsonb
language sql security definer set search_path='' as $$
 select public.booking_status_record(p_call_id)||jsonb_build_object('membership',
   coalesce((select membership_snapshot from public.appointments where call_id=p_call_id),public.current_call_membership(p_call_id)),
   'dispatch',(select jsonb_build_object('job_id',j.id,'status',j.status,'technician_name',t.display_name,
      'technician_id',t.id,'synthetic_roster',t.is_synthetic,'priority',j.priority,'assigned_at',j.assigned_at)
      from public.dispatch_jobs j join public.technicians t on t.id=j.technician_id where j.call_id=p_call_id));
$$;
