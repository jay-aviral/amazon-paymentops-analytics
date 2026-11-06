-- =====================================================================
-- 05_reconciliation.sql | Q3: Do internal and gateway amounts match?
--                         Q6: What is the value of unresolved transactions?
-- Rules are defined once in v_reconciliation (02_views.sql).
--   unresolved = resolution_status IN ('OPEN','NO_REFUND_RAISED','REFUND_IN_PROGRESS')
-- =====================================================================

-- @query: recon_headline
-- @desc: Reconciliation scorecard: match rate, breaks, resolved vs unresolved value.
SELECT COUNT(*)                                                            AS payments_checked,
       SUM(CASE WHEN recon_status = 'MATCHED' THEN 1 ELSE 0 END)           AS matched,
       ROUND(100.0 * SUM(CASE WHEN recon_status = 'MATCHED' THEN 1 ELSE 0 END) / COUNT(*), 3) AS match_rate_pct,
       SUM(CASE WHEN severity <> 'NONE' THEN 1 ELSE 0 END)                 AS breaks,
       SUM(CASE WHEN resolution_status IN ('OPEN','NO_REFUND_RAISED','REFUND_IN_PROGRESS') THEN 1 ELSE 0 END) AS unresolved_items,
       ROUND(SUM(CASE WHEN resolution_status IN ('OPEN','NO_REFUND_RAISED','REFUND_IN_PROGRESS') THEN exposure_amount ELSE 0 END), 2) AS unresolved_value,
       ROUND(SUM(CASE WHEN resolution_status IN ('OPEN','NO_REFUND_RAISED','REFUND_IN_PROGRESS')
                       AND severity IN ('HIGH','MEDIUM') THEN exposure_amount ELSE 0 END), 2) AS unresolved_value_material,
       ROUND(SUM(CASE WHEN resolution_status = 'RESOLVED_REFUNDED' THEN exposure_amount ELSE 0 END), 2) AS resolved_value
FROM v_reconciliation;

-- @query: recon_by_status
-- @desc: Every break type: count, value, how much is still unresolved, and the recommended action.
SELECT recon_status, severity,
       COUNT(*)                                                            AS items,
       ROUND(SUM(exposure_amount), 2)                                      AS exposure_value,
       SUM(CASE WHEN resolution_status IN ('OPEN','NO_REFUND_RAISED','REFUND_IN_PROGRESS') THEN 1 ELSE 0 END) AS unresolved_items,
       ROUND(SUM(CASE WHEN resolution_status IN ('OPEN','NO_REFUND_RAISED','REFUND_IN_PROGRESS') THEN exposure_amount ELSE 0 END), 2) AS unresolved_value,
       CASE recon_status
         WHEN 'CAPTURED_NOT_CONFIRMED'    THEN 'Customer charged but order shows failed: auto-refund or force-confirm'
         WHEN 'DUPLICATE_CAPTURE'         THEN 'Customer double charged: refund the duplicate'
         WHEN 'SUCCESS_NOT_CAPTURED'      THEN 'Order shipped without captured money: chase gateway, recover'
         WHEN 'SUCCESS_NO_GATEWAY_RECORD' THEN 'Missing gateway record: request gateway report, add webhook retry'
         WHEN 'AMOUNT_MISMATCH'           THEN 'Amount differs beyond tolerance: investigate fee / currency / tampering'
         WHEN 'STUCK_PENDING'             THEN 'Pending over 24h: query gateway status API and close out'
         WHEN 'ROUNDING_DIFF'             THEN 'Within tolerance: auto-adjust, no manual work'
         ELSE 'None' END                                                   AS recommended_action
FROM v_reconciliation
WHERE recon_status NOT IN ('MATCHED', 'NOT_SENT_TO_GATEWAY', 'PENDING_WITHIN_SLA')
GROUP BY recon_status, severity
ORDER BY unresolved_value DESC, exposure_value DESC;

-- @query: recon_by_resolution
-- @desc: Breaks by resolution status (are refunds closing the loop?).
SELECT resolution_status,
       COUNT(*)                                                            AS items,
       ROUND(SUM(exposure_amount), 2)                                      AS exposure_value
FROM v_reconciliation
WHERE severity <> 'NONE'
GROUP BY resolution_status
ORDER BY exposure_value DESC;

-- @query: recon_by_gateway
-- @desc: Break rate and unresolved value per gateway.
SELECT gateway_name,
       COUNT(*)                                                            AS payments,
       SUM(CASE WHEN severity IN ('HIGH','MEDIUM') THEN 1 ELSE 0 END)      AS material_breaks,
       ROUND(10000.0 * SUM(CASE WHEN severity IN ('HIGH','MEDIUM') THEN 1 ELSE 0 END) / COUNT(*), 2) AS breaks_per_10k,
       ROUND(SUM(CASE WHEN resolution_status IN ('OPEN','NO_REFUND_RAISED','REFUND_IN_PROGRESS')
                       AND severity IN ('HIGH','MEDIUM') THEN exposure_amount ELSE 0 END), 2) AS unresolved_value
FROM v_reconciliation
GROUP BY gateway_name
ORDER BY breaks_per_10k DESC;

