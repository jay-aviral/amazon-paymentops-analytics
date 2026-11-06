-- =====================================================================
-- 06_refunds_customer_process.sql
--   Q4: Which refunds are pending or delayed?
--   Q5: How often do payment failures affect orders?
--   Q7: What operational changes could reduce failures?
-- "As of" for ageing is the latest payment timestamp (v_as_of).
-- =====================================================================

-- @query: refund_headline
-- @desc: Refund pipeline scorecard.
SELECT COUNT(*)                                                            AS refunds,
       SUM(CASE WHEN refund_status = 'PENDING'   THEN 1 ELSE 0 END)        AS pending,
       SUM(CASE WHEN refund_status = 'PROCESSED' THEN 1 ELSE 0 END)        AS processed,
       SUM(CASE WHEN refund_status = 'FAILED'    THEN 1 ELSE 0 END)        AS failed,
       SUM(is_overdue_pending)                                             AS pending_overdue,
       ROUND(SUM(CASE WHEN refund_status = 'PENDING' THEN refund_amount ELSE 0 END), 2)  AS pending_value,
       ROUND(SUM(CASE WHEN is_overdue_pending = 1 THEN refund_amount ELSE 0 END), 2)     AS overdue_pending_value,
       SUM(is_late_processed)                                              AS processed_late,
       ROUND(100.0 * SUM(is_late_processed) / NULLIF(SUM(CASE WHEN refund_status = 'PROCESSED' THEN 1 ELSE 0 END), 0), 2) AS late_processed_pct,
       ROUND(SUM(CASE WHEN refund_status = 'FAILED' THEN refund_amount ELSE 0 END), 2)   AS failed_value
FROM v_refund_pipeline;

-- @query: refund_by_reason
-- @desc: Refund status and SLA performance by reason.
SELECT refund_reason,
       COUNT(*)                                                            AS refunds,
       SUM(CASE WHEN refund_status = 'PENDING' THEN 1 ELSE 0 END)          AS pending,
       SUM(is_overdue_pending)                                             AS pending_overdue,
       SUM(CASE WHEN refund_status = 'FAILED'  THEN 1 ELSE 0 END)          AS failed,
       SUM(is_late_processed)                                              AS processed_late,
       ROUND(100.0 * (SUM(is_overdue_pending) + SUM(is_late_processed)) / COUNT(*), 2) AS sla_breach_pct,
       ROUND(AVG(CASE WHEN refund_status = 'PROCESSED' THEN elapsed_days END), 2)      AS avg_days_to_process
FROM v_refund_pipeline
GROUP BY refund_reason
ORDER BY sla_breach_pct DESC;

-- @query: refund_by_method
-- @desc: Refund SLA performance by the original payment method.
SELECT method_name,
       MAX(sla_days)                                                       AS sla_days,
       COUNT(*)                                                            AS refunds,
       SUM(is_overdue_pending)                                             AS pending_overdue,
       SUM(is_late_processed)                                              AS processed_late,
       ROUND(100.0 * (SUM(is_overdue_pending) + SUM(is_late_processed)) / COUNT(*), 2) AS sla_breach_pct,
       ROUND(AVG(CASE WHEN refund_status = 'PROCESSED' THEN elapsed_days END), 2)      AS avg_days_to_process
FROM v_refund_pipeline
GROUP BY method_name
ORDER BY sla_breach_pct DESC;

-- @query: refund_by_gateway
-- @desc: Refund SLA performance by gateway.
SELECT gateway_name,
       COUNT(*)                                                            AS refunds,
       SUM(is_overdue_pending)                                             AS pending_overdue,
       SUM(is_late_processed)                                              AS processed_late,
       ROUND(100.0 * (SUM(is_overdue_pending) + SUM(is_late_processed)) / COUNT(*), 2) AS sla_breach_pct
FROM v_refund_pipeline
GROUP BY gateway_name
ORDER BY sla_breach_pct DESC;

-- @query: refund_pending_ageing
-- @desc: Pending refunds by how many days they are past their SLA due date (negative = still inside SLA).
SELECT bucket_no, bucket,
       COUNT(*)                                                            AS refunds,
       ROUND(SUM(refund_amount), 2)                                        AS pending_value
FROM (SELECT x.refund_amount,
             CASE WHEN x.days_over_sla <= 0  THEN 1 WHEN x.days_over_sla <= 7  THEN 2
                  WHEN x.days_over_sla <= 30 THEN 3 WHEN x.days_over_sla <= 90 THEN 4 ELSE 5 END AS bucket_no,
             CASE WHEN x.days_over_sla <= 0  THEN 'Within SLA'
                  WHEN x.days_over_sla <= 7  THEN '1-7 days overdue'
                  WHEN x.days_over_sla <= 30 THEN '8-30 days overdue'
                  WHEN x.days_over_sla <= 90 THEN '31-90 days overdue'
                  ELSE '90+ days overdue' END AS bucket
      FROM (SELECT r.refund_amount,
                   (UNIX_TIMESTAMP(a.as_of_ts) - UNIX_TIMESTAMP(r.refund_sla_due_ts)) / 86400.0 AS days_over_sla
            FROM v_refund_pipeline r CROSS JOIN v_as_of a
            WHERE r.refund_status = 'PENDING') x) y
