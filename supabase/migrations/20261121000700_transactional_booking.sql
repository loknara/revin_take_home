-- Synthetic appointment capacity, not a real dispatch/CRM integration.
alter table public.call_intakes add column material_revision integer not null default 0;
create function public.booking_snapshot(d jsonb) returns jsonb
language sql immutable set search_path='' as $$
 select d - array['access_notes','equipment_notes','issue_duration','onsite_contact','confirmation_quote','callback_confirmation_quote','membership_claim'];
$$;
create function public.advance_material_revision() returns trigger
language plpgsql set search_path='' as $$
begin
 if public.booking_snapshot(new.details) is distinct from public.booking_snapshot(old.details) then new.material_revision:=old.material_revision+1; end if;
 return new;
end;
$$;
create trigger material_revision before update of details on public.call_intakes for each row execute function public.advance_material_revision();

create table public.appointment_slots (
 id uuid primary key default gen_random_uuid(), zone text not null,
 service_type text not null check(service_type in ('repair','maintenance')),
 starts_at timestamptz not null, ends_at timestamptz not null,
 capacity integer not null check(capacity between 1 and 40), enabled boolean not null default true,
 check(ends_at>starts_at), unique(zone,service_type,starts_at)
);
create table public.availability_reads (
 id uuid primary key default gen_random_uuid(), call_id text not null references public.call_intakes(call_id) on delete cascade,
 dependency_snapshot jsonb not null, result jsonb not null,
 created_at timestamptz not null default clock_timestamp(), expires_at timestamptz not null default clock_timestamp()+interval '2 minutes'
);
create table public.booking_proposals (
 id uuid primary key default gen_random_uuid(), call_id text not null references public.call_intakes(call_id) on delete cascade,
 slot_id uuid not null references public.appointment_slots(id), search_id uuid not null references public.availability_reads(id) on delete cascade,
 material_revision integer not null, intake_snapshot jsonb not null,
 status text not null default 'offered' check(status in ('offered','declined','invalidated','booked')),
 created_at timestamptz not null default clock_timestamp(), expires_at timestamptz not null default clock_timestamp()+interval '2 minutes'
);
create table public.booking_agreements (
 proposal_id uuid primary key references public.booking_proposals(id) on delete cascade,
 call_id text not null references public.call_intakes(call_id) on delete cascade,
 intake_revision integer not null, caller_quote text not null check(length(trim(caller_quote)) between 1 and 1000),
 decision text not null check(decision in ('accepted','declined')),
 source text not null default 'assistant_reported', created_at timestamptz not null default clock_timestamp()
);
create table public.appointments (
 id uuid primary key default gen_random_uuid(), call_id text not null unique references public.call_intakes(call_id) on delete cascade,
 proposal_id uuid not null unique references public.booking_proposals(id), slot_id uuid not null references public.appointment_slots(id),
 intake_snapshot jsonb not null, membership_snapshot jsonb not null,
 status text not null default 'booked' check(status='booked'),
 dispatch_status text not null default 'demo_only' check(dispatch_status='demo_only'),
 created_at timestamptz not null default clock_timestamp()
);

create function public.availability_dependencies(d jsonb) returns jsonb
language sql immutable set search_path='' as $$
 select jsonb_build_object('street',d->'service_address','unit',d->'service_unit','zip',d->'service_zip',
 'city',d->'service_city','state',d->'service_state','confirmed',d->'address_confirmed',
 'property',d->'property_type','category',d->'service_category','type',d->'service_type','date',d->'preferred_date',
 'risk',public.assess_intake_risk(d));
$$;

create function public.seed_demo_capacity() returns void
language sql security definer set search_path='' as $$
 insert into public.appointment_slots(zone,service_type,starts_at,ends_at,capacity)
 select z.zone,t.kind,((current_timestamp at time zone 'America/New_York')::date+n.day+h.hour*interval '1 hour') at time zone 'America/New_York',
 ((current_timestamp at time zone 'America/New_York')::date+n.day+(h.hour+3)*interval '1 hour') at time zone 'America/New_York',2
 from (select distinct zone from public.demo_service_areas where enabled) z
 cross join (values('repair'),('maintenance')) t(kind)
 cross join generate_series(1,14) n(day) cross join (values(9),(13)) h(hour)
 where extract(isodow from (current_timestamp at time zone 'America/New_York')::date+n.day)<>7
 on conflict(zone,service_type,starts_at) do nothing;
$$;

