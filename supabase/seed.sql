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
