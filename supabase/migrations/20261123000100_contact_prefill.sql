-- Reuse contact information only after the supplied name matches incoming caller ID.
-- This is conversational matching, not authorization to change a customer account.
alter table public.service_locations add column is_primary boolean not null default false;
create unique index one_primary_service_location on public.service_locations(customer_id) where is_primary;
-- Explicit default for the existing synthetic fixture, not a first-row heuristic.
update public.service_locations set is_primary=true where id='demo_maya_home';

create table public.contact_prefill_events (
 id uuid primary key default gen_random_uuid(),
 call_id text not null references public.call_intakes(call_id) on delete cascade,
 intake_revision integer not null,
 source text not null check (source in ('provider_caller_id','matched_service_location','name_correction')),
 location_id text references public.service_locations(id),
 fields jsonb not null,
 created_at timestamptz not null default now()
);
alter table public.contact_prefill_events enable row level security;
revoke all on public.contact_prefill_events from public,anon,authenticated,service_role;
grant select on public.contact_prefill_events to service_role;

create function public.prefill_call_contact(p_call_id text) returns void
language plpgsql security definer set search_path='' as $$
declare s public.call_intakes%rowtype; d jsonb; loc public.service_locations%rowtype;
 customers text[]; locations text[]; patch jsonb := '{}'; src text; source_location text;
begin
 select * into s from public.call_intakes where call_id=p_call_id for update;
 if not found or s.triage->>'level'='emergency' then return; end if;
 d:=s.details;
 -- A changed name must not retain an address supplied solely by the old match.
 if d ? '_prefill_name' and d->>'_prefill_name' is distinct from public.lookup_text(d->>'caller_name') then
   d:=d-array['service_address','service_unit','service_city','service_state','service_zip',
              'address_confirmed','confirmation_quote','_prefill_name','_prefill_location'];
   if d ? '_prefill_property' then d:=d-array['property_type','_prefill_property']; end if;
   src:='name_correction';
 end if;
 if not (d ? 'callback_number') and s.originating_number ~ '^\+[1-9][0-9]{7,14}$' then
   patch:=patch||jsonb_build_object('callback_number',s.originating_number,'callback_confirmed',false);
   src:=coalesce(src,'provider_caller_id');
 end if;
 -- Never complete or replace a partially caller-supplied address with account data.
 if not (d ?| array['service_address','service_unit','service_city','service_state','service_zip'])
    and nullif(trim(d->>'caller_name'),'') is not null then
   select array_agg(c.id) into customers from public.customers c
   join public.customer_phone_numbers p on p.customer_id=c.id
   where p.phone_number=s.originating_number and
    (public.lookup_text(c.full_name)=public.lookup_text(d->>'caller_name')
      or public.lookup_text(c.first_name)=public.lookup_text(d->>'caller_name'));
   if cardinality(customers)=1 then
     select array_agg(id order by id) into locations from public.service_locations
       where customer_id=customers[1];
     if cardinality(locations)=1 then
       select * into loc from public.service_locations where id=locations[1];
     else
       select * into loc from public.service_locations where customer_id=customers[1] and is_primary;
     end if;
     if loc.id is not null then
       patch:=patch||jsonb_build_object('service_address',loc.street_address,'service_unit',loc.unit,
         'service_city',loc.city,'service_state',loc.state,'service_zip',loc.postal_code,
         'address_confirmed',false,'_prefill_name',public.lookup_text(d->>'caller_name'),
         '_prefill_location',loc.id);
       -- A caller's explicit commercial/residential answer wins over the record.
       if coalesce(d->>'property_type','unknown')='unknown' then patch:=patch||jsonb_build_object('property_type',loc.property_type,'_prefill_property',true); end if;
       src:='matched_service_location';source_location:=loc.id;
     end if;
   end if;
 end if;
 if d||patch is distinct from s.details then
   update public.call_intakes set details=d||patch, updated_at=now()
     where call_id=p_call_id returning * into s;
   insert into public.contact_prefill_events(call_id,intake_revision,source,location_id,fields)
    values(p_call_id,s.revision,src,source_location,patch);
 end if;
end;
$$;
revoke all on function public.prefill_call_contact(text) from public,anon,authenticated,service_role;

create or replace function public.update_call_intake(
 p_call_id text,p_operation_id text,p_originating_number text,p_expected_revision integer,
 p_changes jsonb,p_caller_quote text,p_confirmation_address text default null,p_confirmation_callback text default null
) returns jsonb language plpgsql security definer set search_path='' as $$
declare r jsonb; s public.call_intakes%rowtype;
begin
 r:=public.save_call_intake(p_call_id,p_operation_id,p_originating_number,p_expected_revision,
       p_changes,p_caller_quote,p_confirmation_address,p_confirmation_callback);
 if r->>'status'='saved' and r->>'replayed'='false' then
   if p_changes ? 'property_type' then
     update public.call_intakes set details=details-'_prefill_property'
      where call_id=p_call_id and details ? '_prefill_property';
   end if;
   -- Once the caller supplies address components, it is no longer a saved-address suggestion.
   if p_changes ?| array['service_address','service_unit','service_city','service_state','service_zip'] then
     update public.call_intakes set details=(case when details ? '_prefill_property'
       then details-'property_type' else details end)-array['_prefill_name','_prefill_location','_prefill_property']
       where call_id=p_call_id and details ? '_prefill_name';
   end if;
   perform public.prefill_call_contact(p_call_id);
 end if;
 select * into s from public.call_intakes where call_id=p_call_id;
 return r||jsonb_build_object('state',to_jsonb(s),'readiness',public.intake_readiness(s.details),
   'priority_request',public.priority_request_summary(p_call_id),'membership',public.current_call_membership(p_call_id));
end;
$$;
