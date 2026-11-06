-- =====================================================================
-- 03_payment_performance.sql | Q1: What percentage of payments succeed?
-- Rates are over ATTEMPTS (each row of payments). PENDING attempts count in
-- the denominator but are neither success nor failure.
-- Each query is introduced by "-- @query: name" so Python / Excel / Power BI
-- exports run exactly the same SQL.
-- =====================================================================

-- @query: kpi_headline
-- @desc: Headline payment KPIs for the whole period.
SELECT COUNT(*)                                                           AS attempts,
       SUM(CASE WHEN status = 'SUCCESS' THEN 1 ELSE 0 END)                AS success_attempts,
       SUM(CASE WHEN status = 'FAILED'  THEN 1 ELSE 0 END)                AS failed_attempts,
       SUM(CASE WHEN status = 'PENDING' THEN 1 ELSE 0 END)                AS pending_attempts,
       ROUND(100.0 * SUM(CASE WHEN status = 'SUCCESS' THEN 1 ELSE 0 END) / COUNT(*), 2) AS success_rate_pct,
       ROUND(100.0 * SUM(CASE WHEN status = 'FAILED'  THEN 1 ELSE 0 END) / COUNT(*), 2) AS failure_rate_pct,
       ROUND(SUM(CASE WHEN status = 'SUCCESS' THEN amount ELSE 0 END), 2) AS successful_value,
       ROUND(SUM(CASE WHEN status = 'FAILED'  THEN amount ELSE 0 END), 2) AS failed_value,
       ROUND(SUM(CASE WHEN status = 'PENDING' THEN amount ELSE 0 END), 2) AS pending_value
FROM payments;

-- @query: kpi_monthly_trend
-- @desc: Monthly attempts, success rate and failure rate.
SELECT SUBSTR(payment_date, 1, 7)                                          AS month,
       COUNT(*)                                                            AS attempts,
       ROUND(100.0 * SUM(CASE WHEN status = 'SUCCESS' THEN 1 ELSE 0 END) / COUNT(*), 2) AS success_rate_pct,
       ROUND(100.0 * SUM(CASE WHEN status = 'FAILED'  THEN 1 ELSE 0 END) / COUNT(*), 2) AS failure_rate_pct,
       ROUND(SUM(CASE WHEN status = 'SUCCESS' THEN amount ELSE 0 END), 2)  AS successful_value,
       ROUND(SUM(CASE WHEN status = 'FAILED'  THEN amount ELSE 0 END), 2)  AS failed_value
FROM payments
GROUP BY SUBSTR(payment_date, 1, 7)
ORDER BY month;

-- @query: kpi_daily_trend
-- @desc: Daily attempts and success rate (feeds the dashboard trend line; shows sale-day dips).
SELECT payment_date,
       COUNT(*)                                                            AS attempts,
       SUM(CASE WHEN status = 'SUCCESS' THEN 1 ELSE 0 END)                 AS success_attempts,
       SUM(CASE WHEN status = 'FAILED'  THEN 1 ELSE 0 END)                 AS failed_attempts,
       ROUND(100.0 * SUM(CASE WHEN status = 'SUCCESS' THEN 1 ELSE 0 END) / COUNT(*), 2) AS success_rate_pct
FROM payments
GROUP BY payment_date
ORDER BY payment_date;

-- @query: kpi_hourly_profile
-- @desc: Failure rate by hour of day (peak-load stress).
SELECT payment_hour,
       COUNT(*)                                                            AS attempts,
       ROUND(100.0 * SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) / COUNT(*), 2) AS failure_rate_pct
FROM payments
GROUP BY payment_hour
ORDER BY payment_hour;

-- @query: kpi_attempt_funnel
-- @desc: Success rate by attempt number - are retries converting?
SELECT attempt_no,
       COUNT(*)                                                            AS attempts,
       ROUND(100.0 * SUM(CASE WHEN status = 'SUCCESS' THEN 1 ELSE 0 END) / COUNT(*), 2) AS success_rate_pct
FROM payments
GROUP BY attempt_no
ORDER BY attempt_no;

-- @query: kpi_first_attempt_success
-- @desc: Share of orders paid successfully on the very first attempt, and share paid eventually.
SELECT COUNT(*)                                                            AS orders,
       ROUND(100.0 * SUM(CASE WHEN first_attempt_status = 'SUCCESS' THEN 1 ELSE 0 END) / COUNT(*), 2) AS first_attempt_success_pct,
       ROUND(100.0 * SUM(CASE WHEN success_attempts > 0 THEN 1 ELSE 0 END) / COUNT(*), 2)             AS eventual_success_pct
FROM v_order_payment_summary;
