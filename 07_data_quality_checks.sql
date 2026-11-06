-- =====================================================================
-- 07_data_quality_checks.sql | post-load integrity gate
-- Every query returns one number, issue_count, and every one must be 0.
-- The ETL runs these after loading and fails the run if any is non-zero
-- (use --no-strict to report instead of fail).
-- =====================================================================

-- @query: dq_orphan_orders
-- @desc: Orders whose customer does not exist.
SELECT COUNT(*) AS issue_count
FROM orders o LEFT JOIN customers c ON c.customer_id = o.customer_id
WHERE c.customer_id IS NULL;

-- @query: dq_orphan_payments
-- @desc: Payments whose order does not exist.
SELECT COUNT(*) AS issue_count
FROM payments p LEFT JOIN orders o ON o.order_id = p.order_id
WHERE o.order_id IS NULL;

-- @query: dq_orphan_gateway_rows
-- @desc: Gateway rows whose payment does not exist.
SELECT COUNT(*) AS issue_count
FROM gateway_transactions g LEFT JOIN payments p ON p.payment_id = g.payment_id
WHERE p.payment_id IS NULL;

-- @query: dq_orphan_refunds
-- @desc: Refunds whose payment does not exist.
SELECT COUNT(*) AS issue_count
FROM refunds r LEFT JOIN payments p ON p.payment_id = r.payment_id
WHERE p.payment_id IS NULL;

-- @query: dq_payment_customer_mismatch
-- @desc: Payment customer differs from the customer on the order.
SELECT COUNT(*) AS issue_count
FROM payments p JOIN orders o ON o.order_id = p.order_id
WHERE p.customer_id <> o.customer_id;

-- @query: dq_payment_before_order
-- @desc: Payment timestamp earlier than the order timestamp.
SELECT COUNT(*) AS issue_count
FROM payments p JOIN orders o ON o.order_id = p.order_id
WHERE p.payment_ts < o.order_ts;

-- @query: dq_gateway_name_mismatch
-- @desc: Gateway named on the transaction differs from the routing gateway on the payment.
SELECT COUNT(*) AS issue_count
FROM gateway_transactions g JOIN payments p ON p.payment_id = g.payment_id
WHERE g.gateway_name <> p.gateway_name;

-- @query: dq_orders_without_payment
-- @desc: Orders with no payment attempt at all.
SELECT COUNT(*) AS issue_count
FROM orders o LEFT JOIN payments p ON p.order_id = o.order_id
WHERE p.payment_id IS NULL;

-- @query: dq_multiple_success_per_order
-- @desc: Orders with more than one successful payment (a customer paid twice).
SELECT COUNT(*) AS issue_count
FROM (SELECT order_id FROM payments WHERE status = 'SUCCESS'
      GROUP BY order_id HAVING COUNT(*) > 1) x;

-- @query: dq_attempt_sequence_gaps
-- @desc: Orders whose attempt numbers are not 1..n without gaps.
SELECT COUNT(*) AS issue_count
FROM (SELECT order_id FROM payments GROUP BY order_id
      HAVING COUNT(*) <> MAX(attempt_no)) x;

-- @query: dq_order_status_vs_payments
-- @desc: Order status contradicts its payments (paid status without a success, or failed status with one).
SELECT COUNT(*) AS issue_count
FROM v_order_payment_summary s
WHERE (s.success_attempts > 0 AND s.order_status IN ('PAYMENT_FAILED', 'PAYMENT_PENDING'))
   OR (s.success_attempts = 0 AND s.order_status NOT IN ('PAYMENT_FAILED', 'PAYMENT_PENDING'));

-- @query: dq_refund_before_payment
-- @desc: Refund requested before the payment it refunds.
SELECT COUNT(*) AS issue_count
FROM refunds r JOIN payments p ON p.payment_id = r.payment_id
WHERE r.refund_requested_ts < p.payment_ts;
