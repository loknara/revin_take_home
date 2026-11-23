-- Prepared recipient -> caller confirmation -> durable outbox. No free-form message tool.
create table public.appointment_notifications (
 id uuid primary key default gen_random_uuid(),
 call_id text not null references public.call_intakes(call_id) on delete cascade,
 appointment_id uuid not null references public.appointments(id) on delete cascade,
 channel text not null check(channel in ('sms','email','none')),
 recipient text,
 confirmation_token uuid not null default gen_random_uuid(),
 preference_quote text not null, confirmation_quote text,
 status text not null default 'awaiting_confirmation' check(status in (
  'awaiting_confirmation','declined','queued','blocked_unconfigured','sending','submitted','delivered','failed','delivery_unknown','suppressed_test')),
 content jsonb not null, provider_id text, provider_name text, last_error text,
 lease_token uuid, lease_until timestamptz, next_check_at timestamptz,
 created_at timestamptz not null default clock_timestamp(),updated_at timestamptz not null default clock_timestamp(),
 unique(appointment_id,channel)
);
alter table public.appointment_notifications enable row level security;
revoke all on public.appointment_notifications from public,anon,authenticated,service_role;
grant select on public.appointment_notifications to service_role;

create function public.notification_summary(n public.appointment_notifications) returns jsonb
language sql immutable set search_path='' as $$
 select jsonb_build_object('status',n.status,'notification_id',n.id,'confirmation_token',n.confirmation_token,
    'channel',n.channel,'recipient',n.recipient);
$$;

create function public.prepare_confirmation(p_call_id text,p_channel text,p_caller_quote text,p_email text default null)
 returns jsonb language plpgsql security definer set search_path='' as $$
declare a public.appointments%rowtype; n public.appointment_notifications%rowtype; target text; payload jsonb;
begin
 if p_channel is null or p_channel not in ('sms','email','none') or nullif(trim(p_caller_quote),'') is null
  or length(p_caller_quote)>1000 then raise exception 'Invalid preference'; end if;
 select * into a from public.appointments where call_id=p_call_id for update;
 if not found then return '{"status":"not_booked"}'; end if;
 if p_channel='sms' then
  if a.intake_snapshot->>'callback_confirmed'<>'true' then return '{"status":"needs_confirmed_callback"}'; end if;
  target:=a.intake_snapshot->>'callback_number';
 elsif p_channel='email' then
  if p_email is null or length(p_email)>254 or p_email !~ '^[A-Za-z0-9.!#$%&''*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$' then
   return '{"status":"invalid_email"}'; end if;
  target:=p_email;
 end if;
 if p_channel='none' then
  update public.appointment_notifications set status='declined',updated_at=clock_timestamp()
   where appointment_id=a.id and status in ('awaiting_confirmation','queued','blocked_unconfigured');
 end if;
 select * into n from public.appointment_notifications where appointment_id=a.id and channel=p_channel for update;
 if found then
  if n.recipient is not distinct from target then return public.notification_summary(n); end if;
  if n.status not in ('awaiting_confirmation','declined') then return '{"status":"recipient_change_requires_review"}'; end if;
  update public.appointment_notifications set recipient=target,confirmation_token=gen_random_uuid(),preference_quote=p_caller_quote,
   confirmation_quote=null,status='awaiting_confirmation',updated_at=clock_timestamp() where id=n.id returning * into n;
 else
  -- Snapshot only the minimum confirmation details; no health/vulnerability notes.
  payload:=jsonb_build_object('reference',a.id,'job_type',a.intake_snapshot->>'service_type',
   'street',a.intake_snapshot->>'service_address','unit',a.intake_snapshot->>'service_unit',
   'city',a.intake_snapshot->>'service_city','state',a.intake_snapshot->>'service_state','zip',a.intake_snapshot->>'service_zip',
   'arrival_window',public.get_booking_status(p_call_id)->>'arrival_window',
   'technician_name',public.get_booking_status(p_call_id)->'dispatch'->>'technician_name',
   'synthetic_roster',coalesce(public.get_booking_status(p_call_id)->'dispatch'->'synthetic_roster','true'::jsonb));
  insert into public.appointment_notifications(call_id,appointment_id,channel,recipient,preference_quote,status,content)
   values(p_call_id,a.id,p_channel,target,p_caller_quote,case when p_channel='none' then 'declined' else 'awaiting_confirmation' end,payload)
   returning * into n;
 end if;
 return public.notification_summary(n);
end;
$$;

create function public.confirm_notification(p_call_id text,p_confirmation_token uuid,p_decision text,p_caller_quote text,p_sender_ready boolean default false)
 returns jsonb language plpgsql security definer set search_path='' as $$
