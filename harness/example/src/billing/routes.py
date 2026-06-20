"""HTTP-framework-agnostic handler for ``POST /payments``."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from models import PaymentRequest, PaymentResult, ValidationError


def _parse(payload: Mapping[str, Any]) -> PaymentRequest:
    try:
        request = PaymentRequest(
            amount=payload["amount"],
            currency=payload["currency"],
            user_id=payload["user_id"],
        )
    except KeyError as exc:
        raise ValidationError(f"missing field: {exc.args[0]}") from exc
    request.validate()
    return request


def create_payment(payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    """Handle ``POST /payments``; return ``(status_code, body)``."""
    try:
        request = _parse(payload)
    except ValidationError as exc:
        return 400, {"error": str(exc)}

    result = PaymentResult(
        transaction_id=f"txn_{uuid.uuid4().hex}",
        amount=request.amount,
        currency=request.currency,
        user_id=request.user_id,
        status="created",
    )
    return 201, {
        "transaction_id": result.transaction_id,
        "amount": result.amount,
        "currency": result.currency,
        "user_id": result.user_id,
        "status": result.status,
    }
