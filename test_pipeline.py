"""End-to-end tests. They run the real generator, ETL, SQL, report and export on a small seeded dataset."""
import re
from pathlib import Path

import numpy as np
import openpyxl
import pandas as pd

from src import build_report, config, db, export_powerbi, generate_data

TABLES = ["customers", "orders", "payments", "gateway_transactions", "refunds"]


# ---------------------------------------------------------------- generator
def test_generator_row_count_and_determinism(tmp_path, pipeline):
    assert len(pd.read_csv(pipeline["truth"] / "payments.csv")) == 40_000
    generate_data.generate(3_000, 800, 11, out=tmp_path / "a")
    generate_data.generate(3_000, 800, 11, out=tmp_path / "b")
    for t in TABLES:
        assert (tmp_path / "a" / f"{t}.csv").read_bytes() == (tmp_path / "b" / f"{t}.csv").read_bytes(), t


def test_raw_data_is_actually_dirty(pipeline):
    raw = pd.read_csv(pipeline["raw"] / "payments.csv", dtype=str)
    truth = pd.read_csv(pipeline["truth"] / "payments.csv", dtype=str)
    assert len(raw) > len(truth)
    assert raw["payment_id"].duplicated().any()
    assert set(raw["status"].dropna()) - {"SUCCESS", "FAILED", "PENDING"}


# --------------------------------------------------------------------- ETL
def test_etl_recovers_ground_truth(pipeline):
    for t in TABLES:
        truth = pd.read_csv(pipeline["truth"] / f"{t}.csv", dtype=str)
        clean = pd.read_csv(pipeline["clean"] / f"{t}.csv", dtype=str)
        key = truth.columns[0]
        assert sorted(truth[key]) == sorted(clean[key]), f"{t}: key sets differ"
    p_t = pd.read_csv(pipeline["truth"] / "payments.csv").set_index("payment_id")
    p_c = pd.read_csv(pipeline["clean"] / "payments.csv").set_index("payment_id").loc[p_t.index]
    assert np.allclose(p_t["amount"], p_c["amount"])
    assert (p_t["status"] == p_c["status"]).all()


def test_etl_rules_fire_and_rejects_are_kept(pipeline):
    s = pipeline["summary"]["tables"]["payments"]["rejects_by_rule"]
    for rule in ("DUPLICATE_EXACT", "INVALID_AMOUNT", "ORPHAN_ORDER", "FUTURE_TIMESTAMP",
                 "PAYMENT_BEFORE_ORDER", "DUPLICATE_KEY_CONFLICT"):
        assert s.get(rule, 0) > 0, rule
    rejects = pd.read_csv(pipeline["out"] / "rejects" / "payments_rejects.csv")
    assert len(rejects) == pipeline["summary"]["tables"]["payments"]["rejected_rows"]


def test_post_load_verification_clean(pipeline):
    v = pipeline["summary"]["verification"]
    assert v["problems"] == []
    assert all(n == 0 for n in v["integrity"].values())
    assert all(c["dataframe"] == c["database"] for c in v["row_counts"].values())


# ---------------------------------------------------------------- reconciliation
def test_reconciliation_matches_independent_pandas_logic(pipeline, conn):
    pay = pd.read_csv(pipeline["clean"] / "payments.csv")
    gw = pd.read_csv(pipeline["clean"] / "gateway_transactions.csv")
    cap = gw[gw.gateway_status == "CAPTURED"].groupby("payment_id").agg(n=("gateway_txn_id", "size"),
                                                                       amt=("gateway_amount", "max"))
    m = pay.merge(cap, left_on="payment_id", right_index=True, how="left")
    has_gw = pay.payment_id.isin(gw.payment_id)
    n = m["n"].fillna(0)
    expected = {
        "DUPLICATE_CAPTURE": int((n > 1).sum()),
        "CAPTURED_NOT_CONFIRMED": int(((n == 1) & (m.status != "SUCCESS")).sum()),
        "SUCCESS_NOT_CAPTURED": int(((m.status == "SUCCESS") & has_gw & (n == 0)).sum()),
        "SUCCESS_NO_GATEWAY_RECORD": int(((m.status == "SUCCESS") & ~has_gw).sum()),
        "AMOUNT_MISMATCH": int(((n == 1) & (m.status == "SUCCESS") & ((m.amt - m.amount).abs().round(2) > 1.0)).sum()),
    }
    got = db.fetch_df(conn, "SELECT recon_status, COUNT(*) AS n FROM v_reconciliation GROUP BY recon_status")
    got = dict(zip(got.recon_status, got.n))
    for status, cnt in expected.items():
        assert got.get(status, 0) == cnt, f"{status}: sql={got.get(status, 0)} pandas={cnt}"
    assert expected["DUPLICATE_CAPTURE"] > 0 and expected["AMOUNT_MISMATCH"] > 0


def test_recon_statuses_partition_all_payments(conn):
    total = db.fetch_df(conn, "SELECT COUNT(*) n FROM v_reconciliation").n[0]
    assert total == db.table_count(conn, "payments")
    bad = db.fetch_df(conn, "SELECT COUNT(*) n FROM v_reconciliation WHERE exposure_amount < 0 "
                            "OR (severity <> 'NONE' AND exposure_amount = 0 AND recon_status NOT IN ('ROUNDING_DIFF'))").n[0]
    assert bad == 0


