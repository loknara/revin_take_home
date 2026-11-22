-- Durable background work. A sleeping/crashed web worker cannot lose queued jobs.
create table public.enrichment_jobs (
    id uuid primary key default gen_random_uuid(),
    call_id text not null references public.call_intakes(call_id) on delete cascade,
    kind text not null default 'membership' check(kind='membership'),
    input_revision integer not null,
    dependency_snapshot jsonb not null,
    status text not null default 'pending' check(status in ('pending','running','ready','failed','timed_out','superseded')),
    result jsonb,
    source text not null default 'demo_supabase_membership',
    created_at timestamptz not null default clock_timestamp(),
    ready_after timestamptz not null default clock_timestamp(),
    deadline timestamptz not null default clock_timestamp()+interval '15 seconds',
    expires_at timestamptz not null default clock_timestamp()+interval '5 minutes',
    lease_token uuid,
    lease_until timestamptz,
    attempts integer not null default 0,
    completed_at timestamptz
);
create index enrichment_pending on public.enrichment_jobs(status,ready_after);
alter table public.enrichment_jobs enable row level security;
revoke all on public.enrichment_jobs from public,anon,authenticated,service_role;
grant select on public.enrichment_jobs to service_role;
alter table public.evaluation_sessions add column lookup_delay_ms integer not null default 0 check(lookup_delay_ms between 0 and 8000);

create function public.start_membership_check(p_call_id text,p_expected_revision integer) returns jsonb
language plpgsql security definer set search_path = '' as $$
declare s public.call_intakes%rowtype; j public.enrichment_jobs%rowtype; delay_ms integer := 0;
begin
    select * into s from public.call_intakes where call_id=p_call_id for update;
    if not found then return jsonb_build_object('status','needs_intake'); end if;
    if s.revision <> p_expected_revision then return jsonb_build_object('status','revision_conflict','current_revision',s.revision); end if;
    if s.triage->>'level'='emergency' then return jsonb_build_object('status','deferred_for_emergency'); end if;
    if s.details->>'property_type'='commercial' then return jsonb_build_object('status','out_of_scope'); end if;
    if coalesce(s.details->>'address_confirmed','false')<>'true' or nullif(s.details->>'caller_name','') is null
       or nullif(s.details->>'service_unit','') is null or nullif(s.details->>'service_zip','') is null then
        return jsonb_build_object('status','needs_details'); end if;
    update public.enrichment_jobs set status='superseded',completed_at=clock_timestamp()
      where call_id=p_call_id and status in ('pending','running','ready')
      and dependency_snapshot <> public.membership_input_snapshot(s.details);
    select * into j from public.enrichment_jobs where call_id=p_call_id
      and dependency_snapshot=public.membership_input_snapshot(s.details)
      and expires_at>clock_timestamp() and status in ('pending','running','ready')
      order by created_at desc limit 1;
    if found then return jsonb_build_object('status',j.status,'request_id',j.id,'expires_at',j.expires_at); end if;
    select lookup_delay_ms into delay_ms from public.evaluation_sessions where call_id=p_call_id and closed_at is null;
    insert into public.enrichment_jobs(call_id,input_revision,dependency_snapshot,ready_after)
      values(p_call_id,s.revision,public.membership_input_snapshot(s.details),clock_timestamp()+coalesce(delay_ms,0)*interval '1 millisecond') returning * into j;
    return jsonb_build_object('status','pending','request_id',j.id,'expires_at',j.expires_at,
      'next_action','Collect an independent missing fact such as callback; refresh context after the answer.');
end;
$$;

create function public.claim_enrichment_job() returns jsonb
language plpgsql security definer set search_path = '' as $$
declare j public.enrichment_jobs%rowtype;
begin
    update public.enrichment_jobs set status='timed_out',completed_at=clock_timestamp()
      where status in ('pending','running') and deadline<=clock_timestamp();
    select * into j from public.enrichment_jobs
      where deadline>clock_timestamp() and ready_after<=clock_timestamp() and attempts<2
      and (status='pending' or (status='running' and lease_until<clock_timestamp()))
      order by created_at for update skip locked limit 1;
    if not found then return null; end if;
    update public.enrichment_jobs set status='running',lease_token=gen_random_uuid(),
      lease_until=clock_timestamp()+interval '5 seconds', attempts=attempts+1 where id=j.id returning * into j;
    return jsonb_build_object('id',j.id,'lease_token',j.lease_token);
end;
$$;

create function public.finish_enrichment_job(p_job_id uuid,p_lease_token uuid,p_failed boolean default false) returns jsonb
language plpgsql security definer set search_path = '' as $$
declare j public.enrichment_jobs%rowtype; s public.call_intakes%rowtype; r jsonb;
begin
    -- Always acquire call before job to match intake/start order and avoid deadlocks.
    select * into j from public.enrichment_jobs where id=p_job_id;
    if not found then return jsonb_build_object('status','not_found'); end if;
    select * into s from public.call_intakes where call_id=j.call_id for update;
    select * into j from public.enrichment_jobs where id=p_job_id for update;
    if j.status<>'running' or j.lease_token is distinct from p_lease_token then return jsonb_build_object('status','lease_lost'); end if;
    if j.dependency_snapshot<>public.membership_input_snapshot(s.details) or s.triage->>'level'='emergency' then
      j.status:='superseded';
    elsif j.deadline<=clock_timestamp() then j.status:='timed_out';
    elsif p_failed then j.status:='failed';
    else
      r:=public.resolve_call_membership(j.call_id,s.revision);
      j.status:='ready';
    end if;
    update public.enrichment_jobs set status=j.status,result=r,completed_at=clock_timestamp() where id=j.id;
    return jsonb_build_object('status',j.status);
end;
$$;

create function public.refresh_call_context(p_call_id text) returns jsonb
language plpgsql security definer set search_path = '' as $$
declare s public.call_intakes%rowtype; jobs jsonb;
begin
    select * into s from public.call_intakes where call_id=p_call_id for update;
    if not found then return jsonb_build_object('status','needs_intake'); end if;
    update public.enrichment_jobs set status='superseded',completed_at=clock_timestamp()
      where call_id=p_call_id and status in ('pending','running','ready') and
      (dependency_snapshot<>public.membership_input_snapshot(s.details) or s.triage->>'level'='emergency');
    update public.enrichment_jobs set status='timed_out',completed_at=clock_timestamp()
      where call_id=p_call_id and status in ('pending','running') and deadline<=clock_timestamp();
    select coalesce(jsonb_agg(x),'[]') into jobs from (
      select id as request_id,status,source,input_revision,created_at,completed_at,expires_at,
        case when status='ready' and expires_at>clock_timestamp() then result else null end as result
      from public.enrichment_jobs where call_id=p_call_id order by created_at desc limit 3
    ) x;
    return jsonb_build_object('status','current','state',jsonb_build_object('call_id',s.call_id,'revision',s.revision,'details',s.details,'triage',s.triage),
      'readiness',public.intake_readiness(s.details),'membership',public.current_call_membership(p_call_id),
      'priority_request',public.priority_request_summary(p_call_id),'jobs',jobs);
end;
$$;
revoke all on function public.start_membership_check(text,integer),public.claim_enrichment_job(),
 public.finish_enrichment_job(uuid,uuid,boolean),public.refresh_call_context(text) from public,anon,authenticated,service_role;
grant execute on function public.start_membership_check(text,integer),public.claim_enrichment_job(),
 public.finish_enrichment_job(uuid,uuid,boolean),public.refresh_call_context(text) to service_role;
