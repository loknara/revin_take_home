-- Cover every residential ZIP in the three service counties (New York, Kings, Queens),
-- not a hand-picked sample. Coverage is decided by ZIP + state; the spoken city is kept
-- for the address readback only, because Queens mail uses neighborhood names
-- (Astoria, Flushing, Jamaica...) that a city allowlist would wrongly reject.

insert into public.demo_service_areas(postal_code, zone, county, cities)
select z, 'manhattan', 'New York', array['new york','new york city','manhattan','ny']
from unnest(array[
 '10001','10002','10003','10004','10005','10006','10007','10009','10010','10011','10012','10013',
 '10014','10016','10017','10018','10019','10020','10021','10022','10023','10024','10025','10026',
 '10027','10028','10029','10030','10031','10032','10033','10034','10035','10036','10037','10038',
 '10039','10040','10044','10065','10069','10075','10128','10280','10282'
]) z
on conflict (postal_code) do nothing;

insert into public.demo_service_areas(postal_code, zone, county, cities)
select z, 'brooklyn', 'Kings', array['brooklyn','new york','new york city','ny']
from unnest(array[
 '11201','11203','11204','11205','11206','11207','11208','11209','11210','11211','11212','11213',
 '11214','11215','11216','11217','11218','11219','11220','11221','11222','11223','11224','11225',
 '11226','11228','11229','11230','11231','11232','11233','11234','11235','11236','11237','11238',
 '11239','11249'
]) z
on conflict (postal_code) do nothing;

insert into public.demo_service_areas(postal_code, zone, county, cities)
select z, 'queens', 'Queens', array['queens','new york','new york city','ny']
from unnest(array[
 '11004','11005','11101','11102','11103','11104','11105','11106','11109','11354','11355','11356',
 '11357','11358','11360','11361','11362','11363','11364','11365','11366','11367','11368','11369',
 '11370','11372','11373','11374','11375','11377','11378','11379','11385','11411','11412','11413',
 '11414','11415','11416','11417','11418','11419','11420','11421','11422','11423','11426','11427',
 '11428','11429','11432','11433','11434','11435','11436','11691','11692','11693','11694','11697'
]) z
on conflict (postal_code) do nothing;

-- Same readiness rules as before, except coverage no longer depends on the city name.
create or replace function public.intake_readiness_before_dispatch(d jsonb) returns jsonb
language plpgsql stable security definer set search_path = '' as $$
declare a public.demo_service_areas%rowtype; missing jsonb := '[]'; k text; coverage text; scope text := 'eligible'; risk jsonb := public.assess_intake_risk(d);
begin
    if d->>'property_type' = 'commercial' or d->>'service_category' = 'other' or d->>'service_type' = 'other' then scope := 'out_of_scope';
    elsif coalesce(d->>'property_type','unknown') <> 'residential' or coalesce(d->>'service_category','unknown') <> 'hvac' then scope := 'needs_details'; end if;
    select * into a from public.demo_service_areas where postal_code=d->>'service_zip' and enabled;
    if d->>'service_zip' is null or d->>'service_state' is null then coverage := 'needs_details';
    elsif a.postal_code is null or d->>'service_state' <> a.state then coverage := 'out_of_area';
    else coverage := 'covered'; end if;
    foreach k in array array['caller_name','callback_number','service_address','service_unit','service_city','service_state','service_zip','issue_summary'] loop
        if nullif(trim(d->>k),'') is null then missing := missing || to_jsonb(k); end if;
    end loop;
    foreach k in array array['address_confirmed','callback_confirmed'] loop
        if coalesce(d->>k,'false') <> 'true' then missing := missing || to_jsonb(k); end if;
    end loop;
    if coalesce(d->>'service_type','unknown') not in ('repair','maintenance') then missing := missing || '"service_type"'; end if;
    if coalesce(d->>'property_type','unknown') <> 'residential' then missing := missing || '"property_type"'; end if;
    if coalesce(d->>'service_category','unknown') <> 'hvac' then missing := missing || '"service_category"'; end if;
    return jsonb_build_object('scope',scope,'coverage',coverage,'zone',a.zone,'county',a.county,'policy','county-coverage-v2',
        'missing_fields',missing,'risk',risk,
        'can_book',scope='eligible' and coverage='covered' and missing='[]' and risk->>'level'='routine',
        'next_required',case when risk->>'level' in ('emergency','priority') then risk->>'level'
            when risk->>'level'='needs_assessment' then risk->'missing_risk_fields'->>0
            when scope='out_of_scope' then 'explain_scope' when coverage='out_of_area' then 'explain_coverage'
            else missing->>0 end);
end;
$$;
revoke all on function public.intake_readiness_before_dispatch(jsonb) from public,anon,authenticated,service_role;
