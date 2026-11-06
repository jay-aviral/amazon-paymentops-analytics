-- =====================================================================
-- 02_views.sql  |  Analytical views (MySQL 8; also runs on the SQLite test target)
--
-- Layering
--   v_as_of                 "now" for ageing = latest payment timestamp in the data
--   v_payment_facts         payments + method / customer / order attributes
--   v_order_payment_summary one row per order: attempts, failures, recovery
--   v_gateway_rollup        gateway rows collapsed to one row per payment
--   v_refund_resolution     refunds that resolve a reconciliation break
--   v_reconciliation_base   internal vs gateway comparison -> recon_status
--   v_reconciliation        + exposure amount, severity, resolution status
--   v_refund_pipeline       refunds with SLA / ageing flags
--
-- Reconciliation rules live ONLY here, so the dashboard, the Excel report and
-- ad-hoc SQL can never disagree. Thresholds (change here if the business does):
--   amount tolerance  : 1.00 currency units   (config.RECON_TOLERANCE)
--   stuck pending     : older than 24 hours   (config.PENDING_STUCK_HOURS = 86400 s)
-- =====================================================================

DROP VIEW IF EXISTS v_refund_pipeline;
DROP VIEW IF EXISTS v_reconciliation;
DROP VIEW IF EXISTS v_reconciliation_base;
DROP VIEW IF EXISTS v_refund_resolution;
DROP VIEW IF EXISTS v_gateway_rollup;
DROP VIEW IF EXISTS v_order_payment_summary;
DROP VIEW IF EXISTS v_payment_facts;
DROP VIEW IF EXISTS v_as_of;

CREATE VIEW v_as_of AS
SELECT MAX(payment_ts) AS as_of_ts FROM payments;

CREATE VIEW v_payment_facts AS
SELECT p.payment_id, p.order_id, p.customer_id, p.attempt_no,
       p.payment_ts, p.payment_date, p.payment_hour,
       m.method_name, m.method_type, p.amount, p.status,
       p.failure_code, p.failure_category, p.device_type, p.gateway_name,
       c.is_prime, c.city_tier, c.state, o.category, o.order_amount
FROM payments p
JOIN payment_methods m ON m.method_id = p.method_id
JOIN customers c       ON c.customer_id = p.customer_id
JOIN orders o          ON o.order_id = p.order_id;

CREATE VIEW v_order_payment_summary AS
SELECT o.order_id, o.customer_id, o.order_date, o.order_amount, o.category, o.order_status,
       COUNT(p.payment_id)                                         AS attempts,
       SUM(CASE WHEN p.status = 'SUCCESS' THEN 1 ELSE 0 END)       AS success_attempts,
       SUM(CASE WHEN p.status = 'FAILED'  THEN 1 ELSE 0 END)       AS failed_attempts,
       SUM(CASE WHEN p.status = 'PENDING' THEN 1 ELSE 0 END)       AS pending_attempts,
       MAX(CASE WHEN p.attempt_no = 1 THEN p.status END)           AS first_attempt_status
FROM orders o
LEFT JOIN payments p ON p.order_id = o.order_id
GROUP BY o.order_id, o.customer_id, o.order_date, o.order_amount, o.category, o.order_status;

CREATE VIEW v_gateway_rollup AS
SELECT g.payment_id,
       COUNT(*)                                                                 AS gateway_rows,
       SUM(CASE WHEN g.gateway_status = 'CAPTURED' THEN 1 ELSE 0 END)           AS captured_rows,
       SUM(CASE WHEN g.gateway_status = 'DECLINED' THEN 1 ELSE 0 END)           AS declined_rows,
       SUM(CASE WHEN g.gateway_status = 'CAPTURED' THEN g.gateway_amount ELSE 0 END) AS captured_total,
       MAX(CASE WHEN g.gateway_status = 'CAPTURED' THEN g.gateway_amount END)   AS captured_amount,
       SUM(g.fee_amount)                                                        AS fee_total
FROM gateway_transactions g
GROUP BY g.payment_id;

-- refunds whose purpose is to fix a reconciliation break
CREATE VIEW v_refund_resolution AS
SELECT r.payment_id,
       COUNT(*)                                                                     AS refund_rows,
       SUM(CASE WHEN r.refund_status = 'PROCESSED' THEN r.refund_amount ELSE 0 END) AS refunded_processed
FROM refunds r
WHERE r.refund_reason IN ('DUPLICATE_CHARGE', 'FAILED_PAYMENT_DEBIT')
GROUP BY r.payment_id;