GROUP BY bucket_no, bucket
ORDER BY bucket_no;

-- @query: refund_monthly
-- @desc: Refunds requested per month and share breaching SLA (late or still overdue).
SELECT SUBSTR(CAST(refund_requested_ts AS CHAR), 1, 7)                     AS month,
       COUNT(*)                                                            AS refunds,
       ROUND(SUM(refund_amount), 2)                                        AS refund_value,
       ROUND(100.0 * (SUM(is_overdue_pending) + SUM(is_late_processed)) / COUNT(*), 2) AS sla_breach_pct
FROM v_refund_pipeline
GROUP BY SUBSTR(CAST(refund_requested_ts AS CHAR), 1, 7)
ORDER BY month;

-- @query: refund_overdue_worklist
-- @desc: The 100 largest pending refunds that are past their SLA (collections-style work list).
SELECT refund_id, payment_id, order_id, refund_reason, method_name, gateway_name,
       refund_amount, refund_requested_ts, refund_sla_due_ts,
       ROUND(elapsed_days, 1)                                              AS days_pending
FROM v_refund_pipeline
WHERE is_overdue_pending = 1
ORDER BY refund_amount DESC, refund_id
LIMIT 100;

-- @query: cust_order_impact
-- @desc: How often do payment failures touch orders, and what happens next (recovered by retry vs abandoned)?
SELECT COUNT(*)                                                            AS orders,
       SUM(CASE WHEN failed_attempts > 0 THEN 1 ELSE 0 END)                AS orders_with_failed_attempt,
       ROUND(100.0 * SUM(CASE WHEN failed_attempts > 0 THEN 1 ELSE 0 END) / COUNT(*), 2) AS pct_orders_hit,
       SUM(CASE WHEN failed_attempts > 0 AND success_attempts > 0 THEN 1 ELSE 0 END) AS recovered_by_retry,
       SUM(CASE WHEN failed_attempts > 0 AND success_attempts = 0 THEN 1 ELSE 0 END) AS abandoned_after_failure,
       ROUND(100.0 * SUM(CASE WHEN failed_attempts > 0 AND success_attempts > 0 THEN 1 ELSE 0 END)
             / NULLIF(SUM(CASE WHEN failed_attempts > 0 THEN 1 ELSE 0 END), 0), 2) AS retry_recovery_pct,
       ROUND(SUM(CASE WHEN failed_attempts > 0 AND success_attempts = 0 THEN order_amount ELSE 0 END), 2) AS lost_order_value,
       ROUND(SUM(CASE WHEN pending_attempts > 0 AND success_attempts = 0 AND failed_attempts = 0 THEN order_amount ELSE 0 END), 2) AS pending_order_value
FROM v_order_payment_summary;

-- @query: cust_impact_by_segment
-- @desc: Order impact by Prime / non-Prime customers.
SELECT CASE WHEN c.is_prime = 1 THEN 'Prime' ELSE 'Non-Prime' END          AS segment,
       COUNT(*)                                                            AS orders,
       ROUND(100.0 * SUM(CASE WHEN s.failed_attempts > 0 THEN 1 ELSE 0 END) / COUNT(*), 2) AS pct_orders_hit,
       ROUND(100.0 * SUM(CASE WHEN s.failed_attempts > 0 AND s.success_attempts = 0 THEN 1 ELSE 0 END)
             / NULLIF(SUM(CASE WHEN s.failed_attempts > 0 THEN 1 ELSE 0 END), 0), 2) AS abandon_rate_after_failure_pct
FROM v_order_payment_summary s
JOIN customers c ON c.customer_id = s.customer_id
GROUP BY CASE WHEN c.is_prime = 1 THEN 'Prime' ELSE 'Non-Prime' END
ORDER BY segment;

-- @query: cust_recovery_by_first_failure
-- @desc: For orders that hit a failure: what kind of failure came first, and did the customer recover?
SELECT f.failure_category,
       COUNT(*)                                                            AS orders_hit,
       SUM(CASE WHEN s.success_attempts > 0 THEN 1 ELSE 0 END)             AS recovered,
       SUM(CASE WHEN s.success_attempts = 0 THEN 1 ELSE 0 END)             AS abandoned,
       ROUND(100.0 * SUM(CASE WHEN s.success_attempts > 0 THEN 1 ELSE 0 END) / COUNT(*), 2) AS recovery_pct,
       ROUND(SUM(CASE WHEN s.success_attempts = 0 THEN s.order_amount ELSE 0 END), 2) AS lost_order_value
FROM (SELECT order_id, failure_category
      FROM (SELECT order_id, failure_category,
                   ROW_NUMBER() OVER (PARTITION BY order_id ORDER BY attempt_no) AS rn
            FROM payments WHERE status = 'FAILED') ranked
      WHERE rn = 1) f
