"""Contract tests for the POST /payments handler (docs/API_SCHEMA.md)."""

from __future__ import annotations

from routes import create_payment


def test_create_payment_success() -> None:
    status, body = create_payment({"amount": 1000, "currency": "USD", "user_id": "u_1"})
    assert status == 201
    assert body["transaction_id"].startswith("txn_")
    assert body["amount"] == 1000
    assert body["currency"] == "USD"
    assert body["user_id"] == "u_1"
    assert body["status"] == "created"


def test_create_payment_rejects_non_positive_amount() -> None:
    status, body = create_payment({"amount": 0, "currency": "USD", "user_id": "u_1"})
    assert status == 400
    assert "amount" in body["error"]


def test_create_payment_rejects_unknown_currency() -> None:
    status, body = create_payment({"amount": 100, "currency": "XYZ", "user_id": "u_1"})
    assert status == 400
    assert "currency" in body["error"]


def test_create_payment_requires_user_id() -> None:
    status, body = create_payment({"amount": 100, "currency": "USD"})
    assert status == 400
    assert "user_id" in body["error"]


def test_transaction_ids_are_unique() -> None:
    first = create_payment({"amount": 1, "currency": "USD", "user_id": "u"})[1]
    second = create_payment({"amount": 1, "currency": "USD", "user_id": "u"})[1]
    assert first["transaction_id"] != second["transaction_id"]
