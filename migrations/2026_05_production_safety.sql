-- Production safety fixes for multi-key payments/access control.

ALTER TABLE public.users
    ADD COLUMN IF NOT EXISTS is_banned boolean NOT NULL DEFAULT false;

ALTER TABLE public.keys
    ADD COLUMN IF NOT EXISTS disabled_at timestamptz NULL;

CREATE OR REPLACE FUNCTION public.set_primary_key(p_tg_id bigint, p_key_id bigint)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM public.keys
        WHERE tg_id = p_tg_id AND id = p_key_id
    ) THEN
        RAISE EXCEPTION 'key % for user % not found', p_key_id, p_tg_id;
    END IF;

    UPDATE public.keys
    SET is_primary = (id = p_key_id)
    WHERE tg_id = p_tg_id;
END;
$$;

CREATE OR REPLACE FUNCTION public.extend_subscription_months(p_tg_id bigint, p_months integer)
RETURNS public.subscriptions
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    v_row public.subscriptions%ROWTYPE;
    v_base timestamptz;
    v_expires timestamptz;
BEGIN
    SELECT *
    INTO v_row
    FROM public.subscriptions
    WHERE tg_id = p_tg_id
      AND status = 'active'
      AND expires_at > now()
    ORDER BY expires_at DESC
    LIMIT 1
    FOR UPDATE;

    IF FOUND THEN
        v_base := GREATEST(COALESCE(v_row.expires_at, now()), now());
        v_expires := v_base + make_interval(months => GREATEST(COALESCE(p_months, 1), 1));
        UPDATE public.subscriptions
        SET expires_at = v_expires,
            status = 'active'
        WHERE id = v_row.id
        RETURNING * INTO v_row;
        RETURN v_row;
    END IF;

    v_expires := now() + make_interval(months => GREATEST(COALESCE(p_months, 1), 1));
    INSERT INTO public.subscriptions (tg_id, expires_at, status)
    VALUES (p_tg_id, v_expires, 'active')
    RETURNING * INTO v_row;
    RETURN v_row;
END;
$$;

CREATE OR REPLACE FUNCTION public.extend_subscription_days(p_tg_id bigint, p_days integer)
RETURNS public.subscriptions
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    v_row public.subscriptions%ROWTYPE;
    v_base timestamptz;
    v_expires timestamptz;
BEGIN
    SELECT *
    INTO v_row
    FROM public.subscriptions
    WHERE tg_id = p_tg_id
      AND status = 'active'
      AND expires_at > now()
    ORDER BY expires_at DESC
    LIMIT 1
    FOR UPDATE;

    IF FOUND THEN
        v_base := GREATEST(COALESCE(v_row.expires_at, now()), now());
        v_expires := v_base + make_interval(days => GREATEST(COALESCE(p_days, 1), 1));
        UPDATE public.subscriptions
        SET expires_at = v_expires,
            status = 'active'
        WHERE id = v_row.id
        RETURNING * INTO v_row;
        RETURN v_row;
    END IF;

    v_expires := now() + make_interval(days => GREATEST(COALESCE(p_days, 1), 1));
    INSERT INTO public.subscriptions (tg_id, expires_at, status)
    VALUES (p_tg_id, v_expires, 'active')
    RETURNING * INTO v_row;
    RETURN v_row;
END;
$$;

CREATE OR REPLACE FUNCTION public.update_server_health(
    p_server_id bigint,
    p_is_active boolean,
    p_ok boolean,
    p_error_text text
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
    UPDATE public.servers
    SET is_active = p_is_active,
        last_health_check = now(),
        last_error = COALESCE(p_error_text, ''),
        health_errors = CASE
            WHEN p_ok THEN 0
            ELSE COALESCE(health_errors, 0) + 1
        END
    WHERE id = p_server_id;
END;
$$;
