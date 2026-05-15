from __future__ import annotations

"""Platega payment webhook endpoint.

Platega sends POST callbacks with:
  {"id": "<platega_transaction_id>", "amount": 970.0, "currency": "RUB",
   "status": "CONFIRMED|CANCELED", "paymentMethod": 2}

There is no HMAC signature — we verify authenticity by fetching the
transaction status directly from the Platega API before processing.

The callback URL should include a secret token to prevent spoofing:
  https://<host>/platega/webhook?secret=<PLATEGA_WEBHOOK_SECRET>
"""

import logging

from aiohttp import web

from app.repositories.payments import PaymentsRepository

logger = logging.getLogger(__name__)


async def platega_webhook(request: web.Request) -> web.Response:
    settings = request.app["settings"]

    # URL-based secret check (PLATEGA_WEBHOOK_SECRET must be set; deny all when missing).
    expected_secret = getattr(settings, "platega_webhook_secret", "")
    incoming_secret = request.query.get("secret", "")
    if not expected_secret or incoming_secret != expected_secret:
        logger.warning(
            "Platega webhook: invalid or missing secret from ip=%s", request.remote
        )
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)

    # Optional IP whitelist (PLATEGA_ALLOWED_IPS: comma-separated list).
    allowed_ips_raw = getattr(settings, "platega_allowed_ips", "") or ""
    if allowed_ips_raw:
        allowed_ips = {ip.strip() for ip in allowed_ips_raw.split(",") if ip.strip()}
        if request.remote not in allowed_ips:
            logger.warning("Platega webhook: blocked IP ip=%s", request.remote)
            return web.json_response({"ok": False, "error": "forbidden"}, status=403)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid json"}, status=400)

    transaction_id = str(body.get("id") or "").strip()
    status = str(body.get("status") or "").strip()
    amount = body.get("amount")

    if not transaction_id:
        return web.json_response({"ok": False, "error": "missing id"}, status=400)

    logger.info(
        "Platega webhook received transaction_id=%s status=%s amount=%s ip=%s",
        transaction_id, status, amount, request.remote,
    )

    if status != "CONFIRMED":
        # CANCELED / EXPIRED — nothing to do.
        return web.json_response({"ok": True})

    # Early guard — skip API verification and processing for already-paid payments.
    db = request.app["db"]
    try:
        existing = await PaymentsRepository(db).get_by_payload(transaction_id)
        if existing and existing.get("status") in ("paid", "provisioning", "active", "failed"):
            logger.info("Platega webhook: already finalized/processing, skipping transaction_id=%s", transaction_id)
            return web.json_response({"ok": True})
    except Exception:
        logger.warning("Platega webhook: early-guard DB check failed transaction_id=%s", transaction_id)

    # Verify by calling Platega API (no signature available).
    platega_client = request.app.get("platega_client")
    if platega_client is None:
        logger.error("Platega webhook: platega_client not configured")
        return web.Response(status=503)

    try:
        actual_status = await platega_client.verify_payment_status(transaction_id)
    except Exception:
        logger.exception("Platega webhook: status verification failed transaction_id=%s", transaction_id)
        return web.Response(status=500)

    if actual_status != "CONFIRMED":
        logger.warning(
            "Platega webhook: verification mismatch transaction_id=%s "
            "webhook_status=%s api_status=%s — ignoring",
            transaction_id, status, actual_status,
        )
        return web.json_response({"ok": True})

    # Process the payment.
    bot = request.app["bot"]
    try:
        from app.services.platega_handler import process_confirmed_platega_payment
        await process_confirmed_platega_payment(transaction_id, bot, db, settings)
    except Exception:
        logger.exception("Platega webhook: handler failed transaction_id=%s", transaction_id)
        return web.Response(status=500)

    return web.json_response({"ok": True})


def register_platega_webhook_routes(app: web.Application) -> None:
    app.router.add_post("/platega/webhook", platega_webhook)
