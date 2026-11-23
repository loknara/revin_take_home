-- Dispatcher alerts: "flagged as priority" must reach a person, not just a table.
-- Alerts are queued by triggers in the SAME transaction that records the priority case or
-- callback request, so a crash can't lose one. A backend worker pushes them (ntfy) and
-- records the outcome. Alert text carries no caller name, address or phone number: only
-- the reason, county and a call reference the dispatcher looks up in the ops database.

create table public.dispatcher_alerts (
 id uuid primary key default gen_random_uuid(),
 call_id text not null references public.call_intakes(call_id) on delete cascade,
 kind text not null check (kind in ('emergency','urgent','elevated','callback_requested')),
 title text not null,
 body text not null,
 status text not null default 'queued'
   check (status in ('queued','sending','sent','failed','suppressed_test','blocked_unconfigured')),
 attempts integer not null default 0,
 lease_token uuid,
 leased_until timestamptz,
 error text,
 created_at timestamptz not null default clock_timestamp(),
 sent_at timestamptz,
 unique (call_id, kind)
);
alter table public.dispatcher_alerts enable row level security;
revoke all on public.dispatcher_alerts from public,anon,authenticated,service_role;
grant select on public.dispatcher_alerts to service_role;

create function public.alert_reason_label(r text) returns text
language sql immutable set search_path='' as $$
 select case r
  when 'gas_smell' then 'gas smell reported'
  when 'carbon_monoxide_alarm' then 'carbon monoxide alarm'
  when 'smoke_or_burning_smell' then 'smoke or burning smell'
  when 'electrical_sparking' then 'electrical sparking'
  when 'no_heat_vulnerable' then 'no heat, vulnerable person at home'
  when 'no_cooling_vulnerable' then 'no AC, vulnerable person at home'
  when 'cannot_safely_wait' then 'caller says they cannot safely wait'
  when 'no_heat_cold' then 'no heat, caller reports cold indoors'
  when 'no_heat_cold_weather' then 'no heat during cold weather'
  when 'no_cooling_hot_weather' then 'no AC during hot weather'
  else replace(r,'_',' ') end;
$$;
revoke all on function public.alert_reason_label(text) from public,anon,authenticated,service_role;

create function public.queue_dispatcher_alert(p_call_id text, p_kind text) returns void
language plpgsql security definer set search_path='' as $$
declare d jsonb; t jsonb; county text; reasons text; title text; body text;
begin
 select details, triage into d, t from public.call_intakes where call_id=p_call_id;
 select a.county into county from public.demo_service_areas a where a.postal_code=d->>'service_zip';
 select string_agg(public.alert_reason_label(value),'; ') into reasons from jsonb_array_elements_text(t->'reasons');
 title := case p_kind
   when 'emergency' then 'EMERGENCY: '||coalesce(reasons,'safety hazard')
   when 'urgent' then 'URGENT priority: '||coalesce(reasons,'priority repair')
   when 'elevated' then 'Priority: '||coalesce(reasons,'priority repair')
   else 'Callback requested' end;
 body := concat_ws(' | ',
   case when county is not null then county||' County' else 'Location not collected yet' end,
   case when p_kind='emergency' then 'Caller told to evacuate and call 911; no booking made'
        when p_kind='callback_requested' then 'Caller asked to speak with a person'
        else 'Service: '||coalesce(d->>'service_type','unknown') end,
   'Call ref '||left(p_call_id,8));
 insert into public.dispatcher_alerts(call_id,kind,title,body) values(p_call_id,p_kind,title,body)
 on conflict (call_id,kind) do nothing;
end;
$$;
revoke all on function public.queue_dispatcher_alert(text,text) from public,anon,authenticated,service_role;

-- New priority case, or an escalation to a higher tier, queues one alert per tier.
create function public.alert_on_priority() returns trigger
language plpgsql security definer set search_path='' as $$
begin
 if new.priority_tier is not null and (tg_op='INSERT'
    or public.priority_tier_rank(new.priority_tier) > public.priority_tier_rank(old.priority_tier)) then
   perform public.queue_dispatcher_alert(new.call_id,new.priority_tier);
 end if;
 return new;
end;
$$;
revoke all on function public.alert_on_priority() from public,anon,authenticated,service_role;
create trigger alert_dispatcher_on_priority after insert or update of priority_tier
 on public.priority_requests for each row execute function public.alert_on_priority();

