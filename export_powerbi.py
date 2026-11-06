"""Export a Power BI-ready star schema as CSV (output/powerbi_data/).

Power BI can connect to MySQL directly (see powerbi/DASHBOARD_SPEC.md). These
CSVs are the no-database alternative and are intentionally *aggregated* so the
model stays fast: no 1M-row fact is needed for any dashboard visual.

    dim_date, dim_payment_method, dim_gateway
    fact_payments_daily   date x method x gateway x device x prime x status x failure_code
    fact_reconciliation   one row per payment that is not a clean MATCH (breaks + not-sent + pending)
    fact_refunds          one row per refund, with SLA flags
    fact_orders_daily     order_date x category x prime: orders, orders hit by failures, recovered, abandoned
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

import pandas as pd

from . import config, db

log = logging.getLogger("powerbi")

Q_PAYMENTS_DAILY = """
SELECT payment_date, method_name, gateway_name, device_type, is_prime, status,
       COALESCE(failure_code, 'NONE')          AS failure_code,
       COALESCE(failure_category, 'NONE')      AS failure_category,
       COUNT(*)                                AS attempts,
       ROUND(SUM(amount), 2)                   AS amount
FROM v_payment_facts
GROUP BY payment_date, method_name, gateway_name, device_type, is_prime, status,
         COALESCE(failure_code, 'NONE'), COALESCE(failure_category, 'NONE')
"""
Q_RECON = """
SELECT payment_id, order_id, payment_date, payment_ts, method_name, gateway_name,
       recon_status, severity, resolution_status, internal_status, gateway_status,
       internal_amount, gateway_amount, ROUND(exposure_amount, 2) AS exposure_amount,
       ROUND((UNIX_TIMESTAMP(a.as_of_ts) - UNIX_TIMESTAMP(payment_ts)) / 86400.0, 1) AS age_days
FROM v_reconciliation CROSS JOIN v_as_of a
WHERE recon_status <> 'MATCHED'
"""
Q_REFUNDS = """
SELECT refund_id, payment_id, order_id, refund_reason, refund_status, method_name, gateway_name,
       refund_amount, refund_requested_ts, refund_sla_due_ts, refund_processed_ts,
       ROUND(sla_days, 1) AS sla_days, ROUND(elapsed_days, 2) AS elapsed_days,
       is_overdue_pending, is_late_processed
FROM v_refund_pipeline
"""
Q_ORDERS_DAILY = """
SELECT s.order_date, s.category, c.is_prime,
       COUNT(*)                                                                     AS orders,
       ROUND(SUM(s.order_amount), 2)                                                AS order_value,
       SUM(CASE WHEN s.failed_attempts > 0 THEN 1 ELSE 0 END)                       AS orders_hit,
       SUM(CASE WHEN s.failed_attempts > 0 AND s.success_attempts > 0 THEN 1 ELSE 0 END) AS recovered_orders,
       SUM(CASE WHEN s.failed_attempts > 0 AND s.success_attempts = 0 THEN 1 ELSE 0 END) AS abandoned_orders,
       ROUND(SUM(CASE WHEN s.failed_attempts > 0 AND s.success_attempts = 0 THEN s.order_amount ELSE 0 END), 2) AS lost_order_value
FROM v_order_payment_summary s JOIN customers c ON c.customer_id = s.customer_id
GROUP BY s.order_date, s.category, c.is_prime
"""


def export(conn, out: Path) -> dict[str, int]:
    out.mkdir(parents=True, exist_ok=True)
    frames = {
        "fact_payments_daily": db.fetch_df(conn, Q_PAYMENTS_DAILY),
        "fact_reconciliation": db.fetch_df(conn, Q_RECON),
        "fact_refunds": db.fetch_df(conn, Q_REFUNDS),
        "fact_orders_daily": db.fetch_df(conn, Q_ORDERS_DAILY),
        "dim_payment_method": db.fetch_df(conn, "SELECT * FROM payment_methods"),
    }
    gw = db.fetch_df(conn, "SELECT DISTINCT gateway_name FROM payments ORDER BY gateway_name")
    gw["gateway_key"] = range(1, len(gw) + 1)
    frames["dim_gateway"] = gw
    d = frames["fact_payments_daily"]["payment_date"].astype(str)
    dates = pd.date_range(d.min(), d.max(), freq="D")
    frames["dim_date"] = pd.DataFrame({
        "date": dates.strftime("%Y-%m-%d"), "year": dates.year, "month_no": dates.month,
        "month": dates.strftime("%Y-%m"), "month_name": dates.strftime("%b %Y"),
        "week_of_year": dates.isocalendar().week.to_numpy(), "day_name": dates.strftime("%a"),
        "is_weekend": (dates.dayofweek >= 5).astype(int)})
    counts = {}
    for name, df in frames.items():
        df.to_csv(out / f"{name}.csv", index=False)
        counts[name] = len(df)
        log.info("wrote %-22s %9s rows", name, f"{len(df):,}")
    return counts


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--target", choices=["sqlite", "mysql"], default="sqlite")
    ap.add_argument("--out", default=str(config.OUTPUT))
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    out = Path(a.out)
    conn = db.connect(a.target, path=out / "paymentops.db")
    try:
        export(conn, out / "powerbi_data")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