declare n public.appointment_notifications%rowtype;
begin
 if p_decision is null or p_decision not in ('accepted','declined') or nullif(trim(p_caller_quote),'') is null or length(p_caller_quote)>1000 then raise exception 'Invalid confirmation'; end if;
 select * into n from public.appointment_notifications where call_id=p_call_id and confirmation_token=p_confirmation_token for update;
 if not found then return '{"status":"stale_confirmation"}'; end if;
 if n.status<>'awaiting_confirmation' then return public.notification_summary(n); end if;
 update public.appointment_notifications set confirmation_quote=p_caller_quote,
  status=case when p_decision='declined' then 'declined'
   when public.call_inventory_scope(p_call_id)<>'demo' or p_call_id like 'test_%' then 'suppressed_test'
   when p_sender_ready is not true then 'blocked_unconfigured' else 'queued' end,
  updated_at=clock_timestamp() where id=n.id returning * into n;
 return public.notification_summary(n);
end;
$$;

create function public.claim_appointment_notification() returns jsonb
language plpgsql security definer set search_path='' as $$
declare n public.appointment_notifications%rowtype; action text;
begin
 -- A lost send response must never cause another send. Polling has no side effect
 -- and can recover its lease normally when a provider ID was saved.
 update public.appointment_notifications set status='delivery_unknown',last_error='send_lease_expired',lease_token=null,lease_until=null
  where status='sending' and lease_until<clock_timestamp();
 select * into n from public.appointment_notifications
  where status='queued' or (status='submitted' and next_check_at<=clock_timestamp() and (lease_until is null or lease_until<clock_timestamp()))
  order by created_at limit 1 for update skip locked;
 if not found then return null; end if;
 if public.call_inventory_scope(n.call_id)<>'demo' or n.call_id like 'test_%' then
  update public.appointment_notifications set status='suppressed_test' where id=n.id;return null;
 end if;
 action:=case when n.status='queued' then 'send' else 'poll' end;
 update public.appointment_notifications set status=case when action='send' then 'sending' else status end,
  lease_token=gen_random_uuid(),lease_until=clock_timestamp()+interval '90 seconds',updated_at=clock_timestamp()
  where id=n.id returning * into n;
 return to_jsonb(n)||jsonb_build_object('action',action);
end;
$$;

create function public.finish_appointment_notification(p_id uuid,p_lease_token uuid,p_status text,
 p_provider_id text default null,p_provider_name text default null,p_error text default null) returns jsonb
language plpgsql security definer set search_path='' as $$
declare n public.appointment_notifications%rowtype;
begin
 if p_status is null or p_status not in ('blocked_unconfigured','submitted','delivered','failed','delivery_unknown') then raise exception 'Invalid delivery status'; end if;
 select * into n from public.appointment_notifications where id=p_id for update;
 if not found or n.lease_token is distinct from p_lease_token or n.lease_until<clock_timestamp() or n.status not in ('sending','submitted') then return '{"status":"lease_lost"}'; end if;
 if p_status in ('submitted','delivered') and coalesce(p_provider_id,n.provider_id) is null then raise exception 'Missing provider receipt'; end if;
 update public.appointment_notifications set status=case when p_status='submitted' and created_at<clock_timestamp()-interval '24 hours' then 'delivery_unknown' else p_status end,
  provider_id=coalesce(p_provider_id,provider_id),provider_name=coalesce(p_provider_name,provider_name),last_error=p_error,
  next_check_at=clock_timestamp()+interval '60 seconds',lease_token=null,lease_until=null,updated_at=clock_timestamp()
  where id=n.id returning * into n;
 return public.notification_summary(n);
end;
$$;

create function public.get_confirmation_status(p_call_id text) returns jsonb
language sql security definer set search_path='' as $$
 select jsonb_build_object('status','current','notifications',coalesce(jsonb_agg(public.notification_summary(n) order by n.created_at),'[]'))
 from public.appointment_notifications n where n.call_id=p_call_id;
$$;
revoke all on function public.notification_summary(public.appointment_notifications),public.prepare_confirmation(text,text,text,text),
 public.confirm_notification(text,uuid,text,text,boolean),public.claim_appointment_notification(),
 public.finish_appointment_notification(uuid,uuid,text,text,text,text),public.get_confirmation_status(text)
 from public,anon,authenticated,service_role;
grant execute on function public.prepare_confirmation(text,text,text,text),public.confirm_notification(text,uuid,text,text,boolean),
 public.claim_appointment_notification(),public.finish_appointment_notification(uuid,uuid,text,text,text,text),public.get_confirmation_status(text) to service_role;
