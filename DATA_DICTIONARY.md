# Data dictionary

All amounts are INR. All data is simulated.

| Table | Grain | Key columns | Notes |
|---|---|---|---|
| `payment_methods` | one row per method | method_id | fee %, settlement days and refund SLA days per method |
| `customers` | one row per customer who ordered | customer_id | state, city_tier (Tier-1/2/3/Unknown), is_prime |
| `orders` | one row per order | order_id → customer_id | order_status: DELIVERED, IN_TRANSIT, CANCELLED, RETURNED, PAYMENT_FAILED, PAYMENT_PENDING |
| `payments` | one row per payment **attempt** (1,000,000) | payment_id → order_id, method_id | attempt_no 1-5, status SUCCESS/FAILED/PENDING, failure_code and failure_category for FAILED, gateway_name |
| `gateway_transactions` | gateway's view of a payment | gateway_txn_id → payment_id | status CAPTURED/DECLINED/TIMEOUT, fee, settlement date. **payment_id is not unique:** a duplicate capture has two rows |
| `refunds` | one row per refund request | refund_id → payment_id, order_id | status PENDING/PROCESSED/FAILED, SLA due time, reason |

## Failure categories

| Category | Codes | Meaning |
|---|---|---|
| CUSTOMER_ACTION | INSUFFICIENT_FUNDS, UPI_PIN_INCORRECT, INVALID_OTP, CARD_EXPIRED | customer can fix |
| ISSUER_BANK | BANK_DECLINED, ISSUER_TIMEOUT, 3DS_AUTH_FAILED | issuing bank / authentication |
| TECHNICAL | GATEWAY_TIMEOUT, NETWORK_ERROR | our or gateway infrastructure |
| RISK | RISK_REJECTED | fraud engine; never reaches the gateway |

## Reconciliation statuses (view `v_reconciliation`)

| Status | Severity | Meaning |
|---|---|---|
| MATCHED | none | internal and gateway agree |
| ROUNDING_DIFF | LOW | success, amount gap within INR 1 |
| AMOUNT_MISMATCH | MEDIUM | success, amount gap above INR 1 |
| SUCCESS_NO_GATEWAY_RECORD | MEDIUM | we say success, gateway has nothing |
| STUCK_PENDING | MEDIUM | pending more than 24 h, no gateway outcome |
| SUCCESS_NOT_CAPTURED | HIGH | we say success, gateway did not capture |
| CAPTURED_NOT_CONFIRMED | HIGH | gateway captured, we say not success (customer charged) |
| DUPLICATE_CAPTURE | HIGH | gateway captured twice (customer double-charged) |
| NOT_SENT_TO_GATEWAY / PENDING_WITHIN_SLA | none | expected (risk reject / recent pending) |

Resolution: `RESOLVED_REFUNDED`, `REFUND_IN_PROGRESS`, `NO_REFUND_RAISED`, `OPEN`, `NOT_APPLICABLE`.
"Unresolved" = OPEN + NO_REFUND_RAISED + REFUND_IN_PROGRESS.

## ETL rules (see `output/data_quality_report.csv`)

DUPLICATE_EXACT, DUPLICATE_KEY_CONFLICT, ORPHAN_*, INVALID_AMOUNT, FUTURE_TIMESTAMP, PAYMENT_BEFORE_ORDER, REFUND_EXCEEDS_PAYMENT,
plus standardisation of case variants, amount text, alternative timestamp formats and missing city tier.
