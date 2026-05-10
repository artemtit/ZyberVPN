-- Atomic increment functions for traffic limits.
-- Replaces read-modify-write pattern in payment handlers to prevent
-- concurrent payments from losing each other's traffic increment.

CREATE OR REPLACE FUNCTION increment_user_traffic_limit(p_tg_id bigint, p_amount int)
RETURNS void
LANGUAGE sql
AS $$
  UPDATE users
  SET traffic_limit_gb = COALESCE(traffic_limit_gb, 0) + p_amount
  WHERE tg_id = p_tg_id;
$$;

CREATE OR REPLACE FUNCTION increment_key_traffic_limit(p_key_id bigint, p_tg_id bigint, p_amount int)
RETURNS void
LANGUAGE sql
AS $$
  UPDATE keys
  SET traffic_limit_gb = COALESCE(traffic_limit_gb, 0) + p_amount
  WHERE id = p_key_id AND tg_id = p_tg_id;
$$;
