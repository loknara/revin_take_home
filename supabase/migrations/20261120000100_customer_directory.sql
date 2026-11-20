-- Caller ID supplies a candidate, not proof of identity or membership.
create table public.customers (
    id text primary key default gen_random_uuid()::text,
    first_name text not null check (length(trim(first_name)) between 1 and 100),
    created_at timestamptz not null default now()
);

create table public.customer_phone_numbers (
    phone_number text not null check (phone_number ~ '^\+[1-9][0-9]{6,14}$'),
    customer_id text not null references public.customers(id) on delete cascade,
    created_at timestamptz not null default now(),
    -- Shared household numbers can point to multiple distinct customers.
    primary key (phone_number, customer_id)
);

alter table public.customers enable row level security;
alter table public.customer_phone_numbers enable row level security;

-- No browser/client access. The backend key can read this directory only.
revoke all on public.customers, public.customer_phone_numbers from public, anon, authenticated, service_role;
grant select on public.customers, public.customer_phone_numbers to service_role;

comment on table public.customers is 'Synthetic Summit Air demo customers; no membership data yet.';
