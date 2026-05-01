-- TODO: update host, api_url, credentials, public_key, short_id
-- and set is_active=true when servers are configured.

INSERT INTO public.servers (
  name, host, api_url, username, password, inbound_id,
  public_key, short_id, country, is_active, sni, public_port
)
VALUES
  ('ZyberVPN-NL-1', 'vpn2.zybervpn.ru', 'http://vpn2.zybervpn.ru:54321',
   'admin', 'changeme', 1,
   '', '', 'NL', false, 'static.rutube.ru', 443),
  ('ZyberVPN-DE-1', 'vpn3.zybervpn.ru', 'http://vpn3.zybervpn.ru:54321',
   'admin', 'changeme', 1,
   '', '', 'DE', false, 'static.rutube.ru', 443);
