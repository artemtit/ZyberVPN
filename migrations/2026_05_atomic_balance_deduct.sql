-- Atomic balance deduction — replaces Python read-then-check-then-write TOCTOU pattern.
-- Returns new balance on success, -1 if balance is insufficient or user not found.

CREATE OR REPLACE FUNCTION public.deduct_user_balance_safe(p_tg_id bigint, p_amount int)
RETURNS int
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
  new_balance int;
BEGIN
  UPDATE public.users
  SET balance = balance - p_amount
  WHERE tg_id = p_tg_id AND COALESCE(balance, 0) >= p_amount
  RETURNING balance INTO new_balance;

  RETURN COALESCE(new_balance, -1);
END;
$$;
