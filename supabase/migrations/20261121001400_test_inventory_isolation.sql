-- Independent inventories for phone demos, model simulations, and each DB fixture.
-- Scope is derived from trusted server call identity, never model arguments.
alter table public.appointment_slots add column inventory_scope text not null default 'demo';
alter table public.appointment_slots drop constraint appointment_slots_zone_service_type_starts_at_key;
alter table public.appointment_slots add unique(zone,service_type,starts_at,inventory_scope);
create function public.call_inventory_scope(p_call_id text) returns text
language sql immutable set search_path='' as $$
 select case when p_call_id like 'eval_%' then 'evaluation'
 when p_call_id like 'test_workflow_%' then 'test_workflow_'||split_part(p_call_id,'_',3)
 else 'demo' end;
$$;
drop function public.seed_demo_capacity();
create or replace function public.seed_demo_capacity(p_scope text) returns void
language sql security definer set search_path='' as $$
 insert into public.appointment_slots(zone,service_type,starts_at,ends_at,capacity,inventory_scope)
 select z.zone,t.kind,((current_timestamp at time zone 'America/New_York')::date+n.day+h.hour*interval '1 hour') at time zone 'America/New_York',
 ((current_timestamp at time zone 'America/New_York')::date+n.day+(h.hour+3)*interval '1 hour') at time zone 'America/New_York',2,p_scope
 from (select distinct zone from public.demo_service_areas where enabled) z
 cross join (values('repair'),('maintenance')) t(kind)
 cross join generate_series(1,14) n(day) cross join (values(9),(13)) h(hour)
 where extract(isodow from (current_timestamp at time zone 'America/New_York')::date+n.day)<>7
 on conflict(zone,service_type,starts_at,inventory_scope) do nothing;
$$;

create or replace function public.search_slots(p_call_id text,p_expected_revision integer) returns jsonb
language plpgsql security definer set search_path='' as $$
declare s public.call_intakes%rowtype; r jsonb; choices jsonb; search public.availability_reads%rowtype;
begin
 select * into s from public.call_intakes where call_id=p_call_id for update;
 if not found then return jsonb_build_object('status','needs_intake'); end if;
 if s.revision<>p_expected_revision then return jsonb_build_object('status','revision_conflict','current_revision',s.revision); end if;
 r:=public.intake_readiness(s.details);
 if r->>'scope'<>'eligible' or r->>'coverage'<>'covered' or coalesce(s.details->>'address_confirmed','false')<>'true'
    or s.details->>'service_type' not in ('repair','maintenance') or s.triage->>'level'<>'routine' then
   return jsonb_build_object('status','not_eligible','readiness',r); end if;
 if public.call_inventory_scope(p_call_id) in ('demo','evaluation') then perform public.seed_demo_capacity(public.call_inventory_scope(p_call_id)); end if;
 select coalesce(jsonb_agg(option order by option->>'starts_at'),'[]') into choices from (
   select jsonb_build_object('slot_id',slot.id,'starts_at',slot.starts_at,'ends_at',slot.ends_at,
    'local_date',to_char(slot.starts_at at time zone 'America/New_York','YYYY-MM-DD'),
    'arrival_window',to_char(slot.starts_at at time zone 'America/New_York','FMDay, Month FMDD, YYYY FMHH12:MI AM')||' to '||to_char(slot.ends_at at time zone 'America/New_York','FMHH12:MI AM'),
    'timezone','America/New_York') as option
   from public.appointment_slots slot where slot.inventory_scope=public.call_inventory_scope(p_call_id) and slot.enabled and slot.zone=r->>'zone' and slot.service_type=s.details->>'service_type'
     and slot.starts_at>clock_timestamp()+interval '2 hours' and slot.starts_at<clock_timestamp()+interval '15 days'
     and (s.details->>'preferred_date' is null or (slot.starts_at at time zone 'America/New_York')::date=(s.details->>'preferred_date')::date)
     and (select count(*) from public.appointments a where a.slot_id=slot.id)<slot.capacity
   order by slot.starts_at limit 3
 ) options;
 insert into public.availability_reads(call_id,dependency_snapshot,result)
   values(p_call_id,public.availability_dependencies(s.details),choices) returning * into search;
 return jsonb_build_object('status',case when choices='[]' then 'no_availability' else 'available' end,
   'search_id',search.id,'slots',choices,'expires_at',search.expires_at,'demo_only',true);
end;
$$;

create or replace function public.confirm_booking(p_call_id text,p_expected_revision integer,p_proposal_id uuid,p_decision text,p_caller_quote text) returns jsonb
language plpgsql security definer set search_path='' as $$
declare s public.call_intakes%rowtype; p public.booking_proposals%rowtype; slot public.appointment_slots%rowtype; readiness jsonb;
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
 if slot.inventory_scope<>public.call_inventory_scope(p_call_id) or not slot.enabled or slot.starts_at<=clock_timestamp()+interval '2 hours' or slot.zone<>readiness->>'zone' or slot.service_type<>s.details->>'service_type' then
   return jsonb_build_object('status','slot_unavailable'); end if;
 if (select count(*) from public.appointments where slot_id=slot.id)>=slot.capacity then
   update public.booking_proposals set status='invalidated' where id=p.id;
   return jsonb_build_object('status','slot_conflict','next_action','Search for fresh alternatives and obtain new agreement.'); end if;
 insert into public.booking_agreements(proposal_id,call_id,intake_revision,caller_quote,decision) values(p.id,p_call_id,s.revision,p_caller_quote,p_decision);
 insert into public.appointments(call_id,proposal_id,slot_id,intake_snapshot,membership_snapshot)
   values(p_call_id,p.id,slot.id,s.details,public.current_call_membership(p_call_id));
 update public.booking_proposals set status='booked' where id=p.id;
 return public.get_booking_status(p_call_id);
end;
$$;


revoke all on function public.call_inventory_scope(text),public.seed_demo_capacity(text) from public,anon,authenticated,service_role;
