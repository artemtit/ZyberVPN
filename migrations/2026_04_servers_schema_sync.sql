-- Servers table schema sync (safe additive migration)
-- Run in Supabase SQL editor before deploy

alter table if exists public.servers add column if not exists name text;
alter table if exists public.servers add column if not exists host text;
alter table if exists public.servers add column if not exists api_url text;
alter table if exists public.servers add column if not exists username text;
alter table if exists public.servers add column if not exists password text;
alter table if exists public.servers add column if not exists inbound_id int;
alter table if exists public.servers add column if not exists public_key text;
alter table if exists public.servers add column if not exists short_id text;
alter table if exists public.servers add column if not exists country text;
alter table if exists public.servers add column if not exists is_active boolean not null default true;
alter table if exists public.servers add column if not exists sni text;
alter table if exists public.servers add column if not exists public_port int not null default 443;
alter table if exists public.servers add column if not exists ws_path text not null default '/ws';
alter table if exists public.servers add column if not exists ws_host text;
alter table if exists public.servers add column if not exists last_health_check timestamptz;
alter table if exists public.servers add column if not exists health_errors int not null default 0;
alter table if exists public.servers add column if not exists last_error text;

update public.servers set country = 'unknown' where country is null;
update public.servers set ws_path = '/ws' where ws_path is null;
update public.servers set public_port = 443 where public_port is null;
update public.servers set health_errors = 0 where health_errors is null;
update public.servers set is_active = true where is_active is null;

alter table if exists public.servers alter column name set not null;
alter table if exists public.servers alter column host set not null;
alter table if exists public.servers alter column api_url set not null;
alter table if exists public.servers alter column username set not null;
alter table if exists public.servers alter column password set not null;
alter table if exists public.servers alter column inbound_id set not null;
alter table if exists public.servers alter column country set not null;
alter table if exists public.servers alter column is_active set not null;
alter table if exists public.servers alter column public_port set not null;
alter table if exists public.servers alter column ws_path set not null;
alter table if exists public.servers alter column health_errors set not null;