JOIN v_order_payment_summary s ON s.order_id = f.order_id
GROUP BY f.failure_category
ORDER BY lost_order_value DESC;

-- @query: cust_recovery_by_method
-- @desc: Retry recovery by the method used on the first failed attempt (do customers switch method?).
SELECT f.method_name,
       COUNT(*)                                                            AS orders_hit,
       ROUND(100.0 * SUM(CASE WHEN s.success_attempts > 0 THEN 1 ELSE 0 END) / COUNT(*), 2) AS recovery_pct,
       ROUND(SUM(CASE WHEN s.success_attempts = 0 THEN s.order_amount ELSE 0 END), 2)       AS lost_order_value
FROM (SELECT order_id, method_name
      FROM (SELECT order_id, method_name,
                   ROW_NUMBER() OVER (PARTITION BY order_id ORDER BY attempt_no) AS rn
            FROM v_payment_facts WHERE status = 'FAILED') ranked
      WHERE rn = 1) f
JOIN v_order_payment_summary s ON s.order_id = f.order_id
GROUP BY f.method_name
ORDER BY lost_order_value DESC;

-- @query: proc_failure_reduction_scenarios
-- @desc: Sizing of operational fixes. reduction_assumption_pct is an ASSUMPTION (edit it); value = lost order value x assumption.
SELECT failure_category,
       lost_order_value,
       reduction_assumption_pct,
       ROUND(lost_order_value * reduction_assumption_pct / 100.0, 2)       AS potential_recovered_value,
       proposed_change
FROM (SELECT f.failure_category,
             ROUND(SUM(CASE WHEN s.success_attempts = 0 THEN s.order_amount ELSE 0 END), 2) AS lost_order_value,
             CASE f.failure_category WHEN 'TECHNICAL' THEN 60 WHEN 'ISSUER_BANK' THEN 20
                                     WHEN 'CUSTOMER_ACTION' THEN 15 ELSE 0 END AS reduction_assumption_pct,
             CASE f.failure_category
               WHEN 'TECHNICAL'       THEN 'Smart routing + automatic failover to a healthy gateway; timeout tuning; incident alerting'
               WHEN 'ISSUER_BANK'     THEN 'Retry on alternate rail / issuer; 3DS2 frictionless; bank-level routing rules'
               WHEN 'CUSTOMER_ACTION' THEN 'Saved-method nudges, balance / OTP guidance, one-tap switch to UPI or wallet'
               ELSE 'Keep: fraud controls; tune rules only with risk team' END AS proposed_change
      FROM (SELECT order_id, failure_category
            FROM (SELECT order_id, failure_category,
                         ROW_NUMBER() OVER (PARTITION BY order_id ORDER BY attempt_no) AS rn
                  FROM payments WHERE status = 'FAILED') ranked
            WHERE rn = 1) f
      JOIN v_order_payment_summary s ON s.order_id = f.order_id
      GROUP BY f.failure_category) t
ORDER BY potential_recovered_value DESC;

-- @query: proc_gateway_routing_opportunity
-- @desc: If each method/gateway pair ran at the best gateway's failure rate for that method, how many failures disappear?
SELECT method_name, gateway_name, attempts, failed_attempts,
       ROUND(100.0 * failed_attempts / attempts, 2)                        AS failure_rate_pct,
       ROUND(100.0 * best_rate, 2)                                         AS best_gateway_rate_pct,
       CASE WHEN failed_attempts > attempts * best_rate
            THEN ROUND(failed_attempts - attempts * best_rate, 0) ELSE 0 END AS avoidable_failures
FROM (SELECT g.*,
             MIN(1.0 * failed_attempts / attempts) OVER (PARTITION BY method_name) AS best_rate
      FROM (SELECT method_name, gateway_name,
                   COUNT(*) AS attempts,
                   SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) AS failed_attempts
            FROM v_payment_facts
            GROUP BY method_name, gateway_name
            HAVING COUNT(*) >= 1000) g) z
ORDER BY avoidable_failures DESC;

-- @query: proc_sale_event_pressure
-- @desc: Failure rate on the busiest days vs the rest (capacity / scaling case).
SELECT CASE WHEN d.attempts >= t.p90 THEN 'Top-10% busiest days' ELSE 'Other days' END AS day_type,
       COUNT(*)                                                            AS days,
       ROUND(AVG(d.attempts), 0)                                           AS avg_attempts_per_day,
       ROUND(100.0 * SUM(d.failed) / SUM(d.attempts), 2)                   AS failure_rate_pct
FROM (SELECT payment_date, COUNT(*) AS attempts,
             SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) AS failed
      FROM payments GROUP BY payment_date) d
CROSS JOIN (SELECT MIN(attempts) AS p90
            FROM (SELECT attempts, NTILE(10) OVER (ORDER BY attempts DESC) AS tile
                  FROM (SELECT payment_date, COUNT(*) AS attempts FROM payments GROUP BY payment_date) q) r
            WHERE tile = 1) t
GROUP BY CASE WHEN d.attempts >= t.p90 THEN 'Top-10% busiest days' ELSE 'Other days' END
ORDER BY day_type DESC;
