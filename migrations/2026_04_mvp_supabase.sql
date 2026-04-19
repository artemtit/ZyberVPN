-- Run in Supabase SQL editor

create table if not exists public.servers (
  id bigint generated always as identity primary key,
  name text not null,
  host text not null,
  api_url text not null,
  username text not null,
  password text not null,
  inbound_id int not null,
  public_key text,
  short_id text,
  country text not null default 'unknown',
  is_active boolean not null default true,
  sni text,
  public_port int not null default 443,
  ws_path text not null default '/ws',
  ws_host text
);

create table if not exists public.user_vpn (
  id bigint generated always as identity primary key,
  user_id bigint not null,
  server_id bigint not null references public.servers(id),
  uuid text not null,
  protocol text not null default 'vless-reality',
  config text not null,
  created_at timestamptz not null default now(),
  unique (user_id, server_id, protocol)
);

create index if not exists idx_user_vpn_user_id on public.user_vpn(user_id);
create index if not exists idx_user_vpn_server_id on public.user_vpn(server_id);
