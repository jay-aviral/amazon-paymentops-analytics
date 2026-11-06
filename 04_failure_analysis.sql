-- =====================================================================
-- 04_failure_analysis.sql | Q2: Which methods (and gateways, devices, hours...)
-- have the highest failure rates, and why?
-- =====================================================================

-- @query: fail_by_method
-- @desc: Failure rate by payment method, ranked.
SELECT method_name,
       COUNT(*)                                                            AS attempts,
       SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END)                  AS failed_attempts,
       ROUND(100.0 * SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) / COUNT(*), 2) AS failure_rate_pct,
       ROUND(SUM(CASE WHEN status = 'FAILED' THEN amount ELSE 0 END), 2)   AS failed_value
FROM v_payment_facts
GROUP BY method_name
ORDER BY failure_rate_pct DESC;

-- @query: fail_by_gateway
-- @desc: Failure rate by gateway.
SELECT gateway_name,
       COUNT(*)                                                            AS attempts,
       SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END)                  AS failed_attempts,
       ROUND(100.0 * SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) / COUNT(*), 2) AS failure_rate_pct,
       ROUND(SUM(CASE WHEN status = 'FAILED' THEN amount ELSE 0 END), 2)   AS failed_value
FROM payments
GROUP BY gateway_name
ORDER BY failure_rate_pct DESC;

-- @query: fail_by_device
-- @desc: Failure rate by device type.
SELECT device_type,
       COUNT(*)                                                            AS attempts,
       ROUND(100.0 * SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) / COUNT(*), 2) AS failure_rate_pct
FROM payments
GROUP BY device_type
ORDER BY failure_rate_pct DESC;

-- @query: fail_by_ticket_band
-- @desc: Failure rate by order-value band (large tickets fail more).
SELECT band_no, ticket_band,
       COUNT(*)                                                            AS attempts,
       ROUND(100.0 * SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) / COUNT(*), 2) AS failure_rate_pct
FROM (SELECT status,
             CASE WHEN amount < 500   THEN 1 WHEN amount < 2000  THEN 2
                  WHEN amount < 5000  THEN 3 WHEN amount < 15000 THEN 4 ELSE 5 END AS band_no,
             CASE WHEN amount < 500   THEN '1. under 500'
                  WHEN amount < 2000  THEN '2. 500 - 1,999'
                  WHEN amount < 5000  THEN '3. 2,000 - 4,999'
                  WHEN amount < 15000 THEN '4. 5,000 - 14,999'
                  ELSE '5. 15,000 and above' END AS ticket_band
      FROM payments) x
GROUP BY band_no, ticket_band
ORDER BY band_no;

-- @query: fail_by_customer_segment
-- @desc: Failure rate by Prime status and city tier.
SELECT CASE WHEN is_prime = 1 THEN 'Prime' ELSE 'Non-Prime' END           AS segment,
       city_tier,
       COUNT(*)                                                            AS attempts,
       ROUND(100.0 * SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) / COUNT(*), 2) AS failure_rate_pct
FROM v_payment_facts
GROUP BY CASE WHEN is_prime = 1 THEN 'Prime' ELSE 'Non-Prime' END, city_tier
ORDER BY segment, city_tier;

-- @query: fail_method_gateway_matrix
-- @desc: Method x gateway failure rate (spot bad routing combinations).
SELECT method_name, gateway_name,
       COUNT(*)                                                            AS attempts,
       ROUND(100.0 * SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) / COUNT(*), 2) AS failure_rate_pct
FROM v_payment_facts
GROUP BY method_name, gateway_name
ORDER BY method_name, gateway_name;

-- @query: fail_reason_pareto
-- @desc: Failure reasons ranked with cumulative share (Pareto).
SELECT failure_code, failure_category, failed_attempts,
       ROUND(100.0 * failed_attempts / total_failed, 2)                    AS share_pct,
       ROUND(100.0 * SUM(failed_attempts) OVER (ORDER BY failed_attempts DESC, failure_code
                                                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) / total_failed, 2) AS cumulative_share_pct,
       failed_value
FROM (SELECT failure_code, failure_category,
             COUNT(*)                          AS failed_attempts,
             ROUND(SUM(amount), 2)             AS failed_value,
             (SELECT COUNT(*) FROM payments WHERE status = 'FAILED') AS total_failed
      FROM payments
      WHERE status = 'FAILED'
      GROUP BY failure_code, failure_category) f
ORDER BY failed_attempts DESC, failure_code;

-- @query: fail_category_by_method
-- @desc: Failure category mix inside each method (which kind of failure drives each method).
SELECT method_name, failure_category,
       COUNT(*)                                                            AS failed_attempts,
       ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (PARTITION BY method_name), 2) AS share_of_method_failures_pct
FROM v_payment_facts
WHERE status = 'FAILED'
GROUP BY method_name, failure_category
ORDER BY method_name, failed_attempts DESC;

-- @query: fail_monthly_by_method
-- @desc: Monthly failure rate per method (trend / sale-month pressure).
SELECT SUBSTR(payment_date, 1, 7)                                          AS month,
       method_name,
       COUNT(*)                                                            AS attempts,
       ROUND(100.0 * SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) / COUNT(*), 2) AS failure_rate_pct
FROM v_payment_facts
GROUP BY SUBSTR(payment_date, 1, 7), method_name
ORDER BY month, method_name;

-- @query: fail_incident_hours
-- @desc: Gateway-hours whose failure rate is at least 3x that gateway's norm (outage / incident detector).
SELECT h.gateway_name, h.payment_date, h.payment_hour,
       h.attempts, h.failed_attempts,
       ROUND(100.0 * h.failed_attempts / h.attempts, 2)                    AS failure_rate_pct,
       ROUND(100.0 * b.base_rate, 2)                                       AS gateway_baseline_pct,
       ROUND(h.failed_attempts - h.attempts * b.base_rate, 0)              AS excess_failures
FROM (SELECT gateway_name, payment_date, payment_hour,
             COUNT(*) AS attempts,
             SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) AS failed_attempts
      FROM payments
      GROUP BY gateway_name, payment_date, payment_hour) h
JOIN (SELECT gateway_name,
             1.0 * SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) / COUNT(*) AS base_rate
      FROM payments GROUP BY gateway_name) b ON b.gateway_name = h.gateway_name
WHERE h.attempts >= 30
  AND 1.0 * h.failed_attempts / h.attempts >= 3 * b.base_rate
ORDER BY excess_failures DESC
LIMIT 25;

-- @query: fail_incident_by_method_hour
-- @desc: Method-hours at least 3x that method's norm (finds issuer / method-level incidents).
SELECT h.method_name, h.payment_date, h.payment_hour,
       h.attempts, h.failed_attempts,
       ROUND(100.0 * h.failed_attempts / h.attempts, 2)                    AS failure_rate_pct,
       ROUND(100.0 * b.base_rate, 2)                                       AS method_baseline_pct,
       ROUND(h.failed_attempts - h.attempts * b.base_rate, 0)              AS excess_failures
FROM (SELECT method_name, payment_date, payment_hour,
             COUNT(*) AS attempts,
             SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) AS failed_attempts
      FROM v_payment_facts
      GROUP BY method_name, payment_date, payment_hour) h
JOIN (SELECT method_name,
             1.0 * SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) / COUNT(*) AS base_rate
      FROM v_payment_facts GROUP BY method_name) b ON b.method_name = h.method_name
WHERE h.attempts >= 10
  AND 1.0 * h.failed_attempts / h.attempts >= 3 * b.base_rate
ORDER BY excess_failures DESC
LIMIT 25;
