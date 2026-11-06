"""ETL: raw CSV extract -> validated, standardised tables -> database.

Stages
  1. extract     read the raw CSV layer (everything as text so nothing is silently coerced)
  2. standardise trim/case/alias fixes, money and timestamp parsing, derived columns
  3. deduplicate exact duplicates dropped; same-key/different-content rows flagged
  4. validate    rule-based checks; every rejected row is kept with its rule code
  5. load        create schema, bulk insert in FK order, build indexes and views
  6. verify      row counts, amount totals and SQL integrity checks (sql/07_*.sql)

Outputs (under output/): etl_run_summary.json, data_quality_report.csv,
rejects/<table>_rejects.csv. Cleaned CSVs go to data/clean/.

Run:  python -m src.etl_pipeline --target sqlite
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import time
from pathlib import Path

import numpy as np
import pandas as pd

from . import config, db

log = logging.getLogger("etl")

# ---- allowed value domains (what a real ETL would hold in reference config)
CATEGORIES = ["Electronics", "Mobiles", "Fashion", "Home & Kitchen", "Grocery", "Beauty",
              "Books", "Toys & Games", "Sports", "Appliances"]
GATEWAYS = ["PayGate-A", "PayGate-B", "PayGate-C"]
DEVICES = ["Android", "iOS", "Web"]
PAY_STATUS = ["SUCCESS", "FAILED", "PENDING"]
ORDER_STATUS = ["DELIVERED", "IN_TRANSIT", "CANCELLED", "RETURNED", "PAYMENT_FAILED", "PAYMENT_PENDING"]
GW_STATUS = ["CAPTURED", "DECLINED", "TIMEOUT"]
REFUND_STATUS = ["PENDING", "PROCESSED", "FAILED"]
REFUND_REASONS = ["CUSTOMER_CANCELLATION", "RETURN", "DUPLICATE_CHARGE", "FAILED_PAYMENT_DEBIT"]
TIERS = ["Tier-1", "Tier-2", "Tier-3"]
METHOD_ALIASES = {"CC": "CREDIT_CARD", "DC": "DEBIT_CARD", "NETBANKING": "NET_BANKING"}
PAY_STATUS_ALIASES = {"SUCCESSFUL": "SUCCESS", "SUCCEEDED": "SUCCESS", "FAILURE": "FAILED",
                      "FAIL": "FAILED", "IN_PROCESS": "PENDING"}

ID_COLS = {
    "customers": ["customer_id"], "orders": ["order_id", "customer_id"],
    "payments": ["payment_id", "order_id", "customer_id"],
    "gateway_transactions": ["gateway_txn_id", "payment_id"],
    "refunds": ["refund_id", "payment_id", "order_id"],
    "payment_methods": ["method_id"],
}

TABLE_COLUMNS = {
    "payment_methods": ["method_id", "method_name", "method_type", "gateway_fee_pct",
                        "settlement_days", "refund_sla_days"],
    "customers": ["customer_id", "signup_date", "state", "city_tier", "is_prime"],
    "orders": ["order_id", "customer_id", "order_ts", "order_date", "order_amount", "currency",
               "category", "order_status"],
    "payments": ["payment_id", "order_id", "customer_id", "method_id", "attempt_no", "payment_ts",
                 "payment_date", "payment_hour", "amount", "status", "failure_code",
                 "failure_category", "device_type", "gateway_name"],
    "gateway_transactions": ["gateway_txn_id", "payment_id", "gateway_name", "gateway_ts",
                             "gateway_amount", "gateway_status", "response_code", "fee_amount",
                             "settlement_date"],
    "refunds": ["refund_id", "payment_id", "order_id", "refund_amount", "refund_reason",
                "refund_requested_ts", "refund_sla_due_ts", "refund_status", "refund_processed_ts"],
}


# --------------------------------------------------------------------------
# cleaning primitives (unit-tested)
# --------------------------------------------------------------------------
def _alnum(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def canon(series: pd.Series, allowed, aliases: dict | None = None) -> pd.Series:
    """Map messy text to the canonical allowed value ('credit card ' -> 'CREDIT_CARD').
    Unknown or blank values become NaN."""
    mapping = {_alnum(a): a for a in allowed}
    for k, v in (aliases or {}).items():
        mapping[_alnum(k)] = v
    key = series.astype("object").map(lambda x: _alnum(str(x)) if pd.notna(x) else "")
    return key.map(mapping)


def parse_amount(series: pd.Series) -> pd.Series:
    """'₹1,299.50' / 'Rs. 1299.5' / 'INR 1,299.50' / '1299.50' -> 1299.5"""
    txt = series.astype("object").map(
        lambda x: re.sub(r"(?i)(rs\.?|inr|₹|,|\s)", "", str(x)) if pd.notna(x) else None)
    return pd.to_numeric(txt, errors="coerce")


def parse_ts(series: pd.Series):
    """ISO timestamps, falling back to DD/MM/YYYY HH:MM:SS. Returns (parsed, n_alt_format)."""
    s = series.astype("object").map(lambda x: str(x).strip() if pd.notna(x) else None)
    first = pd.to_datetime(s, format="%Y-%m-%d %H:%M:%S", errors="coerce")
    miss = first.isna() & s.notna()
    n_alt = 0
    if miss.any():
        second = pd.to_datetime(s[miss], format="%d/%m/%Y %H:%M:%S", errors="coerce")
        first = first.copy()
        first[miss] = second
        n_alt = int(second.notna().sum())
    return first, n_alt


def parse_date(series: pd.Series) -> pd.Series:
    s = series.astype("object").map(lambda x: str(x).strip() if pd.notna(x) else None)
    return pd.to_datetime(s, format="%Y-%m-%d", errors="coerce")


def n_changed(raw: pd.Series, clean: pd.Series) -> int:
    r = raw.astype("object").map(lambda x: str(x) if pd.notna(x) else None)
    c = clean.astype("object").map(lambda x: str(x) if pd.notna(x) else None)
    return int(((r != c) & r.notna() & c.notna()).sum())


def fmt_ts(s: pd.Series) -> pd.Series:
    return s.dt.strftime("%Y-%m-%d %H:%M:%S").where(s.notna(), None)


def fmt_date(s: pd.Series) -> pd.Series:
    return s.dt.strftime("%Y-%m-%d").where(s.notna(), None)


# --------------------------------------------------------------------------
# reject bookkeeping
# --------------------------------------------------------------------------
class Quality:
    def __init__(self):
        self.reject_frames: dict[str, list[pd.DataFrame]] = {}
        self.rows: list[dict] = []          # data_quality_report rows
        self.tables: dict[str, dict] = {}

    def table(self, name, raw_rows):
        self.tables[name] = dict(raw_rows=int(raw_rows), clean_rows=0, rejected_rows=0,
                                 rejects_by_rule={}, standardised={}, warnings={})

    def reject(self, name, raw: pd.DataFrame, owner: pd.Series):
        bad = owner != ""
        if not bad.any():
            return bad
        part = raw[bad].copy()
        part.insert(0, "reject_rule", owner[bad].to_numpy())
        self.reject_frames.setdefault(name, []).append(part)
        for rule, n in owner[bad].value_counts().items():
            self.tables[name]["rejects_by_rule"][rule] = int(n)
            self.rows.append(dict(table=name, kind="rejected", rule=rule, rows=int(n)))
        self.tables[name]["rejected_rows"] += int(bad.sum())
        return bad

    def fix(self, name, what, n):
        self.tables[name]["standardised"][what] = int(n)
        if n:
            self.rows.append(dict(table=name, kind="standardised", rule=what, rows=int(n)))

    def warn(self, name, what, n):
        self.tables[name]["warnings"][what] = int(n)
        if n:
            self.rows.append(dict(table=name, kind="warning", rule=what, rows=int(n)))

    def finish(self, name, clean_rows):
        self.tables[name]["clean_rows"] = int(clean_rows)


def apply_rules(q: Quality, name: str, raw: pd.DataFrame, rules: list) -> pd.Series:
    """First matching rule owns a row. Returns boolean mask of rejected rows."""
    owner = pd.Series("", index=raw.index, dtype=object)
    for code, mask in rules:
        m = pd.Series(mask, index=raw.index).fillna(False).astype(bool) & (owner == "")
        owner[m] = code
    return q.reject(name, raw, owner)


def dup_masks(w: pd.DataFrame, key: str):
    exact = w.duplicated(keep="first")
    conflict = w.duplicated(subset=[key], keep="first") & ~exact
    return exact, conflict


# --------------------------------------------------------------------------
# extract
# --------------------------------------------------------------------------
def read_raw(path: Path, table: str) -> pd.DataFrame:
    ids = ID_COLS[table]
    base = dict(dtype=str, keep_default_na=False, na_values=[""])
    df = pd.read_csv(path, **base)
    for c in ids:                                   # non-numeric ids become NaN -> rejected later
        df[c + "__raw"] = df[c]
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("Int64")
    return df


def raw_view(df: pd.DataFrame) -> pd.DataFrame:
    """Original text values for the reject log (restores raw id strings)."""
    out = df.copy()
    for c in [c for c in out.columns if c.endswith("__raw")]:
        out[c[:-5]] = out[c]
        out = out.drop(columns=c)
    return out


# --------------------------------------------------------------------------
# per-table transforms
# --------------------------------------------------------------------------
def etl_payment_methods(raw, q):
    q.table("payment_methods", len(raw))
    w = raw[TABLE_COLUMNS["payment_methods"]].copy()
    w["method_id"] = w["method_id"].astype("Int64")
    for c in ("gateway_fee_pct", "settlement_days", "refund_sla_days"):
        w[c] = pd.to_numeric(w[c], errors="coerce")
    w["method_name"] = w["method_name"].str.strip().str.upper().str.replace(" ", "_")
    rules = [("NULL_MANDATORY", w.isna().any(axis=1)),
             ("DUPLICATE_KEY", w.duplicated(subset=["method_id"], keep="first"))]
    bad = apply_rules(q, "payment_methods", raw_view(raw), rules)
    w = w[~bad]
    w["method_id"] = w["method_id"].astype("int64")
    for c in ("settlement_days", "refund_sla_days"):
        w[c] = w[c].astype("int64")
    q.finish("payment_methods", len(w))
    return w.reset_index(drop=True)


def etl_customers(raw, q):
    q.table("customers", len(raw))
    w = pd.DataFrame(index=raw.index)
    w["customer_id"] = raw["customer_id"]
    w["signup_date"] = parse_date(raw["signup_date"])
    state = raw["state"].astype("object").map(
        lambda x: re.sub(r"\s+", " ", str(x).strip()).title() if pd.notna(x) else None)
    q.fix("customers", "state_standardised", n_changed(raw["state"], state))
    w["state"] = state.fillna("Unknown")
    tier = canon(raw["city_tier"], TIERS)
    q.fix("customers", "city_tier_missing_filled_unknown", int(tier.isna().sum()))
    w["city_tier"] = tier.fillna("Unknown")
    prime = canon(raw["is_prime"], ["0", "1"], {"Y": "1", "YES": "1", "TRUE": "1", "N": "0", "NO": "0", "FALSE": "0"})
    w["is_prime"] = pd.to_numeric(prime, errors="coerce")

    exact, conflict = dup_masks(w, "customer_id")
    rules = [("NULL_MANDATORY", w[["customer_id", "signup_date", "is_prime"]].isna().any(axis=1)),
             ("DUPLICATE_EXACT", exact), ("DUPLICATE_KEY_CONFLICT", conflict)]
    bad = apply_rules(q, "customers", raw_view(raw), rules)
    w = w[~bad].copy()
    w["customer_id"] = w["customer_id"].astype("int64")
    w["is_prime"] = w["is_prime"].astype("int64")
    w["signup_date"] = fmt_date(w["signup_date"])
    q.finish("customers", len(w))
    return w[TABLE_COLUMNS["customers"]].reset_index(drop=True)


def etl_orders(raw, q, customers, limit_ts):
    q.table("orders", len(raw))
    w = pd.DataFrame(index=raw.index)
    w["order_id"], w["customer_id"] = raw["order_id"], raw["customer_id"]
    w["order_ts"], _ = parse_ts(raw["order_ts"])
    w["order_amount"] = parse_amount(raw["order_amount"])
    q.fix("orders", "amount_reformatted", raw["order_amount"].astype("object").map(
        lambda x: bool(re.search(r"[^0-9.\-]", str(x))) if pd.notna(x) else False).sum())
    w["currency"] = raw["currency"].astype("object").map(lambda x: str(x).strip().upper() if pd.notna(x) else None)
    w["category"] = canon(raw["category"], CATEGORIES)
    q.fix("orders", "category_standardised", n_changed(raw["category"], w["category"]))
    w["order_status"] = canon(raw["order_status"], ORDER_STATUS)

    exact, conflict = dup_masks(w, "order_id")
    must = ["order_id", "customer_id", "order_ts", "order_amount", "currency", "category", "order_status"]
    rules = [("NULL_MANDATORY", w[must].isna().any(axis=1)),
             ("DUPLICATE_EXACT", exact), ("DUPLICATE_KEY_CONFLICT", conflict),
             ("INVALID_AMOUNT", w["order_amount"] <= 0),
             ("FUTURE_TIMESTAMP", w["order_ts"] > limit_ts),
             ("ORPHAN_CUSTOMER", ~w["customer_id"].isin(customers["customer_id"]))]
    bad = apply_rules(q, "orders", raw_view(raw), rules)
    w = w[~bad].copy()
    w["order_date"] = fmt_date(w["order_ts"].dt.normalize())
    w["order_ts"] = fmt_ts(w["order_ts"])
    w["order_amount"] = w["order_amount"].round(2)
    for c in ("order_id", "customer_id"):
        w[c] = w[c].astype("int64")
    q.finish("orders", len(w))
    return w[TABLE_COLUMNS["orders"]].reset_index(drop=True)


def etl_payments(raw, q, orders, methods, limit_ts):
    q.table("payments", len(raw))
    w = pd.DataFrame(index=raw.index)
    for c in ("payment_id", "order_id", "customer_id"):
        w[c] = raw[c]
    mname = canon(raw["payment_method"], methods["method_name"].tolist(), METHOD_ALIASES)
    q.fix("payments", "method_standardised", n_changed(raw["payment_method"], mname))
    w["method_id"] = mname.map(dict(zip(methods["method_name"], methods["method_id"])))
    w["attempt_no"] = pd.to_numeric(raw["attempt_no"], errors="coerce")
    ts, n_alt = parse_ts(raw["payment_ts"])
    q.fix("payments", "timestamp_alt_format_parsed", n_alt)
    w["payment_ts"] = ts
    w["amount"] = parse_amount(raw["amount"])
    q.fix("payments", "amount_reformatted", raw["amount"].astype("object").map(
        lambda x: bool(re.search(r"[^0-9.\-]", str(x))) if pd.notna(x) else False).sum())
    w["status"] = canon(raw["status"], PAY_STATUS, PAY_STATUS_ALIASES)
    q.fix("payments", "status_standardised", n_changed(raw["status"], w["status"]))
    codes = list(config.FAILURE_CATEGORY)
    w["failure_code"] = canon(raw["failure_code"], codes)
    w["device_type"] = canon(raw["device_type"], DEVICES)
    w["gateway_name"] = canon(raw["gateway_name"], GATEWAYS)

    exact, conflict = dup_masks(w, "payment_id")
    lk = w[["order_id"]].merge(orders[["order_id", "order_ts", "customer_id"]].assign(
        order_ts=lambda d: pd.to_datetime(d["order_ts"])).rename(
        columns={"order_ts": "o_ts", "customer_id": "o_cust"}), on="order_id", how="left")
    o_ts = pd.Series(lk["o_ts"].to_numpy(), index=w.index)
    o_cust = pd.Series(lk["o_cust"].to_numpy(), index=w.index)
    must = ["payment_id", "order_id", "customer_id", "method_id", "attempt_no", "payment_ts",
            "amount", "status", "device_type", "gateway_name"]
    bad_code = w["failure_code"].isna() & raw["failure_code"].notna()
    rules = [("NULL_MANDATORY", w[must].isna().any(axis=1)),
             ("DUPLICATE_EXACT", exact), ("DUPLICATE_KEY_CONFLICT", conflict),
             ("INVALID_AMOUNT", w["amount"] <= 0),
             ("FUTURE_TIMESTAMP", w["payment_ts"] > limit_ts),
             ("ORPHAN_ORDER", o_ts.isna()),
             ("CUSTOMER_ORDER_MISMATCH", o_cust.notna() & (o_cust.astype("float64") != w["customer_id"].astype("float64"))),
             ("PAYMENT_BEFORE_ORDER", w["payment_ts"] < o_ts),
             ("INVALID_FAILURE_CODE", bad_code),
             ("FAILURE_CODE_INCONSISTENT", ((w["status"] == "FAILED") & w["failure_code"].isna()) |
              ((w["status"] != "FAILED") & w["failure_code"].notna()))]
    bad = apply_rules(q, "payments", raw_view(raw), rules)
    w = w[~bad].copy()
    w["failure_category"] = w["failure_code"].map(config.FAILURE_CATEGORY)
    w["payment_date"] = fmt_date(w["payment_ts"].dt.normalize())
    w["payment_hour"] = w["payment_ts"].dt.hour.astype("int64")
    w["payment_ts"] = fmt_ts(w["payment_ts"])
    w["amount"] = w["amount"].round(2)
    for c in ("payment_id", "order_id", "customer_id", "method_id", "attempt_no"):
        w[c] = w[c].astype("int64")
    q.finish("payments", len(w))
    return w[TABLE_COLUMNS["payments"]].reset_index(drop=True)


def etl_gateway(raw, q, payments, limit_ts):
    q.table("gateway_transactions", len(raw))
    w = pd.DataFrame(index=raw.index)
    w["gateway_txn_id"], w["payment_id"] = raw["gateway_txn_id"], raw["payment_id"]
    w["gateway_name"] = canon(raw["gateway_name"], GATEWAYS)
    w["gateway_ts"], _ = parse_ts(raw["gateway_ts"])
    w["gateway_amount"] = parse_amount(raw["gateway_amount"])
    q.fix("gateway_transactions", "amount_reformatted", raw["gateway_amount"].astype("object").map(
        lambda x: bool(re.search(r"[^0-9.\-]", str(x))) if pd.notna(x) else False).sum())
    w["gateway_status"] = canon(raw["gateway_status"], GW_STATUS, {"CAPTURE": "CAPTURED", "DECLINE": "DECLINED"})
    q.fix("gateway_transactions", "status_standardised", n_changed(raw["gateway_status"], w["gateway_status"]))
    w["response_code"] = raw["response_code"].astype("object").map(lambda x: str(x).strip() if pd.notna(x) else None)
    w["fee_amount"] = parse_amount(raw["fee_amount"])
    w["settlement_date"] = parse_date(raw["settlement_date"])

    exact, conflict = dup_masks(w, "gateway_txn_id")
    must = ["gateway_txn_id", "payment_id", "gateway_name", "gateway_ts", "gateway_amount",
            "gateway_status", "response_code", "fee_amount"]
    rules = [("NULL_MANDATORY", w[must].isna().any(axis=1)),
             ("DUPLICATE_EXACT", exact), ("DUPLICATE_KEY_CONFLICT", conflict),
             ("INVALID_AMOUNT", (w["gateway_amount"] < 0) | (w["fee_amount"] < 0)),
             ("FUTURE_TIMESTAMP", w["gateway_ts"] > limit_ts),
             ("ORPHAN_PAYMENT", ~w["payment_id"].isin(payments["payment_id"])),
             ("CAPTURED_WITHOUT_SETTLEMENT_DATE", (w["gateway_status"] == "CAPTURED") & w["settlement_date"].isna())]
    bad = apply_rules(q, "gateway_transactions", raw_view(raw), rules)
    w = w[~bad].copy()
    # warning only: routing gateway on the payment vs gateway on the transaction
    pg = w[["payment_id"]].merge(payments[["payment_id", "gateway_name"]], on="payment_id", how="left")
    q.warn("gateway_transactions", "gateway_name_differs_from_payment",
           int((pg["gateway_name"].to_numpy() != w["gateway_name"].to_numpy()).sum()))
    w["gateway_ts"] = fmt_ts(w["gateway_ts"])
    w["settlement_date"] = fmt_date(w["settlement_date"])
    w["gateway_amount"] = w["gateway_amount"].round(2)
    w["fee_amount"] = w["fee_amount"].round(2)
    for c in ("gateway_txn_id", "payment_id"):
        w[c] = w[c].astype("int64")
    q.finish("gateway_transactions", len(w))
    return w[TABLE_COLUMNS["gateway_transactions"]].reset_index(drop=True)


def etl_refunds(raw, q, payments, limit_ts):
    q.table("refunds", len(raw))
    w = pd.DataFrame(index=raw.index)
    for c in ("refund_id", "payment_id", "order_id"):
        w[c] = raw[c]
    w["refund_amount"] = parse_amount(raw["refund_amount"])
    w["refund_reason"] = canon(raw["refund_reason"], REFUND_REASONS)
    w["refund_requested_ts"], _ = parse_ts(raw["refund_requested_ts"])
    w["refund_sla_due_ts"], _ = parse_ts(raw["refund_sla_due_ts"])
    w["refund_processed_ts"], _ = parse_ts(raw["refund_processed_ts"])
    w["refund_status"] = canon(raw["refund_status"], REFUND_STATUS)
    q.fix("refunds", "status_standardised", n_changed(raw["refund_status"], w["refund_status"]))

    exact, conflict = dup_masks(w, "refund_id")
    lk = w[["payment_id"]].merge(payments[["payment_id", "order_id", "amount"]].rename(
        columns={"order_id": "p_order", "amount": "p_amount"}), on="payment_id", how="left")
    p_amt = pd.Series(lk["p_amount"].to_numpy(), index=w.index)
    p_ord = pd.Series(lk["p_order"].to_numpy(), index=w.index)
    must = ["refund_id", "payment_id", "order_id", "refund_amount", "refund_reason",
            "refund_requested_ts", "refund_sla_due_ts", "refund_status"]
    rules = [("NULL_MANDATORY", w[must].isna().any(axis=1)),
             ("DUPLICATE_EXACT", exact), ("DUPLICATE_KEY_CONFLICT", conflict),
             ("INVALID_AMOUNT", w["refund_amount"] <= 0),
             ("FUTURE_TIMESTAMP", w["refund_requested_ts"] > limit_ts),
             ("ORPHAN_PAYMENT", p_amt.isna()),
             ("ORDER_PAYMENT_MISMATCH", p_ord.notna() & (p_ord.astype("float64") != w["order_id"].astype("float64"))),
             ("REFUND_EXCEEDS_PAYMENT", w["refund_amount"] > p_amt.astype("float64") + 0.005),
             ("PROCESSED_BEFORE_REQUESTED", w["refund_processed_ts"] < w["refund_requested_ts"]),
             ("SLA_BEFORE_REQUEST", w["refund_sla_due_ts"] < w["refund_requested_ts"]),
             ("STATUS_TIMESTAMP_INCONSISTENT",
              ((w["refund_status"] == "PENDING") & w["refund_processed_ts"].notna()) |
              ((w["refund_status"] != "PENDING") & w["refund_processed_ts"].isna()))]
    bad = apply_rules(q, "refunds", raw_view(raw), rules)
    w = w[~bad].copy()
    for c in ("refund_requested_ts", "refund_sla_due_ts", "refund_processed_ts"):
        w[c] = fmt_ts(w[c])
    w["refund_amount"] = w["refund_amount"].round(2)
    for c in ("refund_id", "payment_id", "order_id"):
        w[c] = w[c].astype("int64")
    q.finish("refunds", len(w))
    return w[TABLE_COLUMNS["refunds"]].reset_index(drop=True)


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------
def transform(raw_dir: Path, as_of: str | None = None):
    """Extract + standardise + validate. Returns (clean tables, Quality)."""
    raw_dir = Path(raw_dir)
    manifest_path = raw_dir / "_manifest.json"
    if as_of is None:
        as_of = (json.loads(manifest_path.read_text())["as_of"] if manifest_path.exists()
                 else f"{config.END_DATE} 23:59:59")
    limit_ts = pd.Timestamp(as_of) + pd.Timedelta(days=1)       # tolerate clock skew, reject the future
    q = Quality()
    t0 = time.time()

    def step(name):
        log.info("%-24s done (%.1fs)", name, time.time() - t0)

    methods = etl_payment_methods(pd.read_csv(raw_dir / "payment_methods.csv", dtype=str, keep_default_na=False,
                                              na_values=[""]), q); step("payment_methods")
    customers = etl_customers(read_raw(raw_dir / "customers.csv", "customers"), q); step("customers")
    orders = etl_orders(read_raw(raw_dir / "orders.csv", "orders"), q, customers, limit_ts); step("orders")
    payments = etl_payments(read_raw(raw_dir / "payments.csv", "payments"), q, orders, methods, limit_ts); step("payments")
    gateway = etl_gateway(read_raw(raw_dir / "gateway_transactions.csv", "gateway_transactions"), q, payments, limit_ts); step("gateway")
    refunds = etl_refunds(read_raw(raw_dir / "refunds.csv", "refunds"), q, payments, limit_ts); step("refunds")
    tables = dict(payment_methods=methods, customers=customers, orders=orders, payments=payments,
                  gateway_transactions=gateway, refunds=refunds)
    return tables, q


def write_quality_outputs(q: Quality, out_dir: Path):
    out_dir = Path(out_dir)
    (out_dir / "rejects").mkdir(parents=True, exist_ok=True)
    pd.DataFrame(q.rows, columns=["table", "kind", "rule", "rows"]).to_csv(
        out_dir / "data_quality_report.csv", index=False)
    for name, frames in q.reject_frames.items():
        pd.concat(frames).to_csv(out_dir / "rejects" / f"{name}_rejects.csv", index=False)


def verify_load(conn, tables: dict, strict: bool = True) -> dict:
    """Row counts, amount control totals and SQL integrity checks."""
    result = dict(row_counts={}, control_totals={}, integrity={})
    problems = []
    for t in db.TABLE_ORDER:
        n_db, n_df = db.table_count(conn, t), len(tables[t])
        result["row_counts"][t] = dict(dataframe=n_df, database=n_db)
        if n_db != n_df:
            problems.append(f"row count mismatch in {t}: df={n_df} db={n_db}")
    for t, col in (("payments", "amount"), ("orders", "order_amount"),
                   ("gateway_transactions", "gateway_amount"), ("refunds", "refund_amount")):
        cur = conn.cursor()
        cur.execute(f"SELECT SUM({col}) FROM {t}")
        s_db = float(cur.fetchone()[0] or 0)
        s_df = float(tables[t][col].sum())
        result["control_totals"][t] = dict(dataframe=round(s_df, 2), database=round(s_db, 2))
        if abs(s_db - s_df) > 0.5:
            problems.append(f"control total mismatch in {t}.{col}: df={s_df:.2f} db={s_db:.2f}")
    checks = db.load_named_queries(config.SQL_DIR / "07_data_quality_checks.sql")
    for name, spec in checks.items():
        n = int(db.fetch_df(conn, spec["sql"]).iloc[0, 0])
        result["integrity"][name] = n
        if n:
            problems.append(f"integrity check {name}: {n} violating rows")
    result["problems"] = problems
    if problems and strict:
        raise RuntimeError("post-load verification failed:\n  " + "\n  ".join(problems))
    return result


def run_etl(raw_dir=config.DATA_RAW, clean_dir=config.DATA_CLEAN, out_dir=config.OUTPUT,
            target="sqlite", write_clean=True, strict=True, fresh=True) -> dict:
    t0 = time.time()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tables, q = transform(Path(raw_dir))
    write_quality_outputs(q, out_dir)
    if write_clean:
        Path(clean_dir).mkdir(parents=True, exist_ok=True)
        for name, df in tables.items():
            df.to_csv(Path(clean_dir) / f"{name}.csv", index=False)
        log.info("clean CSVs written to %s", clean_dir)

    conn = db.connect(target, fresh=fresh, path=out_dir / "paymentops.db")
    try:
        log.info("creating schema on %s", target)
        db.create_schema(conn)
        for name in db.TABLE_ORDER:
            n = db.bulk_insert(conn, name, tables[name][TABLE_COLUMNS[name]])
            log.info("loaded %-22s %10s rows (%.0fs elapsed)", name, f"{n:,}", time.time() - t0)
        db.create_indexes(conn)
        db.create_views(conn)
        verification = verify_load(conn, tables, strict=strict)
    finally:
        conn.close()

    summary = dict(target=target, seconds=round(time.time() - t0, 1), tables=q.tables,
                   verification=verification)
    (out_dir / "etl_run_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    for name, info in q.tables.items():
        log.info("%-22s raw=%9s clean=%9s rejected=%6s %s", name, f"{info['raw_rows']:,}",
                 f"{info['clean_rows']:,}", f"{info['rejected_rows']:,}", info["rejects_by_rule"])
    log.info("ETL finished in %.1fs", time.time() - t0)
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--raw", default=str(config.DATA_RAW))
    ap.add_argument("--clean", default=str(config.DATA_CLEAN))
    ap.add_argument("--out", default=str(config.OUTPUT))
    ap.add_argument("--target", choices=["sqlite", "mysql"], default="sqlite")
    ap.add_argument("--no-clean-csv", action="store_true", help="skip writing data/clean/*.csv")
    ap.add_argument("--no-strict", action="store_true", help="report verification problems instead of failing")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_etl(a.raw, a.clean, a.out, a.target, not a.no_clean_csv, not a.no_strict)


if __name__ == "__main__":
    main()
