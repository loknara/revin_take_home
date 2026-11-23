-- Fictional records only. Real test numbers are added privately in the database.
insert into public.customers (id, first_name) values
    ('demo_maya', 'Maya'),
    ('demo_omar', 'Omar'),
    ('demo_leah', 'Leah')
on conflict (id) do nothing;

insert into public.customer_phone_numbers (phone_number, customer_id) values
    ('+12125550101', 'demo_maya'),
    ('+12125550102', 'demo_omar'),
    ('+12125550102', 'demo_leah')
on conflict (phone_number, customer_id) do nothing;

-- Fictional service locations and plans. 'none' means explicitly no unit.
update public.customers set full_name = case id
    when 'demo_maya' then 'Maya Chen'
    when 'demo_omar' then 'Omar Rivera'
    when 'demo_leah' then 'Leah Brooks' end
where id in ('demo_maya','demo_omar','demo_leah') and full_name is null;

insert into public.service_locations(id,customer_id,street_address,unit,postal_code,city,state,property_type) values
    ('demo_maya_home','demo_maya','152 West 72nd Street','4B','10023','New York','NY','residential'),
    ('demo_maya_second_home','demo_maya','240 West 95th Street','none','10025','New York','NY','residential'),
    ('demo_omar_home','demo_omar','310 West 80th Street','2A','10024','New York','NY','residential'),
    ('demo_leah_home','demo_leah','410 East 79th Street','6C','10075','New York','NY','residential')
on conflict(id) do nothing;

insert into public.memberships(location_id,plan_name,status,effective_on,expires_on) values
    ('demo_maya_home','Summit Care — demo','active','2026-01-01','2027-12-31'),
    ('demo_maya_second_home','Summit Care — demo','active','2024-01-01','2025-12-31'),
    ('demo_leah_home','Summit Care — demo','inactive','2026-01-01','2027-12-31')
on conflict(location_id) do nothing;
-- Omar has a customer/location record and no membership record; no benefits configured.

-- Explicit synthetic primary address for the saved-address confirmation flow.
update public.service_locations set is_primary=true where id='demo_maya_home';
