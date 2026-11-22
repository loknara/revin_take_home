create table public.followup_requests (
 id uuid primary key default gen_random_uuid(),call_id text not null unique references public.call_intakes(call_id) on delete cascade,
 reasons jsonb not null,details jsonb not null,caller_quote text not null,
 status text not null default 'recorded' check(status='recorded'),
 notification_status text not null default 'not_connected' check(notification_status='not_connected'),
 created_at timestamptz not null default clock_timestamp(),updated_at timestamptz not null default clock_timestamp()
);
create function public.request_followup(p_call_id text,p_expected_revision integer,p_reason text,p_caller_quote text) returns jsonb
language plpgsql security definer set search_path='' as $$
declare s public.call_intakes%rowtype; f public.followup_requests%rowtype;
begin
 if p_reason is null or p_reason not in ('no_availability','booking_problem','priority','membership_verification','incomplete_intake','booking_change','caller_requested')
 or p_caller_quote is null or length(trim(p_caller_quote)) not between 1 and 1000 then raise exception 'Invalid follow-up'; end if;
 select * into s from public.call_intakes where call_id=p_call_id for update;
 if not found then return jsonb_build_object('status','needs_intake'); end if;
 if s.revision<>p_expected_revision then return jsonb_build_object('status','revision_conflict','current_revision',s.revision); end if;
 if s.triage->>'level'='emergency' then return jsonb_build_object('status','emergency_guidance_required','priority_request',public.priority_request_summary(p_call_id)); end if;
 insert into public.followup_requests(call_id,reasons,details,caller_quote) values(p_call_id,jsonb_build_array(p_reason),s.details,p_caller_quote)
 on conflict(call_id) do update set
  reasons=case when public.followup_requests.reasons ? p_reason then public.followup_requests.reasons else public.followup_requests.reasons||to_jsonb(p_reason) end,
  details=excluded.details,caller_quote=excluded.caller_quote,updated_at=clock_timestamp() returning * into f;
 return jsonb_build_object('status','recorded','reference',f.id,'notification_status',f.notification_status,
 'callback_confirmed',coalesce(s.details->>'callback_confirmed','false')='true',
 'message','A demo follow-up record exists. No one has been notified; no callback or arrival is guaranteed.');
end;
$$;

create table public.call_audit (
 call_id text primary key, config jsonb not null default '{}',started_at timestamptz not null default clock_timestamp(),
 ended_at timestamptz,ended_reason text,final_disposition jsonb
);
create table public.tool_audit (
 id bigint generated always as identity primary key,call_id text not null,operation_id text not null,tool_name text not null,
 result_status text not null,elapsed_ms integer not null check(elapsed_ms>=0),config_version text not null,
 created_at timestamptz not null default clock_timestamp()
);
create index tool_audit_call on public.tool_audit(call_id,created_at);
create function public.call_disposition(p_call_id text) returns jsonb
language plpgsql stable security definer set search_path='' as $$
declare s public.call_intakes%rowtype; result text;
begin
 select * into s from public.call_intakes where call_id=p_call_id;
 if not found then result:='no_saved_intake';
 elsif s.triage->>'level'='emergency' then result:='emergency_recorded';
 elsif exists(select 1 from public.appointments where call_id=p_call_id) then result:='booked';
 elsif exists(select 1 from public.priority_requests where call_id=p_call_id) then result:='priority_recorded';
 elsif exists(select 1 from public.followup_requests where call_id=p_call_id) then result:='followup_recorded';
 elsif public.intake_readiness(s.details)->>'scope'='out_of_scope' then result:='out_of_scope';
 elsif public.intake_readiness(s.details)->>'coverage'='out_of_area' then result:='out_of_area';
 else result:='incomplete_intake'; end if;
 return jsonb_build_object('outcome',result,'booking',public.get_booking_status(p_call_id),
  'priority_request',public.priority_request_summary(p_call_id),
  'followup',(select jsonb_build_object('reference',id,'status',status,'notification_status',notification_status) from public.followup_requests where call_id=p_call_id));
end;
$$;
create function public.record_call_event(p_call_id text,p_config jsonb,p_ended_reason text default null) returns jsonb
language plpgsql security definer set search_path='' as $$
begin
 if nullif(trim(p_call_id),'') is null then raise exception 'Invalid call'; end if;
 insert into public.call_audit(call_id,config) values(p_call_id,p_config)
 on conflict(call_id) do update set config=public.call_audit.config||excluded.config;
 if p_ended_reason is not null then
  update public.call_audit set ended_at=coalesce(ended_at,clock_timestamp()),ended_reason=p_ended_reason,
   final_disposition=public.call_disposition(p_call_id) where call_id=p_call_id;
 end if;
 return jsonb_build_object('status','recorded');
end;
$$;
create function public.record_tool_event(p_call_id text,p_operation_id text,p_tool_name text,p_result_status text,p_elapsed_ms integer,p_config_version text) returns void
language sql security definer set search_path='' as $$
 insert into public.tool_audit(call_id,operation_id,tool_name,result_status,elapsed_ms,config_version)
 values(p_call_id,p_operation_id,p_tool_name,p_result_status,p_elapsed_ms,p_config_version);
$$;

do $$ declare t text; begin
 foreach t in array array['followup_requests','call_audit','tool_audit'] loop
  execute format('alter table public.%I enable row level security',t);
  execute format('revoke all on public.%I from public,anon,authenticated,service_role',t);
  execute format('grant select on public.%I to service_role',t);
 end loop;
end $$;
revoke all on function public.request_followup(text,integer,text,text),public.call_disposition(text),public.record_call_event(text,jsonb,text),
 public.record_tool_event(text,text,text,text,integer,text) from public,anon,authenticated,service_role;
grant execute on function public.request_followup(text,integer,text,text),public.call_disposition(text),public.record_call_event(text,jsonb,text),
 public.record_tool_event(text,text,text,text,integer,text) to service_role;
