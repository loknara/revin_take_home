-- Self-service rescheduling and cancelling (live-call feedback, 2026-11-23).
--
-- A caller who calls back from the number they booked with can hear their upcoming visit,
-- move it, or cancel it. Scope, deliberately narrow:
--  * Only visits this agent booked, found by caller ID (same inventory scope as the call:
--    test sessions only ever see test bookings). No caller ID means no lookup.
--  * Moving reuses the normal flow: the visit's confirmed details are copied into this call,
--    the agent searches and offers as usual, and the old visit is released in the same
--    transaction that books the new one, so a failed booking never loses the old visit.
--  * Cancelling needs the caller's explicit words.
-- A changed visit leaves `appointments` (slot capacity and technician schedules count those
-- rows) and is kept in `appointment_history` for the office.

create table public.appointment_history (
 appointment_id uuid primary key,
 original_call_id text not null,
 slot_id uuid not null,
 starts_at timestamptz not null,
 ends_at timestamptz not null,
 technician_name text,
 intake_snapshot jsonb not null,
 action text not null check (action in ('rescheduled','cancelled')),
 changed_by_call text not null,
 replaced_by uuid,
 caller_quote text not null,
 changed_at timestamptz not null default clock_timestamp()
);
alter table public.appointment_history enable row level security;
revoke all on public.appointment_history from public,anon,authenticated,service_role;
grant select on public.appointment_history to service_role;

create table public.appointment_changes (
 call_id text primary key,
 appointment_id uuid not null,
 caller_quote text not null,
 status text not null default 'pending' check (status in ('pending','completed')),
 created_at timestamptz not null default clock_timestamp(),
 completed_at timestamptz
);
alter table public.appointment_changes enable row level security;
revoke all on public.appointment_changes from public,anon,authenticated,service_role;
grant select on public.appointment_changes to service_role;

-- Upcoming visits booked from this number (most recent first, at most two).
create function public.caller_upcoming_appointments(p_call_id text, p_originating_number text)
returns jsonb language sql stable security definer set search_path='' as $$
 select coalesce(jsonb_agg(x order by x->>'booked_at' desc),'[]'::jsonb) from (
  select jsonb_build_object(
    'appointment_id',a.id,'starts_at',slot.starts_at,'ends_at',slot.ends_at,
    'service_address',a.intake_snapshot->>'service_address','service_unit',a.intake_snapshot->>'service_unit',
    'service_type',a.intake_snapshot->>'service_type','caller_name',a.intake_snapshot->>'caller_name',
    'technician_name',t.display_name,'status',a.status,'booked_at',a.created_at) x
  from public.appointments a
  join public.call_intakes c on c.call_id=a.call_id
  join public.appointment_slots slot on slot.id=a.slot_id
  left join public.dispatch_jobs j on j.appointment_id=a.id
  left join public.technicians t on t.id=j.technician_id
  where p_originating_number is not null and c.originating_number=p_originating_number
    and a.call_id<>p_call_id and slot.ends_at>clock_timestamp()
    and slot.inventory_scope=public.call_inventory_scope(p_call_id)
  order by a.created_at desc limit 2) found;
$$;
revoke all on function public.caller_upcoming_appointments(text,text) from public,anon,authenticated,service_role;

create function public.find_my_appointment(p_call_id text, p_originating_number text)
returns jsonb language plpgsql stable security definer set search_path='' as $$
declare found jsonb;
begin
 if p_originating_number is null then
   return jsonb_build_object('status','no_caller_id');
 end if;
 found := public.caller_upcoming_appointments(p_call_id,p_originating_number);
 if jsonb_array_length(found)=0 then return jsonb_build_object('status','none_found'); end if;
 return jsonb_build_object('status','found','appointments',found);
end;
$$;
revoke all on function public.find_my_appointment(text,text) from public,anon,authenticated;
grant execute on function public.find_my_appointment(text,text) to service_role;

-- Move a visit out of `appointments` into history (frees its slot and technician).
create function public.retire_appointment(p_appointment_id uuid, p_action text, p_call_id text,
                                          p_quote text, p_replaced_by uuid default null)
returns jsonb language plpgsql security definer set search_path='' as $$
declare a public.appointments%rowtype; slot public.appointment_slots%rowtype; tech text;
begin
 select * into a from public.appointments where id=p_appointment_id for update;
 if not found then return null; end if;
 select * into slot from public.appointment_slots where id=a.slot_id;
 select t.display_name into tech from public.dispatch_jobs j join public.technicians t on t.id=j.technician_id
  where j.appointment_id=a.id;
 insert into public.appointment_history(appointment_id,original_call_id,slot_id,starts_at,ends_at,technician_name,
   intake_snapshot,action,changed_by_call,replaced_by,caller_quote)
 values(a.id,a.call_id,a.slot_id,slot.starts_at,slot.ends_at,tech,a.intake_snapshot,p_action,p_call_id,p_replaced_by,p_quote);
 delete from public.appointments where id=a.id;  -- cascades its dispatch job
 return jsonb_build_object('starts_at',slot.starts_at,'ends_at',slot.ends_at,
   'service_address',a.intake_snapshot->>'service_address');
