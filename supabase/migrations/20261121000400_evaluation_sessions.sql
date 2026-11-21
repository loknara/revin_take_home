-- Authenticated simulation sessions are separate from real inbound call metadata.
create table public.evaluation_sessions (
    id uuid primary key default gen_random_uuid(),
    call_id text generated always as ('eval_' || id::text) stored unique,
    fixture_key text not null check (fixture_key in ('maya','shared','unknown','withheld')),
    originating_number text generated always as (
        case fixture_key when 'maya' then '+12125550101' when 'shared' then '+12125550102'
                         when 'unknown' then '+12125550199' else null end
    ) stored,
    created_at timestamptz not null default now(),
    expires_at timestamptz not null default (now() + interval '30 minutes'),
    closed_at timestamptz
);
alter table public.evaluation_sessions enable row level security;
revoke all on public.evaluation_sessions from public, anon, authenticated, service_role;
grant select on public.evaluation_sessions to service_role;

create function public.create_evaluation_session(p_fixture_key text) returns jsonb
language plpgsql security definer set search_path = '' as $$
declare session public.evaluation_sessions%rowtype;
begin
    insert into public.evaluation_sessions(fixture_key) values(p_fixture_key) returning * into session;
    return to_jsonb(session);
end;
$$;
create function public.close_evaluation_session(p_session_id uuid) returns void
language sql security definer set search_path = '' as $$
    update public.evaluation_sessions set closed_at=coalesce(closed_at,now()) where id=p_session_id;
$$;
revoke all on function public.create_evaluation_session(text), public.close_evaluation_session(uuid)
    from public, anon, authenticated, service_role;
grant execute on function public.create_evaluation_session(text), public.close_evaluation_session(uuid) to service_role;
comment on table public.evaluation_sessions is
    'Synthetic fixture metadata supplied by the test controller, never the model or simulated caller. One session per conversation. No production phone links are modified.';