create function public.alert_on_callback_request() returns trigger
language plpgsql security definer set search_path='' as $$
begin
 if new.reasons ? 'caller_requested' then
   perform public.queue_dispatcher_alert(new.call_id,'callback_requested');
 end if;
 return new;
end;
$$;
revoke all on function public.alert_on_callback_request() from public,anon,authenticated,service_role;
create trigger alert_dispatcher_on_callback after insert or update of reasons
 on public.followup_requests for each row execute function public.alert_on_callback_request();

-- Worker lease: one sender per alert; an expired lease (worker died mid-send) is
-- reclaimable. A timed-out push may be retried, so a rare duplicate alert is possible:
-- for a dispatcher page, a duplicate is preferable to a missed alert. Max 3 attempts.
create function public.claim_dispatcher_alert() returns jsonb
language plpgsql security definer set search_path='' as $$
declare a public.dispatcher_alerts%rowtype;
begin
 select * into a from public.dispatcher_alerts
  where (status='queued' or (status='sending' and leased_until<clock_timestamp())) and attempts<3
  order by public.priority_tier_rank(kind) desc, created_at
  for update skip locked limit 1;
 if not found then return null; end if;
 update public.dispatcher_alerts set status='sending', attempts=attempts+1,
   lease_token=gen_random_uuid(), leased_until=clock_timestamp()+interval '1 minute'
  where id=a.id returning * into a;
 return jsonb_build_object('id',a.id,'call_id',a.call_id,'kind',a.kind,'title',a.title,
   'body',a.body,'lease_token',a.lease_token,'attempts',a.attempts);
end;
$$;

create function public.finish_dispatcher_alert(p_id uuid,p_lease_token uuid,p_status text,p_error text default null) returns boolean
language plpgsql security definer set search_path='' as $$
begin
 if p_status not in ('sent','failed','queued','suppressed_test','blocked_unconfigured') then
   raise exception 'Invalid alert status';
 end if;
 update public.dispatcher_alerts set
   status=case when p_status='queued' and attempts>=3 then 'failed' else p_status end,
   error=p_error, sent_at=case when p_status='sent' then clock_timestamp() end,
   lease_token=null, leased_until=null
  where id=p_id and lease_token=p_lease_token and status='sending';
 return found;
end;
$$;
revoke all on function public.claim_dispatcher_alert(),public.finish_dispatcher_alert(uuid,uuid,text,text)
 from public,anon,authenticated,service_role;
grant execute on function public.claim_dispatcher_alert(),public.finish_dispatcher_alert(uuid,uuid,text,text)
 to service_role;

create function public.dispatcher_alert_status(p_call_id text, p_kind text) returns text
language sql stable security definer set search_path='' as $$
 select status from public.dispatcher_alerts where call_id=p_call_id and kind=p_kind;
$$;
revoke all on function public.dispatcher_alert_status(text,text) from public,anon,authenticated,service_role;

create or replace function public.priority_request_summary(p_call_id text) returns jsonb
language sql volatile security definer set search_path = '' as $$
    select jsonb_build_object('id', id, 'highest_level', highest_level, 'priority_tier', priority_tier,
                             'current_level', current_level, 'needs_review', needs_review,
                             'status', status, 'notification_status', notification_status,
                             'dispatcher_alert', public.dispatcher_alert_status(p_call_id, priority_tier))
    from public.priority_requests where call_id = p_call_id;
$$;

-- Report the alert alongside a recorded callback request, and stop claiming nobody was told.
alter function public.request_followup(text,integer,text,text) rename to request_followup_before_alerts;
create function public.request_followup(p_call_id text,p_expected_revision integer,p_reason text,p_caller_quote text) returns jsonb
language plpgsql security definer set search_path='' as $$
declare r jsonb := public.request_followup_before_alerts(p_call_id,p_expected_revision,p_reason,p_caller_quote);
 alert text;
begin
 if r->>'status'='recorded' and p_reason='caller_requested' then
   alert := public.dispatcher_alert_status(p_call_id,'callback_requested');
   r := r || jsonb_build_object('dispatcher_alert',alert,
     'message','Callback request recorded and a dispatcher alert was queued. No callback time is guaranteed.');
 end if;
 return r;
end;
$$;
revoke all on function public.request_followup_before_alerts(text,integer,text,text),public.request_followup(text,integer,text,text)
 from public,anon,authenticated,service_role;
grant execute on function public.request_followup(text,integer,text,text) to service_role;