end;
$$;
revoke all on function public.retire_appointment(uuid,text,text,text,uuid) from public,anon,authenticated,service_role;

create function public.change_appointment(
 p_call_id text, p_operation_id text, p_originating_number text, p_expected_revision integer,
 p_appointment_id uuid, p_action text, p_caller_quote text
) returns jsonb language plpgsql security definer set search_path='' as $$
declare mine jsonb; d jsonb; changes jsonb := '{}'::jsonb; k text; r jsonb; old jsonb;
 copied text[] := array['caller_name','property_type','issue_summary','service_address','service_unit','service_zip',
   'service_city','service_state','service_type','service_category','heating_unavailable','cooling_unavailable',
   'cold_conditions_reported','vulnerable_resident','unsafe_to_wait','membership_claim','callback_number',
   'equipment_notes','access_notes','onsite_contact'];
begin
 if p_action not in ('reschedule','cancel') then
   return jsonb_build_object('status','invalid_arguments','errors',jsonb_build_array(
     jsonb_build_object('field',jsonb_build_array('action'),'message','action must be reschedule or cancel')));
 end if;
 select x into mine from jsonb_array_elements(public.caller_upcoming_appointments(p_call_id,p_originating_number)) x
  where x->>'appointment_id'=p_appointment_id::text;
 if mine is null then return jsonb_build_object('status','not_found'); end if;
 if p_action='cancel' then
   old := public.retire_appointment(p_appointment_id,'cancelled',p_call_id,p_caller_quote);
   return jsonb_build_object('status','cancelled','cancelled',old);
 end if;
 -- Reschedule: bring the visit's confirmed details into this call, then book as usual.
 select a.intake_snapshot into d from public.appointments a where a.id=p_appointment_id;
 foreach k in array copied loop
   if d ? k and jsonb_typeof(d->k)<>'null' then changes := changes||jsonb_build_object(k,d->k); end if;
 end loop;
 -- Two saves, like a live call: the details, then their confirmation (the caller just
 -- confirmed this visit's address and number when the agent read the visit back).
 r := public.update_call_intake_before_guard_v2(p_call_id,p_operation_id||':details',p_originating_number,
        p_expected_revision,changes,p_caller_quote,null,null);
 if r->>'status'<>'saved' then return r; end if;
 r := public.update_call_intake_before_guard_v2(p_call_id,p_operation_id||':confirm',p_originating_number,
        (r->'state'->>'revision')::integer,'{"address_confirmed":true,"callback_confirmed":true}'::jsonb,
        p_caller_quote,d->>'service_address',d->>'callback_number');
 if r->>'status'<>'saved' then return r; end if;
 insert into public.appointment_changes(call_id,appointment_id,caller_quote) values(p_call_id,p_appointment_id,p_caller_quote)
 on conflict(call_id) do update set appointment_id=excluded.appointment_id,caller_quote=excluded.caller_quote,
   status='pending',completed_at=null;
 return r||jsonb_build_object('rescheduling',mine);
end;
$$;
revoke all on function public.change_appointment(text,text,text,integer,uuid,text,text) from public,anon,authenticated;
grant execute on function public.change_appointment(text,text,text,integer,uuid,text,text) to service_role;

-- Booking the new window releases the old one in the same transaction.
alter function public.confirm_booking(text,integer,uuid,text,text) rename to confirm_booking_before_reschedule;
revoke all on function public.confirm_booking_before_reschedule(text,integer,uuid,text,text) from public,anon,authenticated,service_role;

create function public.confirm_booking(p_call_id text, p_expected_revision integer, p_proposal_id uuid,
                                       p_decision text, p_caller_quote text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare r jsonb; ch public.appointment_changes%rowtype; old jsonb;
begin
 r := public.confirm_booking_before_reschedule(p_call_id,p_expected_revision,p_proposal_id,p_decision,p_caller_quote);
 if r->>'status'='booked' then
   select * into ch from public.appointment_changes where call_id=p_call_id and status='pending' for update;
   if found then
     old := public.retire_appointment(ch.appointment_id,'rescheduled',p_call_id,ch.caller_quote,(r->>'reference')::uuid);
     update public.appointment_changes set status='completed',completed_at=clock_timestamp() where call_id=p_call_id;
     if old is not null then r := r||jsonb_build_object('replaced',old); end if;
   end if;
 end if;
 return r;
end;
$$;
revoke all on function public.confirm_booking(text,integer,uuid,text,text) from public,anon,authenticated;
grant execute on function public.confirm_booking(text,integer,uuid,text,text) to service_role;
