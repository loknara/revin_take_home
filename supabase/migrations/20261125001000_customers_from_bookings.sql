-- A caller who books becomes a customer (2026-11-24).
-- Until now the customer tables were read-only, standing in for Summit Air's own system, so
-- a new caller who booked was a stranger again on their next call. Once a visit is booked
-- (name given, address and callback confirmed), the caller is saved as a customer with their
-- number(s) and the service address, so the next call recognizes them ("Am I speaking with
-- Alex?" and the address on file). Rules:
--  * Reuse a customer already on that number with the same first name; never overwrite one.
--    A different name on a known number is added as another person (a shared phone).
--  * Marked source='phone_agent' with the call that created it, for the office to review.
--  * Test calls (eval_, test_) are skipped so simulations and tests don't fill the customer list.
--  * Never blocks the booking: any problem here is logged and the booking stands.
-- No membership is created: being a customer and having a plan are separate.

alter table public.customers
 add column source text not null default 'crm' check (source in ('crm','phone_agent')),
 add column created_by_call text;

create function public.remember_booked_caller() returns trigger
language plpgsql security definer set search_path='' as $$
declare c public.call_intakes%rowtype; d jsonb := new.intake_snapshot; first text; cust text; num text;
begin
 if new.call_id like 'eval\_%' or new.call_id like 'test\_%' then return null; end if;
 select * into c from public.call_intakes where call_id=new.call_id;
 if c.originating_number is null or coalesce(trim(d->>'caller_name'),'')=''
    or coalesce(d->>'address_confirmed','')<>'true' or coalesce(d->>'service_zip','') !~ '^[0-9]{5}$' then
   return null;
 end if;
 begin
   first := split_part(trim(d->>'caller_name'),' ',1);
   select p.customer_id into cust from public.customer_phone_numbers p
     join public.customers cu on cu.id=p.customer_id
    where p.phone_number=c.originating_number and lower(cu.first_name)=lower(first)
    order by cu.created_at limit 1;
   if cust is null then
     insert into public.customers(first_name,full_name,source,created_by_call)
     values(first,trim(d->>'caller_name'),'phone_agent',new.call_id) returning id into cust;
   end if;
   foreach num in array array[c.originating_number,
       case when d->>'callback_confirmed'='true' then d->>'callback_number' end] loop
     if num ~ '^\+[1-9][0-9]{6,14}$' then
       insert into public.customer_phone_numbers(phone_number,customer_id) values(num,cust) on conflict do nothing;
     end if;
   end loop;
   insert into public.service_locations(customer_id,street_address,unit,postal_code,city,state,property_type,is_primary)
   values(cust,trim(d->>'service_address'),coalesce(nullif(trim(d->>'service_unit'),''),'none'),d->>'service_zip',
          coalesce(nullif(d->>'service_city',''),'New York'),coalesce(nullif(d->>'service_state',''),'NY'),
          coalesce(nullif(d->>'property_type',''),'residential'),
          not exists(select 1 from public.service_locations where customer_id=cust and is_primary))
   on conflict (customer_id,street_key,unit_key,postal_code) do nothing;
 exception when others then
   raise warning 'remember_booked_caller skipped for %: %', new.call_id, sqlerrm;
 end;
 return null;
end;
$$;
revoke all on function public.remember_booked_caller() from public,anon,authenticated,service_role;

create trigger remember_booked_caller
 after insert on public.appointments
 for each row execute function public.remember_booked_caller();
