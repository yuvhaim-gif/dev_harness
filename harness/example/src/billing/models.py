"""Billing domain models for the payments endpoint."""

from __future__ import annotations

from dataclasses import dataclass

SUPPORTED_CURRENCIES: frozenset[str] = frozenset({"USD", "EUR", "GBP", "ILS"})


class ValidationError(ValueError):
    """Raised when a payment request fails validation."""


@dataclass(frozen=True)
class PaymentRequest:
    amount: int
    currency: str
    user_id: str

    def validate(self) -> None:
        if not isinstance(self.amount, int) or isinstance(self.amount, bool) or self.amount <= 0:
            raise ValidationError("amount must be a positive integer in minor units")
        if self.currency not in SUPPORTED_CURRENCIES:
            raise ValidationError(f"unsupported currency: {self.currency!r}")
        if not self.user_id:
            raise ValidationError("user_id is required")


@dataclass(frozen=True)
class PaymentResult:
    transaction_id: str
    amount: int
    currency: str
    user_id: str
    status: str
