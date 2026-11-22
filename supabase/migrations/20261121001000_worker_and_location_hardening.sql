-- Preserve production worker isolation while deterministic transaction tests run.
drop function public.claim_enrichment_job();
create function public.claim_enrichment_job(p_call_id text default null) returns jsonb
language plpgsql security definer set search_path = '' as $$
declare j public.enrichment_jobs%rowtype;
begin
    update public.enrichment_jobs set status='timed_out',completed_at=clock_timestamp()
      where status in ('pending','running') and deadline<=clock_timestamp();
    select * into j from public.enrichment_jobs
      where ((p_call_id is null and call_id not like 'test_%') or call_id=p_call_id)
      and deadline>clock_timestamp() and ready_after<=clock_timestamp() and attempts<2
      and (status='pending' or (status='running' and lease_until<clock_timestamp()))
      order by created_at for update skip locked limit 1;
    if not found then return null; end if;
    update public.enrichment_jobs set status='running',lease_token=gen_random_uuid(),
      lease_until=clock_timestamp()+interval '5 seconds', attempts=attempts+1 where id=j.id returning * into j;
    return jsonb_build_object('id',j.id,'lease_token',j.lease_token);
end;
$$;

revoke all on function public.claim_enrichment_job(text) from public,anon,authenticated,service_role;
grant execute on function public.claim_enrichment_job(text) to service_role;
create or replace function public.resolve_call_membership(p_call_id text, p_expected_revision integer) returns jsonb
language plpgsql security definer set search_path = '' as $$
declare
    intake public.call_intakes%rowtype;
    location public.service_locations%rowtype;
    plan public.memberships%rowtype;
    matches text[];
    d jsonb;
    result jsonb;
    missing jsonb := '[]';
    resolution text;
    member_status text := 'unknown';
    claim text;
    disputed boolean := false;
    today date := (current_timestamp at time zone 'America/New_York')::date;
begin
    if nullif(trim(p_call_id),'') is null or p_expected_revision is null or p_expected_revision < 0 then
        raise exception 'Invalid membership request';
    end if;
    -- Serialize with intake corrections, so this result describes a real revision.
    select * into intake from public.call_intakes where call_id=p_call_id for update;
    if not found then
        return '{"status":"needs_intake","membership_status":"unknown"}'::jsonb;
    end if;
    if intake.revision <> p_expected_revision then
        return jsonb_build_object('status','revision_conflict','membership_status','unknown',
                                  'current_revision',intake.revision);
    end if;
    d := intake.details;
    claim := coalesce(d->>'membership_claim','unknown');
    if intake.triage->>'level' = 'emergency' then
        resolution := 'deferred_for_emergency';
    elsif d->>'property_type' = 'commercial' then
        resolution := 'out_of_scope';
    else
        select coalesce(jsonb_agg(field), '[]'::jsonb) into missing
        from unnest(array['caller_name','service_address','service_unit','service_zip']) field
        where nullif(trim(d->>field),'') is null;
        if jsonb_array_length(missing) > 0 then
            resolution := 'needs_details';
        elsif d->>'address_confirmed' is distinct from 'true' then
            resolution := 'needs_address_confirmation';
        else
            select array_agg(id) into matches from (
                select loc.id
                from public.service_locations loc join public.customers customer on customer.id=loc.customer_id
                where loc.street_key=public.street_lookup_key(d->>'service_address')
                  and loc.unit_key=public.unit_lookup_key(d->>'service_unit')
                  and loc.postal_code=d->>'service_zip'
                  and (d->>'service_state' is null or d->>'service_state'=loc.state)
                  and (d->>'service_city' is null or public.lookup_text(d->>'service_city')=public.lookup_text(loc.city)
                    or exists(select 1 from public.demo_service_areas a where a.postal_code=loc.postal_code
                        and public.lookup_text(d->>'service_city')=any(a.cities) and public.lookup_text(loc.city)=any(a.cities)))
                  and (
                      public.lookup_text(customer.full_name)=public.lookup_text(d->>'caller_name')
                      or (public.lookup_text(customer.first_name)=public.lookup_text(d->>'caller_name')
                          and exists (select 1 from public.customer_phone_numbers phone
                                      where phone.customer_id=customer.id
                                        and phone.phone_number=intake.originating_number))
                  )
                order by loc.id limit 2
            ) candidates;
            if coalesce(cardinality(matches),0) = 0 then
                resolution := case when position(' ' in public.lookup_text(d->>'caller_name'))=0
                                   then 'needs_full_name' else 'no_match' end;
            elsif cardinality(matches)>1 then
                resolution := 'ambiguous';
            else
                select * into location from public.service_locations where id=matches[1];
                resolution := 'resolved';
                select * into plan from public.memberships where location_id=location.id;
                if not found then member_status := 'none';
                elsif plan.status='inactive' then member_status := 'inactive';
                elsif plan.expires_on < today then member_status := 'expired';
                elsif plan.effective_on > today then member_status := 'not_started';
                else member_status := 'active';
                end if;
                disputed := (claim='true' and member_status<>'active') or
                            (claim='false' and member_status='active');
            end if;
        end if;
    end if;
    result := jsonb_build_object(
        'status',resolution, 'membership_status',member_status,
        'intake_revision',intake.revision, 'missing_fields',missing,
        'caller_claim',claim, 'disputed',disputed,
        'verification_required',member_status='unknown' or disputed,
        'identity_assurance','conversational_demo_only',
        'checked_at',current_timestamp, 'approved_benefits','[]'::jsonb);
    if resolution='resolved' and plan.location_id is not null then
        result := result || jsonb_build_object('plan_name',plan.plan_name,
                    'effective_on',plan.effective_on,'expires_on',plan.expires_on);
    end if;
    insert into public.call_membership_checks(call_id,intake_revision,input_snapshot,customer_id,location_id,result)
        values(p_call_id,intake.revision,public.membership_input_snapshot(d),location.customer_id,location.id,result)
        on conflict(call_id,intake_revision) do update set
            input_snapshot=excluded.input_snapshot, customer_id=excluded.customer_id,
            location_id=excluded.location_id, result=excluded.result, checked_at=now();
    return result;
end;
$$;
