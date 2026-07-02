---
type: API Contract
title: POST /payments
description: Contract for creating a payment transaction in the billing service.
resource: /payments
tags: [billing, payments, contract]
---

# API Schema

## POST /payments

Create a payment transaction in the billing service.

### Request body (application/json)

| Field      | Type    | Required | Notes                                            |
|------------|---------|----------|--------------------------------------------------|
| `amount`   | integer | yes      | Positive amount in **minor units** (e.g. cents). |
| `currency` | string  | yes      | One of `USD`, `EUR`, `GBP`, `ILS`.               |
| `user_id`  | string  | yes      | Non-empty identifier of the paying user.         |

### Responses

**201 Created**

```json
{
  "transaction_id": "txn_<hex>",
  "amount": 1000,
  "currency": "USD",
  "user_id": "u_1",
  "status": "created"
}
```

**400 Bad Request**

```json
{ "error": "<human-readable validation message>" }
```

### Contract guarantees

- `transaction_id` is unique per call and prefixed with `txn_`.
- A malformed or incomplete body never raises; it returns `400` with an `error`.
- The response echoes the validated `amount`, `currency`, and `user_id`.
