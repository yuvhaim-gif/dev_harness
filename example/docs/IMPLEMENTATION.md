# Implementation Notes

## Billing service (`example/src/billing/`)

- `models.py` defines `PaymentRequest` (with `validate()`) and `PaymentResult`.
- `routes.py` exposes `create_payment(payload) -> (status_code, body)`, a
  framework-agnostic handler for `POST /payments`. It parses + validates the
  payload, then mints a unique `transaction_id`.

## Query layer (`example/src/db/`)

- `queries.py` provides a `FakeDB` that counts queries, plus two access
  patterns: `fetch_users_n_plus_one` (one query per id) and
  `fetch_users_batched` (a single batched query). The batched form is the
  optimisation target and must return the same rows, in request order.
