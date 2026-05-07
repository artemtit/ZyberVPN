from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from aiohttp import ClientError, ClientSession, ClientTimeout

logger = logging.getLogger(__name__)

PLATEGA_BASE = "https://app.platega.io"

PAYMENT_METHODS = {
    "sbp": 2,      # СБП / QR (НСПК)
    "card": 10,    # Карты МИР (CardRu)
    "intl": 12,    # Международный эквайринг
}


class PlategaError(RuntimeError):
    pass


class PlategaClient:
    """Async client for the Platega payment API.

    Credentials come from env vars PLATEGA_MERCHANT_ID and PLATEGA_API_KEY.
    """

    def __init__(
        self,
        merchant_id: str,
        api_key: str,
        return_url: str,
        failed_url: str = "",
    ) -> None:
        self._merchant_id = merchant_id
        self._api_key = api_key
        self._return_url = return_url
        self._failed_url = failed_url or return_url

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-MerchantId": self._merchant_id,
            "X-Secret": self._api_key,
        }

    async def create_payment(
        self,
        amount: int,
        description: str,
        internal_payload: str,
        payment_method: int = 2,
    ) -> dict[str, Any]:
        """Create a payment link on Platega.

        Returns:
            {
                "transaction_id": str,   # UUID — store as payload in payments table
                "redirect_url":   str,   # Send this URL to the user
                "status":         str,   # "PENDING"
            }
        Raises PlategaError on non-200 response or network failure.
        """
        transaction_id = str(uuid4())
        body = {
            "paymentMethod": payment_method,
            "id": transaction_id,
            "paymentDetails": {"amount": amount, "currency": "RUB"},
            "description": description,
            "return": self._return_url,
            "failedUrl": self._failed_url,
            "payload": internal_payload,
        }
        timeout = ClientTimeout(total=15)
        try:
            async with ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{PLATEGA_BASE}/transaction/process",
                    json=body,
                    headers=self._headers(),
                ) as resp:
                    data: Any = await resp.json(content_type=None)
                    if resp.status != 200:
                        msg = str((data or {}).get("message") or data)
                        raise PlategaError(f"Platega HTTP {resp.status}: {msg}")
                    logger.info(
                        "Platega payment created transaction_id=%s status=%s",
                        transaction_id, (data or {}).get("status"),
                    )
                    return {
                        "transaction_id": str((data or {}).get("transactionId") or transaction_id),
                        "redirect_url": str((data or {}).get("redirect") or ""),
                        "status": str((data or {}).get("status") or "PENDING"),
                    }
        except ClientError as exc:
            raise PlategaError(f"Platega network error: {exc}") from exc

    async def verify_payment_status(self, transaction_id: str) -> str:
        """Fetch the current status of a transaction directly from Platega.

        Used to verify webhook authenticity (Platega has no webhook signature).
        Returns one of: PENDING, CONFIRMED, EXPIRED, CANCELED, FAILED, UNKNOWN.
        """
        timeout = ClientTimeout(total=10)
        try:
            async with ClientSession(timeout=timeout) as session:
                async with session.get(
                    f"{PLATEGA_BASE}/transaction/{transaction_id}",
                    headers=self._headers(),
                ) as resp:
                    data: Any = await resp.json(content_type=None)
                    return str((data or {}).get("status") or "UNKNOWN")
        except ClientError as exc:
            logger.warning("Platega verify_payment_status failed transaction_id=%s error=%s",
                           transaction_id, exc)
            return "UNKNOWN"