CREATE VIEW v_reconciliation_base AS
SELECT p.payment_id, p.order_id, p.payment_ts, p.payment_date, p.method_id, m.method_name,
       p.gateway_name, p.status AS internal_status, p.amount AS internal_amount,
       CASE WHEN g.payment_id IS NULL THEN 'NONE'
            WHEN g.captured_rows > 0  THEN 'CAPTURED'
            WHEN g.declined_rows > 0  THEN 'DECLINED'
            ELSE 'TIMEOUT' END                                    AS gateway_status,
       g.captured_rows, g.captured_total, g.captured_amount       AS gateway_amount,
       CASE WHEN g.captured_rows > 0 THEN g.captured_amount - p.amount END AS amount_diff,
       CASE
         WHEN g.payment_id IS NULL AND p.status = 'SUCCESS' THEN 'SUCCESS_NO_GATEWAY_RECORD'
         WHEN g.payment_id IS NULL AND p.status = 'PENDING'
              AND (UNIX_TIMESTAMP(a.as_of_ts) - UNIX_TIMESTAMP(p.payment_ts)) > 86400 THEN 'STUCK_PENDING'
         WHEN g.payment_id IS NULL AND p.status = 'PENDING' THEN 'PENDING_WITHIN_SLA'
         WHEN g.payment_id IS NULL THEN 'NOT_SENT_TO_GATEWAY'
         WHEN g.captured_rows > 1 THEN 'DUPLICATE_CAPTURE'
         WHEN g.captured_rows = 1 AND p.status <> 'SUCCESS' THEN 'CAPTURED_NOT_CONFIRMED'
         WHEN p.status = 'SUCCESS' AND g.captured_rows = 0 THEN 'SUCCESS_NOT_CAPTURED'
         WHEN p.status = 'SUCCESS' AND ROUND(ABS(g.captured_amount - p.amount), 2) > 1.00 THEN 'AMOUNT_MISMATCH'
         WHEN p.status = 'SUCCESS' AND ROUND(ABS(g.captured_amount - p.amount), 2) > 0 THEN 'ROUNDING_DIFF'
         WHEN p.status = 'PENDING'
              AND (UNIX_TIMESTAMP(a.as_of_ts) - UNIX_TIMESTAMP(p.payment_ts)) > 86400 THEN 'STUCK_PENDING'
         WHEN p.status = 'PENDING' THEN 'PENDING_WITHIN_SLA'
         ELSE 'MATCHED'
       END AS recon_status
FROM payments p
JOIN payment_methods m ON m.method_id = p.method_id
LEFT JOIN v_gateway_rollup g ON g.payment_id = p.payment_id
CROSS JOIN v_as_of a;

CREATE VIEW v_reconciliation AS
SELECT e.*,
       CASE
         WHEN e.recon_status IN ('CAPTURED_NOT_CONFIRMED', 'DUPLICATE_CAPTURE') THEN
              CASE WHEN COALESCE(r.refunded_processed, 0) >= e.exposure_amount - 0.005 THEN 'RESOLVED_REFUNDED'
                   WHEN r.refund_rows IS NULL THEN 'NO_REFUND_RAISED'
                   ELSE 'REFUND_IN_PROGRESS' END
         WHEN e.recon_status IN ('MATCHED', 'NOT_SENT_TO_GATEWAY', 'PENDING_WITHIN_SLA') THEN 'NOT_APPLICABLE'
         ELSE 'OPEN'
       END AS resolution_status
FROM (
    SELECT b.*,
           CASE b.recon_status
             WHEN 'CAPTURED_NOT_CONFIRMED'    THEN b.captured_total
             WHEN 'DUPLICATE_CAPTURE'         THEN b.captured_total - b.gateway_amount
             WHEN 'SUCCESS_NOT_CAPTURED'      THEN b.internal_amount
             WHEN 'SUCCESS_NO_GATEWAY_RECORD' THEN b.internal_amount
             WHEN 'STUCK_PENDING'             THEN b.internal_amount
             WHEN 'AMOUNT_MISMATCH'           THEN ABS(b.amount_diff)
             WHEN 'ROUNDING_DIFF'             THEN ABS(b.amount_diff)
             ELSE 0
           END AS exposure_amount,
           CASE
             WHEN b.recon_status IN ('CAPTURED_NOT_CONFIRMED', 'DUPLICATE_CAPTURE', 'SUCCESS_NOT_CAPTURED') THEN 'HIGH'
             WHEN b.recon_status IN ('SUCCESS_NO_GATEWAY_RECORD', 'AMOUNT_MISMATCH', 'STUCK_PENDING') THEN 'MEDIUM'
             WHEN b.recon_status = 'ROUNDING_DIFF' THEN 'LOW'
             ELSE 'NONE'
           END AS severity
    FROM v_reconciliation_base b
) e
LEFT JOIN v_refund_resolution r ON r.payment_id = e.payment_id;

CREATE VIEW v_refund_pipeline AS
SELECT r.refund_id, r.payment_id, r.order_id, r.refund_amount, r.refund_reason, r.refund_status,
       r.refund_requested_ts, r.refund_sla_due_ts, r.refund_processed_ts,
       m.method_name, p.gateway_name,
       (UNIX_TIMESTAMP(r.refund_sla_due_ts) - UNIX_TIMESTAMP(r.refund_requested_ts)) / 86400.0 AS sla_days,
       CASE WHEN r.refund_status = 'PENDING' AND a.as_of_ts > r.refund_sla_due_ts THEN 1 ELSE 0 END AS is_overdue_pending,
       CASE WHEN r.refund_status = 'PROCESSED' AND r.refund_processed_ts > r.refund_sla_due_ts THEN 1 ELSE 0 END AS is_late_processed,
       CASE WHEN r.refund_status = 'PENDING'
                 THEN (UNIX_TIMESTAMP(a.as_of_ts) - UNIX_TIMESTAMP(r.refund_requested_ts)) / 86400.0
            ELSE (UNIX_TIMESTAMP(r.refund_processed_ts) - UNIX_TIMESTAMP(r.refund_requested_ts)) / 86400.0
       END AS elapsed_days
FROM refunds r
JOIN payments p        ON p.payment_id = r.payment_id
JOIN payment_methods m ON m.method_id = p.method_id
CROSS JOIN v_as_of a;