# ------------------------------------------------------------ analysis queries
def test_every_named_query_runs(conn):
    qs = db.all_queries()
    assert len(qs) >= 40
    for name, spec in qs.items():
        if name.startswith("dq_"):
            continue
        df = db.fetch_df(conn, spec["sql"])
        assert len(df) > 0, f"{name} returned no rows"


def test_kpi_consistency(conn):
    k = db.run_query(conn, "kpi_headline").iloc[0]
    assert k.success_attempts + k.failed_attempts + k.pending_attempts == k.attempts == 40_000
    assert abs(k.success_rate_pct + k.failure_rate_pct - 100 * (1 - k.pending_attempts / k.attempts)) < 0.02
    assert db.run_query(conn, "fail_by_method").attempts.sum() == k.attempts
    assert db.run_query(conn, "fail_by_gateway").failed_attempts.sum() == k.failed_attempts
    assert db.run_query(conn, "fail_reason_pareto").failed_attempts.sum() == k.failed_attempts
    assert db.run_query(conn, "fail_reason_pareto").cumulative_share_pct.iloc[-1] == 100.0


def test_cross_query_ties(conn):
    ci = db.run_query(conn, "cust_order_impact").iloc[0]
    by_cat = db.run_query(conn, "cust_recovery_by_first_failure")
    assert abs(by_cat.lost_order_value.sum() - ci.lost_order_value) < 0.5
    assert by_cat.orders_hit.sum() == ci.orders_with_failed_attempt
    rf = db.run_query(conn, "refund_headline").iloc[0]
    assert rf.refunds == db.table_count(conn, "refunds")
    assert rf.pending + rf.processed + rf.failed == rf.refunds
    assert db.run_query(conn, "refund_by_reason").refunds.sum() == rf.refunds
    rh = db.run_query(conn, "recon_headline").iloc[0]
    assert abs(db.run_query(conn, "recon_by_status").unresolved_value.sum() - (
        rh.unresolved_value)) < 1.0
    assert abs(db.run_query(conn, "refund_pending_ageing").pending_value.sum() - rf.pending_value) < 0.5


def test_seeded_incidents_are_detected(conn, pipeline):
    inc = db.run_query(conn, "fail_incident_hours")
    top = inc.iloc[0]
    assert top.gateway_name == "PayGate-B" and str(top.payment_date) == "2026-07-15"
    assert 14 <= top.payment_hour <= 17 and top.failure_rate_pct > 25


def test_sql_is_portable_between_sqlite_and_mysql():
    banned = re.compile(r"strftime|julianday|datetime\(|ifnull\(|\|\||group_concat|date\(|printf\(", re.I)
    for f in sorted(config.SQL_DIR.glob("*.sql")):
        body = "\n".join(l for l in f.read_text().splitlines() if not l.strip().startswith("--"))
        assert not banned.search(body), f"{f.name} uses a non-portable function"


# ------------------------------------------------------------------ outputs
def test_excel_report(pipeline, conn, tmp_path):
    d = build_report.load_all(conn)
    path = build_report.build(d, pd.read_csv(pipeline["out"] / "data_quality_report.csv"),
                              "2026-10-31", tmp_path / "r.xlsx")
    wb = openpyxl.load_workbook(path)
    for sheet in ("Summary", "Payment Performance", "Failures", "Reconciliation", "Refunds",
                  "Customer Impact", "Recommendations", "Work Lists", "Data Quality", "Definitions"):
        assert sheet in wb.sheetnames
    rec = wb["Recommendations"]
    formulas = [c.value for row in rec.iter_rows() for c in row if isinstance(c.value, str) and c.value.startswith("=")]
    assert any("SUM" in f for f in formulas) and any("*C" in f for f in formulas)
    assert len(wb["Summary"]._charts) == 0  # charts live on the analysis sheets
    assert len(wb["Failures"]._charts) >= 2


def test_powerbi_export_ties_to_sql(pipeline, conn, tmp_path):
    export_powerbi.export(conn, tmp_path)
    k = db.run_query(conn, "kpi_headline").iloc[0]
    f = pd.read_csv(tmp_path / "fact_payments_daily.csv")
    assert f.attempts.sum() == k.attempts
    assert round(100 * f[f.status == "SUCCESS"].attempts.sum() / f.attempts.sum(), 2) == k.success_rate_pct
    r = pd.read_csv(tmp_path / "fact_reconciliation.csv")
    rh = db.run_query(conn, "recon_headline").iloc[0]
    assert round(100 * (1 - len(r) / k.attempts), 3) == rh.match_rate_pct          # the DAX Match Rate %
    unresolved = r[r.resolution_status.isin(["OPEN", "NO_REFUND_RAISED", "REFUND_IN_PROGRESS"])
                   & r.severity.isin(["HIGH", "MEDIUM"])].exposure_amount.sum()
    assert abs(unresolved - rh.unresolved_value_material) < 1
    ref = pd.read_csv(tmp_path / "fact_refunds.csv")
    assert ref.is_overdue_pending.sum() == db.run_query(conn, "refund_headline").iloc[0].pending_overdue
    o = pd.read_csv(tmp_path / "fact_orders_daily.csv")
    ci = db.run_query(conn, "cust_order_impact").iloc[0]
    assert o.orders.sum() == ci.orders and abs(o.lost_order_value.sum() - ci.lost_order_value) < 0.5