create function public.search_slots(p_call_id text,p_expected_revision integer) returns jsonb
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
 perform public.seed_demo_capacity();
 select coalesce(jsonb_agg(option order by option->>'starts_at'),'[]') into choices from (
   select jsonb_build_object('slot_id',slot.id,'starts_at',slot.starts_at,'ends_at',slot.ends_at,
    'local_date',to_char(slot.starts_at at time zone 'America/New_York','YYYY-MM-DD'),
    'arrival_window',to_char(slot.starts_at at time zone 'America/New_York','FMDay, Month FMDD, YYYY FMHH12:MI AM')||' to '||to_char(slot.ends_at at time zone 'America/New_York','FMHH12:MI AM'),
    'timezone','America/New_York') as option
   from public.appointment_slots slot where slot.enabled and slot.zone=r->>'zone' and slot.service_type=s.details->>'service_type'
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

create function public.propose_booking(p_call_id text,p_expected_revision integer,p_search_id uuid,p_slot_id uuid) returns jsonb
language plpgsql security definer set search_path='' as $$
declare s public.call_intakes%rowtype; search public.availability_reads%rowtype; p public.booking_proposals%rowtype; choice jsonb; readiness jsonb;
begin
 select * into s from public.call_intakes where call_id=p_call_id for update;
 if not found then return jsonb_build_object('status','needs_intake'); end if;
 if exists(select 1 from public.appointments where call_id=p_call_id) then return jsonb_build_object('status','already_booked','next_action','get_booking_status'); end if;
 if s.revision<>p_expected_revision then return jsonb_build_object('status','revision_conflict','current_revision',s.revision); end if;
 readiness:=public.intake_readiness(s.details);
 if readiness->>'can_book'<>'true' then return jsonb_build_object('status','not_eligible','readiness',readiness); end if;
 select * into search from public.availability_reads where id=p_search_id and call_id=p_call_id;
 if not found or search.expires_at<=clock_timestamp() or search.dependency_snapshot<>public.availability_dependencies(s.details) then
   return jsonb_build_object('status','stale_availability'); end if;
 select value into choice from jsonb_array_elements(search.result) where value->>'slot_id'=p_slot_id::text;
 if choice is null then return jsonb_build_object('status','invalid_slot'); end if;
 -- Reuse an identical live offer. Invalidate alternatives so an old yes cannot book one.
 select * into p from public.booking_proposals where call_id=p_call_id and slot_id=p_slot_id and search_id=p_search_id
   and material_revision=s.material_revision and status='offered' and expires_at>clock_timestamp() order by created_at desc limit 1;
 if not found then
   update public.booking_proposals set status='invalidated' where call_id=p_call_id and status='offered';
   insert into public.booking_proposals(call_id,slot_id,search_id,material_revision,intake_snapshot)
     values(p_call_id,p_slot_id,p_search_id,s.material_revision,s.details) returning * into p;
 end if;
 return jsonb_build_object('status','offered','proposal_id',p.id,'expires_at',p.expires_at,
   'arrival',choice,'details',p.intake_snapshot,'demo_only',true,
   'next_action','Read this window in New York time, address and job summary. Ask for explicit agreement and END YOUR TURN. This is not booked.');
end;
$$;

create function public.get_booking_status(p_call_id text) returns jsonb
language plpgsql security definer set search_path='' as $$
declare a public.appointments%rowtype; slot public.appointment_slots%rowtype; d jsonb;
begin
 select * into a from public.appointments where call_id=p_call_id;
 if not found then return jsonb_build_object('status','not_booked'); end if;
 select * into slot from public.appointment_slots where id=a.slot_id;
 select details into d from public.call_intakes where call_id=p_call_id;
 return jsonb_build_object('status','booked','reference',a.id,'proposal_id',a.proposal_id,'slot_id',a.slot_id,
   'starts_at',slot.starts_at,'ends_at',slot.ends_at,'timezone','America/New_York',
   'arrival_window',to_char(slot.starts_at at time zone 'America/New_York','FMDay, Month FMDD, YYYY FMHH12:MI AM')||' to '||to_char(slot.ends_at at time zone 'America/New_York','FMHH12:MI AM'),
   'details',a.intake_snapshot,'dispatch_status',a.dispatch_status,'demo_only',true,
   'change_requires_followup',public.booking_snapshot(d)<>public.booking_snapshot(a.intake_snapshot));
end;
$$;

create function public.confirm_booking(p_call_id text,p_expected_revision integer,p_proposal_id uuid,p_decision text,p_caller_quote text) returns jsonb
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
 if not slot.enabled or slot.starts_at<=clock_timestamp()+interval '2 hours' or slot.zone<>readiness->>'zone' or slot.service_type<>s.details->>'service_type' then
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

-- RPCs only; public clients cannot create or tamper with reservations/capacity.
do $$ declare t text; begin
 foreach t in array array['appointment_slots','availability_reads','booking_proposals','booking_agreements','appointments'] loop
   execute format('alter table public.%I enable row level security',t);
   execute format('revoke all on public.%I from public,anon,authenticated,service_role',t);
   execute format('grant select on public.%I to service_role',t);
 end loop;
end $$;
revoke all on function public.booking_snapshot(jsonb),public.advance_material_revision(),public.availability_dependencies(jsonb),public.seed_demo_capacity(),
 public.search_slots(text,integer),public.propose_booking(text,integer,uuid,uuid),public.get_booking_status(text),public.confirm_booking(text,integer,uuid,text,text)
 from public,anon,authenticated,service_role;
grant execute on function public.search_slots(text,integer),public.propose_booking(text,integer,uuid,uuid),public.get_booking_status(text),public.confirm_booking(text,integer,uuid,text,text) to service_role;