-- @query: recon_by_method
-- @desc: Break rate and unresolved value per payment method.
SELECT method_name,
       COUNT(*)                                                            AS payments,
       SUM(CASE WHEN severity IN ('HIGH','MEDIUM') THEN 1 ELSE 0 END)      AS material_breaks,
       ROUND(10000.0 * SUM(CASE WHEN severity IN ('HIGH','MEDIUM') THEN 1 ELSE 0 END) / COUNT(*), 2) AS breaks_per_10k,
       ROUND(SUM(CASE WHEN resolution_status IN ('OPEN','NO_REFUND_RAISED','REFUND_IN_PROGRESS')
                       AND severity IN ('HIGH','MEDIUM') THEN exposure_amount ELSE 0 END), 2) AS unresolved_value
FROM v_reconciliation
GROUP BY method_name
ORDER BY breaks_per_10k DESC;

-- @query: recon_monthly
-- @desc: Material breaks and unresolved value by payment month.
SELECT SUBSTR(payment_date, 1, 7)                                          AS month,
       COUNT(*)                                                            AS payments,
       SUM(CASE WHEN severity IN ('HIGH','MEDIUM') THEN 1 ELSE 0 END)      AS material_breaks,
       ROUND(SUM(CASE WHEN resolution_status IN ('OPEN','NO_REFUND_RAISED','REFUND_IN_PROGRESS')
                       AND severity IN ('HIGH','MEDIUM') THEN exposure_amount ELSE 0 END), 2) AS unresolved_value
FROM v_reconciliation
GROUP BY SUBSTR(payment_date, 1, 7)
ORDER BY month;

-- @query: recon_ageing
-- @desc: Ageing of unresolved material breaks (days since the payment).
SELECT bucket_no, age_bucket,
       COUNT(*)                                                            AS items,
       ROUND(SUM(exposure_amount), 2)                                      AS unresolved_value
FROM (SELECT r.exposure_amount,
             (UNIX_TIMESTAMP(a.as_of_ts) - UNIX_TIMESTAMP(r.payment_ts)) / 86400.0 AS age_days
      FROM v_reconciliation r CROSS JOIN v_as_of a
      WHERE r.resolution_status IN ('OPEN','NO_REFUND_RAISED','REFUND_IN_PROGRESS')
        AND r.severity IN ('HIGH','MEDIUM')) x
JOIN (SELECT 1 AS bucket_no, '0-7 days'    AS age_bucket, 0   AS lo, 7    AS hi UNION ALL
      SELECT 2, '8-30 days',   7,   30   UNION ALL
      SELECT 3, '31-90 days',  30,  90   UNION ALL
      SELECT 4, '91-180 days', 90,  180  UNION ALL
      SELECT 5, '180+ days',   180, 100000) b
  ON x.age_days >= b.lo AND x.age_days < b.hi
GROUP BY bucket_no, age_bucket
ORDER BY bucket_no;

-- @query: recon_top_breaks
-- @desc: The 100 largest unresolved material breaks (work list for the recon team).
SELECT payment_id, order_id, payment_ts, gateway_name, method_name,
       recon_status, severity, resolution_status,
       internal_status, internal_amount, gateway_status, gateway_amount,
       ROUND(exposure_amount, 2)                                           AS exposure_amount
FROM v_reconciliation
WHERE resolution_status IN ('OPEN','NO_REFUND_RAISED','REFUND_IN_PROGRESS')
  AND severity IN ('HIGH','MEDIUM')
ORDER BY exposure_amount DESC, payment_id
LIMIT 100;

-- @query: recon_customer_money_held
-- @desc: Customers charged by the gateway while the order shows no successful payment (needs a refund).
SELECT recon_status,
       COUNT(*)                                                            AS items,
       ROUND(SUM(exposure_amount), 2)                                      AS charged_value,
       ROUND(SUM(CASE WHEN resolution_status = 'RESOLVED_REFUNDED' THEN exposure_amount ELSE 0 END), 2) AS refunded_value,
       ROUND(SUM(CASE WHEN resolution_status <> 'RESOLVED_REFUNDED' THEN exposure_amount ELSE 0 END), 2) AS still_with_us_value
FROM v_reconciliation
WHERE recon_status IN ('CAPTURED_NOT_CONFIRMED', 'DUPLICATE_CAPTURE')
GROUP BY recon_status;

-- @query: recon_amount_mismatch_detail
-- @desc: Direction and size of amount mismatches beyond tolerance (gateway captured more or less than we booked).
SELECT CASE WHEN amount_diff > 0 THEN 'Gateway captured MORE' ELSE 'Gateway captured LESS' END AS direction,
       COUNT(*)                                                            AS items,
       ROUND(SUM(ABS(amount_diff)), 2)                                     AS total_abs_diff,
       ROUND(AVG(ABS(amount_diff)), 2)                                     AS avg_abs_diff,
       ROUND(MAX(ABS(amount_diff)), 2)                                     AS max_abs_diff
FROM v_reconciliation
WHERE recon_status = 'AMOUNT_MISMATCH'
GROUP BY CASE WHEN amount_diff > 0 THEN 'Gateway captured MORE' ELSE 'Gateway captured LESS' END;
