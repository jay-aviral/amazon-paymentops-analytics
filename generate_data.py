"""Synthetic payment-operations data generator.

Produces the raw CSV layer for a fictional Amazon-style marketplace:

    customers.csv · orders.csv · payments.csv · gateway_transactions.csv ·
    refunds.csv · payment_methods.csv

`payments` has exactly --transactions rows (payment *attempts*); orders may have
several attempts because customers retry after a failure.

The data is built so that an analyst can actually *find* things in it:

* failure rates differ by payment method, gateway, device, ticket size
* two sale events lift volume and failure rates
* two seeded incidents: a PayGate-B outage and a net-banking issuer problem
* a small, realistic share of reconciliation breaks (missing gateway record,
  amount mismatch, duplicate capture, captured-but-not-confirmed, stuck pending)
* refunds with SLA breaches, concentrated in the manual-handling reasons

On top of the clean "truth" tables, a layer of realistic extract noise is added
(exact duplicates, key conflicts, messy formats, orphan and invalid rows) so
the ETL has real work to do. With --truth-dir the pre-noise tables are saved so
the ETL can be tested against the ground truth.

Run:  python -m src.generate_data --transactions 1000000
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

import numpy as np
import pandas as pd

from . import config

log = logging.getLogger("generate")

# --------------------------------------------------------------------------
# Reference data
# --------------------------------------------------------------------------
# id, name, type, gateway fee %, settlement business days, refund SLA days,
# share of attempts, base failure probability
METHODS = [
    (1, "UPI", "Instant Bank Transfer", 0.00, 1, 3, 0.46, 0.040),
    (2, "CREDIT_CARD", "Card", 1.80, 2, 7, 0.18, 0.050),
    (3, "DEBIT_CARD", "Card", 0.90, 2, 7, 0.14, 0.068),
    (4, "NET_BANKING", "Bank Redirect", 1.20, 2, 5, 0.08, 0.088),
    (5, "WALLET", "Stored Value", 0.50, 1, 2, 0.08, 0.025),
    (6, "EMI", "Instalment Card", 2.20, 3, 7, 0.06, 0.075),
]
M_NAMES = np.array([m[1] for m in METHODS])
M_FEE = np.array([m[3] for m in METHODS])
M_SETTLE = np.array([m[4] for m in METHODS])
M_SLA = np.array([m[5] for m in METHODS])
M_MIX = np.array([m[6] for m in METHODS])
M_BASE_FAIL = np.array([m[7] for m in METHODS])
EMI_IDX, EMI_MIN_AMOUNT = 5, 3000

GATEWAYS = np.array(["PayGate-A", "PayGate-B", "PayGate-C"])
GW_FAIL_FACTOR = np.array([1.00, 1.15, 0.90])
GW_WEIGHTS = np.array([  # rows = methods, cols = gateways
    [0.50, 0.30, 0.20],
    [0.40, 0.40, 0.20],
    [0.40, 0.40, 0.20],
    [0.30, 0.30, 0.40],
    [0.60, 0.20, 0.20],
    [0.35, 0.45, 0.20],
])
DEVICES = np.array(["Android", "iOS", "Web"])
DEVICE_MIX = np.array([0.55, 0.15, 0.30])
DEVICE_FAIL_FACTOR = np.array([1.00, 0.90, 1.15])

FAIL_CODES = ["INSUFFICIENT_FUNDS", "UPI_PIN_INCORRECT", "INVALID_OTP", "CARD_EXPIRED",
              "BANK_DECLINED", "ISSUER_TIMEOUT", "3DS_AUTH_FAILED",
              "GATEWAY_TIMEOUT", "NETWORK_ERROR", "RISK_REJECTED"]
C = {c: i for i, c in enumerate(FAIL_CODES)}
_cat_order = ["CUSTOMER_ACTION", "ISSUER_BANK", "TECHNICAL", "RISK"]
CODE_CAT_IDX = np.array([_cat_order.index(config.FAILURE_CATEGORY[c]) for c in FAIL_CODES])

# P(failure code | method failed) -- rows are methods
_CODE_DIST = {
    "UPI": {"UPI_PIN_INCORRECT": .22, "INSUFFICIENT_FUNDS": .24, "BANK_DECLINED": .15,
            "ISSUER_TIMEOUT": .17, "NETWORK_ERROR": .12, "GATEWAY_TIMEOUT": .06, "RISK_REJECTED": .04},
    "CREDIT_CARD": {"3DS_AUTH_FAILED": .22, "INSUFFICIENT_FUNDS": .12, "BANK_DECLINED": .20,
                    "ISSUER_TIMEOUT": .12, "CARD_EXPIRED": .06, "INVALID_OTP": .14,
                    "GATEWAY_TIMEOUT": .06, "RISK_REJECTED": .08},
    "DEBIT_CARD": {"INSUFFICIENT_FUNDS": .26, "INVALID_OTP": .20, "BANK_DECLINED": .18,
                   "3DS_AUTH_FAILED": .10, "ISSUER_TIMEOUT": .10, "CARD_EXPIRED": .04,
                   "GATEWAY_TIMEOUT": .06, "RISK_REJECTED": .06},
    "NET_BANKING": {"ISSUER_TIMEOUT": .30, "INVALID_OTP": .16, "INSUFFICIENT_FUNDS": .14,
                    "BANK_DECLINED": .12, "NETWORK_ERROR": .14, "GATEWAY_TIMEOUT": .10,
                    "RISK_REJECTED": .04},
    "WALLET": {"INSUFFICIENT_FUNDS": .55, "NETWORK_ERROR": .20, "GATEWAY_TIMEOUT": .10,
               "RISK_REJECTED": .15},
    "EMI": {"BANK_DECLINED": .30, "INSUFFICIENT_FUNDS": .22, "3DS_AUTH_FAILED": .14,
            "ISSUER_TIMEOUT": .12, "INVALID_OTP": .10, "GATEWAY_TIMEOUT": .06,
            "RISK_REJECTED": .06},
}
CODE_PROB = np.zeros((len(METHODS), len(FAIL_CODES)))
for _mi, _m in enumerate(METHODS):
    for _code, _p in _CODE_DIST[_m[1]].items():
        CODE_PROB[_mi, C[_code]] = _p
assert np.allclose(CODE_PROB.sum(axis=1), 1.0)

# gateway response codes (fictional)
CODE_RESPONSE = {"INSUFFICIENT_FUNDS": "51", "UPI_PIN_INCORRECT": "U3", "INVALID_OTP": "55",
                 "CARD_EXPIRED": "54", "BANK_DECLINED": "05", "ISSUER_TIMEOUT": "91",
                 "3DS_AUTH_FAILED": "63", "GATEWAY_TIMEOUT": "68", "NETWORK_ERROR": "96",
                 "RISK_REJECTED": "59"}
RESP_BY_CODE = np.array([CODE_RESPONSE[c] for c in FAIL_CODES])

# P(customer retries | failure category), P(switches method | category)
RETRY_P = np.array([0.62, 0.58, 0.72, 0.15])
SWITCH_P = np.array([0.20, 0.35, 0.35, 0.50])
P_PENDING = 0.0035

# gateway-captured despite internal FAILED ("captured, not confirmed")
CAPTURED_DESPITE_FAIL = {C["GATEWAY_TIMEOUT"]: 0.035, C["ISSUER_TIMEOUT"]: 0.015,
                         C["NETWORK_ERROR"]: 0.010}

# order categories: name, share, median ticket, sigma
CATEGORIES = [
    ("Electronics", .14, 4500, .9), ("Mobiles", .08, 14000, .6), ("Fashion", .20, 1100, .6),
    ("Home & Kitchen", .14, 1200, .7), ("Grocery", .14, 650, .5), ("Beauty", .08, 600, .5),
    ("Books", .06, 400, .5), ("Toys & Games", .04, 700, .6), ("Sports", .05, 1500, .7),
    ("Appliances", .07, 9000, .7),
]
STATES = [("Maharashtra", .14), ("Karnataka", .11), ("Tamil Nadu", .10), ("Delhi", .09),
          ("Uttar Pradesh", .12), ("West Bengal", .08), ("Telangana", .07), ("Gujarat", .08),
          ("Rajasthan", .05), ("Kerala", .05), ("Haryana", .04), ("Bihar", .04), ("Odisha", .03)]
HOUR_W = np.array([0.8, 0.5, 0.35, 0.3, 0.3, 0.4, 0.7, 1.1, 1.5, 1.8, 2.0, 2.1, 2.2, 2.1,
                   2.0, 2.0, 2.1, 2.3, 2.6, 3.0, 3.4, 3.5, 3.0, 1.8])
HOUR_W = HOUR_W / HOUR_W.sum()

# scenario events (dates must fall inside the window to have any effect)
SALE_MID = ("2026-07-14", "2026-07-15", 2.2, 1.25)      # start, end, volume x, failure x
SALE_FESTIVE = ("2026-10-01", "2026-10-10", 2.0, 1.35)
SALE_FESTIVE_PEAK = ("2026-10-02", "2026-10-04", 2.6, 1.45)
INCIDENT_GATEWAY = dict(gateway="PayGate-B", start="2026-07-15T14:00:00",
                        end="2026-07-15T17:59:59", fail_p=0.45)
INCIDENT_NETBANK = dict(start="2026-09-08T10:00:00", end="2026-09-08T13:59:59", fail_p=0.35)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _fmt_ts(base: np.datetime64, seconds: np.ndarray) -> np.ndarray:
    ts = base + seconds.astype("timedelta64[s]")
    return np.char.replace(ts.astype("datetime64[s]").astype("U19"), "T", " ")


def _fmt_date(base: np.datetime64, seconds: np.ndarray) -> np.ndarray:
    ts = (base + seconds.astype("timedelta64[s]")).astype("datetime64[D]")
    return ts.astype("U10")


def _rel_seconds(base: np.datetime64, iso: str) -> int:
    return int((np.datetime64(iso) - base) / np.timedelta64(1, "s"))


def draw_methods(amount: np.ndarray, exclude: np.ndarray, rng) -> np.ndarray:
    """Draw payment method per row; EMI only for tickets >= 3000; optionally
    exclude the method currently in use (customer switching method)."""
    out = np.empty(len(amount), dtype=np.int64)
    for ex in range(-1, len(METHODS)):
        for emi_ok in (True, False):
            mask = (exclude == ex) & ((amount >= EMI_MIN_AMOUNT) == emi_ok)
            if not mask.any():
                continue
            w = M_MIX.copy()
            if ex >= 0:
                w[ex] = 0
            if not emi_ok:
                w[EMI_IDX] = 0
            out[mask] = rng.choice(len(METHODS), size=int(mask.sum()), p=w / w.sum())
    return out


def draw_gateways(method_idx: np.ndarray, rng) -> np.ndarray:
    out = np.empty(len(method_idx), dtype=np.int64)
    for m in range(len(METHODS)):
        mask = method_idx == m
        if mask.any():
            out[mask] = rng.choice(len(GATEWAYS), size=int(mask.sum()), p=GW_WEIGHTS[m])
    return out


def draw_codes(method_idx: np.ndarray, rng) -> np.ndarray:
    out = np.empty(len(method_idx), dtype=np.int64)
    for m in range(len(METHODS)):
        mask = method_idx == m
        if mask.any():
            out[mask] = rng.choice(len(FAIL_CODES), size=int(mask.sum()), p=CODE_PROB[m])
    return out


# --------------------------------------------------------------------------
# simulation
# --------------------------------------------------------------------------
class Calendar:
    """Day-level volume and failure multipliers for the generation window."""

    def __init__(self, start: str, end: str):
        self.base = np.datetime64(start + "T00:00:00")
        end_excl = np.datetime64(end) + np.timedelta64(1, "D")
        self.n_days = int((end_excl - np.datetime64(start)) / np.timedelta64(1, "D"))
        days = np.datetime64(start) + np.arange(self.n_days)
        dow = (days.astype("datetime64[D]").astype(np.int64) + 3) % 7   # Monday = 0
        trend = (1 + 0.001 * np.arange(self.n_days)) * np.where(dow >= 5, 1.15, 1.0)
        vol = trend.copy()
        fail = np.ones(self.n_days)
        for s, e, v, f in (SALE_MID, SALE_FESTIVE, SALE_FESTIVE_PEAK):   # peak overrides festive
            mask = (days >= np.datetime64(s)) & (days <= np.datetime64(e))
            vol = np.where(mask, trend * v, vol)
            fail = np.where(mask, f, fail)
        self.vol_w = vol / vol.sum()
        self.fail_mult = fail
        self.max_ts = self.n_days * 86400
        self.inc_gw = (_rel_seconds(self.base, INCIDENT_GATEWAY["start"]),
                       _rel_seconds(self.base, INCIDENT_GATEWAY["end"]))
        self.inc_nb = (_rel_seconds(self.base, INCIDENT_NETBANK["start"]),
                       _rel_seconds(self.base, INCIDENT_NETBANK["end"]))


def draw_outcomes(method, gw, amount, ts, device, attempt, cal: Calendar, rng):
    """Return outcome (0 success, 1 failed, 2 pending) and failure code index."""
    n = len(method)
    p = M_BASE_FAIL[method] * GW_FAIL_FACTOR[gw] * DEVICE_FAIL_FACTOR[device]
    p = p * np.where(amount > 20000, 1.35, np.where(amount > 5000, 1.15, 1.0))
    if attempt > 1:
        p = p * 0.8
    p = p * cal.fail_mult[np.minimum(ts // 86400, cal.n_days - 1)]
    gw_b = int(np.where(GATEWAYS == INCIDENT_GATEWAY["gateway"])[0][0])
    inc_gw = (gw == gw_b) & (ts >= cal.inc_gw[0]) & (ts <= cal.inc_gw[1])
    inc_nb = (method == 3) & (ts >= cal.inc_nb[0]) & (ts <= cal.inc_nb[1])
    p = np.where(inc_gw, np.maximum(p, INCIDENT_GATEWAY["fail_p"]), p)
    p = np.where(inc_nb, np.maximum(p, INCIDENT_NETBANK["fail_p"]), p)
    p = np.clip(p, 0, 0.9)

    failed = rng.random(n) < p
    pending = (~failed) & (rng.random(n) < P_PENDING)
    outcome = np.where(failed, 1, np.where(pending, 2, 0))

    code = draw_codes(method, rng)
    if inc_gw.any():
        k = int(inc_gw.sum())
        code[inc_gw] = np.where(rng.random(k) < 0.8, C["GATEWAY_TIMEOUT"], C["NETWORK_ERROR"])
    nb_only = inc_nb & ~inc_gw
    if nb_only.any():
        k = int(nb_only.sum())
        code[nb_only] = np.where(rng.random(k) < 0.85, C["ISSUER_TIMEOUT"], C["NETWORK_ERROR"])
    code = np.where(outcome == 1, code, -1)
    return outcome, code


def simulate(n_target: int, n_customers: int, start: str, end: str, rng):
    cal = Calendar(start, end)
    n = n_target                     # each order has >= 1 attempt, so n orders is enough

    day = rng.choice(cal.n_days, size=n, p=cal.vol_w)
    hour = rng.choice(24, size=n, p=HOUR_W)
    ts = day * 86400 + hour * 3600 + rng.integers(0, 3600, size=n)
    ts = np.minimum(ts, cal.max_ts - 3601)                 # leave room for retries

    cat_p = np.array([c[1] for c in CATEGORIES])
    cat = rng.choice(len(CATEGORIES), size=n, p=cat_p / cat_p.sum())
    med = np.array([c[2] for c in CATEGORIES])[cat]
    sig = np.array([c[3] for c in CATEGORIES])[cat]
    amt = np.clip(rng.lognormal(np.log(med), sig), 49, 150000)
    amt = np.where(rng.random(n) < 0.8, np.round(amt), np.round(amt, 2))

    cust_w = rng.lognormal(0, 0.8, size=n_customers)
    cust = rng.choice(n_customers, size=n, p=cust_w / cust_w.sum()) + 1
    device = rng.choice(len(DEVICES), size=n, p=DEVICE_MIX)

    # ---- attempt rounds ----------------------------------------------------
    idx = np.arange(n)
    cur_m = draw_methods(amt, np.full(n, -1), rng)
    cur_g = draw_gateways(cur_m, rng)
    cur_t = ts.copy()
    rounds = []
    for attempt in (1, 2, 3):
        outcome, code = draw_outcomes(cur_m, cur_g, amt[idx], cur_t, device[idx], attempt, cal, rng)
        rounds.append(dict(order_idx=idx, attempt_no=np.full(len(idx), attempt), method=cur_m,
                           gw=cur_g, ts=cur_t, outcome=outcome, code=code, device=device[idx]))
        if attempt == 3:
            break
        failed = outcome == 1
        cat_f = CODE_CAT_IDX[np.where(failed, code, 0)]
        p_retry = RETRY_P[cat_f] * (0.8 if attempt == 2 else 1.0)
        retry = failed & (rng.random(len(idx)) < p_retry)
        if not retry.any():
            break
        switch = retry & (rng.random(len(idx)) < SWITCH_P[cat_f])
        new_m = cur_m.copy()
        if switch.any():
            new_m[switch] = draw_methods(amt[idx][switch], cur_m[switch], rng)
        new_g = cur_g.copy()
        changed = switch | (retry & (rng.random(len(idx)) < 0.3))
        if changed.any():
            new_g[changed] = draw_gateways(new_m[changed], rng)
        delay = np.clip(rng.lognormal(np.log(90), 0.8, len(idx)), 20, 1800).astype(np.int64)
        idx, cur_m, cur_g, cur_t = idx[retry], new_m[retry], new_g[retry], (cur_t + delay)[retry]

    att = {k: np.concatenate([r[k] for r in rounds]) for k in rounds[0]}
    order = np.lexsort((att["attempt_no"], att["order_idx"]))
    att = {k: v[order][:n_target] for k, v in att.items()}      # exactly n_target attempts

    keep_orders = np.unique(att["order_idx"])
    ords = dict(ts=ts[keep_orders], cat=cat[keep_orders], amt=amt[keep_orders],
                cust=cust[keep_orders], device=device[keep_orders], old_idx=keep_orders)
    return cal, ords, att


# --------------------------------------------------------------------------
# table assembly
# --------------------------------------------------------------------------
def build_tables(n_target: int, n_customers: int, start: str, end: str, seed: int):
    rng = np.random.default_rng(seed)
    cal, ords, att = simulate(n_target, n_customers, start, end, rng)
    base = cal.base
    as_of_sec = cal.max_ts                                   # midnight after END
    n_ord = len(ords["ts"])

    # order ids ascend with order time
    o_sort = np.argsort(ords["ts"], kind="stable")
    rank = np.empty(n_ord, dtype=np.int64)
    rank[o_sort] = np.arange(n_ord)
    old_to_rank = np.full(int(ords["old_idx"].max()) + 1, -1, dtype=np.int64)
    old_to_rank[ords["old_idx"]] = rank
    order_pos = old_to_rank[att["order_idx"]]               # new order position per attempt
    ords_sorted = {k: v[o_sort] for k, v in ords.items()}
    order_id = 100_000_001 + np.arange(n_ord)

    # attempts sorted by payment time -> payment ids ascend with time
    p_sort = np.lexsort((att["attempt_no"], att["ts"]))
    att = {k: v[p_sort] for k, v in att.items()}
    order_pos = order_pos[p_sort]
    n = len(att["ts"])
    payment_id = 500_000_001 + np.arange(n)
    amount = ords_sorted["amt"][order_pos]
    outcome = att["outcome"]
    code = att["code"]
    method = att["method"]

    # ---- payments ------------------------------------------------------------
    status_names = np.array(["SUCCESS", "FAILED", "PENDING"])
    payments = pd.DataFrame({
        "payment_id": payment_id,
        "order_id": order_id[order_pos],
        "customer_id": ords_sorted["cust"][order_pos],
        "payment_method": M_NAMES[method],
        "attempt_no": att["attempt_no"],
        "payment_ts": _fmt_ts(base, att["ts"]),
        "amount": amount,
        "status": status_names[outcome],
        "failure_code": np.where(code >= 0, np.array(FAIL_CODES + [""])[code], ""),
        "device_type": DEVICES[att["device"]],
        "gateway_name": GATEWAYS[att["gw"]],
    })
    payments["failure_code"] = payments["failure_code"].replace("", np.nan)

    # ---- gateway transactions -------------------------------------------------
    u = rng.random(n)
    CAPT, DECL, TOUT = 0, 1, 2
    gw_status = np.full(n, DECL)
    gw_present = np.ones(n, dtype=bool)
    gw_amount = amount.copy()
    dup = np.zeros(n, dtype=bool)

    s = outcome == 0
    gw_status[s] = CAPT
    gw_present[s & (u < 0.0012)] = False                                   # missing record
    dup[s & (u >= 0.0012) & (u < 0.0016)] = True                           # double capture
    gw_status[s & (u >= 0.0016) & (u < 0.0019)] = DECL                     # success but declined
    rnd = s & (u >= 0.0019) & (u < 0.0079)                                 # rounding gap
    delta = rng.integers(1, 100, size=n) / 100 * np.where(rng.random(n) < 0.5, -1, 1)
    gw_amount = np.where(rnd, np.round(amount + delta, 2), gw_amount)
    mat = s & (u >= 0.0079) & (u < 0.0094)                                 # material gap
    gap = np.maximum(2.0, np.round(amount * rng.uniform(0.01, 0.08, size=n), 2))
    gw_amount = np.where(mat, np.where(rng.random(n) < 0.7, amount - gap, amount + gap), gw_amount)

    f = outcome == 1
    tech = f & np.isin(code, [C["GATEWAY_TIMEOUT"], C["NETWORK_ERROR"]])
    gw_status[tech] = TOUT
    cap_p = np.zeros(n)
    for ci, pr in CAPTURED_DESPITE_FAIL.items():
        cap_p[f & (code == ci)] = pr
    cap_not_conf = f & (rng.random(n) < cap_p)
    gw_status[cap_not_conf] = CAPT
    gw_present[f & (code == C["RISK_REJECTED"])] = False                   # never sent to gateway

    pnd = outcome == 2
    v = rng.random(n)
    gw_status[pnd & (v < .55)] = CAPT
    gw_status[pnd & (v >= .55) & (v < .80)] = TOUT
    gw_status[pnd & (v >= .80) & (v < .92)] = DECL
    gw_present[pnd & (v >= .80) & (v < .92)] = False
    pend_captured = pnd & (v < .55)
    captured_unconfirmed = cap_not_conf | pend_captured

    resp = np.where(gw_status == CAPT, "00", np.where(code >= 0, RESP_BY_CODE[np.maximum(code, 0)],
                                                       np.where(gw_status == TOUT, "68", "05")))
    resp = np.where((outcome == 0) & (gw_status == DECL), "05", resp)
    gw_names = np.array(["CAPTURED", "DECLINED", "TIMEOUT"])

    gts = att["ts"] + rng.integers(1, 5, size=n)
    gdate = (base + gts.astype("timedelta64[s]")).astype("datetime64[D]")
    settle = np.busday_offset(gdate, M_SETTLE[method], roll="forward")
    fee = np.round(np.where(gw_status == CAPT, gw_amount * M_FEE[method] / 100, 0.0), 2)

    def gw_frame(mask, ts_sec, status_idx, amt_, fee_, resp_, settle_):
        return pd.DataFrame({
            "payment_id": payment_id[mask], "gateway_name": GATEWAYS[att["gw"][mask]],
            "gateway_ts_sec": ts_sec[mask], "gateway_amount": amt_[mask],
            "gateway_status": gw_names[status_idx[mask]], "response_code": resp_[mask],
            "fee_amount": fee_[mask],
            "settlement_date": np.where(status_idx[mask] == CAPT, settle_[mask].astype("U10"), ""),
        })

    g_main = gw_frame(gw_present, gts, gw_status, gw_amount, fee, resp, settle)
    dup_ts = gts + rng.integers(5, 120, size=n)
    g_dup = gw_frame(dup, dup_ts, np.full(n, CAPT), gw_amount, fee, resp, settle)
    gateway = pd.concat([g_main, g_dup], ignore_index=True).sort_values(
        ["gateway_ts_sec", "payment_id"], kind="stable").reset_index(drop=True)
    gateway.insert(0, "gateway_txn_id", 700_000_001 + np.arange(len(gateway)))
    gateway["gateway_ts"] = _fmt_ts(base, gateway.pop("gateway_ts_sec").to_numpy())
    gateway["settlement_date"] = gateway["settlement_date"].replace("", np.nan)
    gateway = gateway[["gateway_txn_id", "payment_id", "gateway_name", "gateway_ts", "gateway_amount",
                       "gateway_status", "response_code", "fee_amount", "settlement_date"]]

    # ---- orders ---------------------------------------------------------------
    paid = np.zeros(n_ord, dtype=bool)
    pay_idx_of_order = np.full(n_ord, -1, dtype=np.int64)
    sidx = np.where(s)[0]
    paid[order_pos[sidx]] = True
    pay_idx_of_order[order_pos[sidx]] = sidx
    pend_order = np.zeros(n_ord, dtype=bool)
    pend_order[order_pos[np.where(pnd)[0]]] = True

    o_ts = ords_sorted["ts"]
    age_days = (as_of_sec - o_ts) / 86400
    u1, u2 = rng.random(n_ord), rng.random(n_ord)
    cancelled = paid & (u1 < 0.045)
    returned = paid & ~cancelled & (u2 < 0.055) & (age_days > 10)
    in_transit = paid & ~cancelled & ~returned & (age_days <= 6)
    ostatus = np.full(n_ord, "DELIVERED", dtype=object)
    ostatus[in_transit] = "IN_TRANSIT"
    ostatus[cancelled] = "CANCELLED"
    ostatus[returned] = "RETURNED"
    ostatus[~paid] = "PAYMENT_FAILED"
    ostatus[~paid & pend_order] = "PAYMENT_PENDING"

    cat_names = np.array([c[0] for c in CATEGORIES])
    orders = pd.DataFrame({
        "order_id": order_id,
        "customer_id": ords_sorted["cust"],
        "order_ts": _fmt_ts(base, o_ts),
        "order_amount": ords_sorted["amt"],
        "currency": config.CURRENCY,
        "category": cat_names[ords_sorted["cat"]],
        "order_status": ostatus,
    })

    # ---- refunds ---------------------------------------------------------------
    reasons, pay_ix, req_sec, ramt = [], [], [], []

    def add(reason, ix, req, amt_):
        reasons.append(np.full(len(ix), reason, dtype=object))
        pay_ix.append(ix); req_sec.append(req); ramt.append(amt_)

    pi = pay_idx_of_order[cancelled]
    add("CUSTOMER_CANCELLATION", pi, att["ts"][pi] + (rng.uniform(0.5, 40, len(pi)) * 3600).astype(np.int64),
        amount[pi])
    pi = pay_idx_of_order[returned]
    full = rng.random(len(pi)) < 0.85
    r_amt = np.where(full, amount[pi], np.round(amount[pi] * rng.uniform(0.55, 0.9, len(pi)), 2))
    add("RETURN", pi, att["ts"][pi] + (rng.uniform(5, 16, len(pi)) * 86400).astype(np.int64), r_amt)

    pi = np.where(dup)[0]
    pi = pi[rng.random(len(pi)) >= 0.05]                       # 5% never raised
    add("DUPLICATE_CHARGE", pi, dup_ts[pi] + (rng.uniform(1, 6, len(pi)) * 86400).astype(np.int64), amount[pi])
    pi = np.where(captured_unconfirmed)[0]
    pi = pi[rng.random(len(pi)) >= 0.04]                       # 4% never raised
    add("FAILED_PAYMENT_DEBIT", pi, att["ts"][pi] + (rng.uniform(2, 72, len(pi)) * 3600).astype(np.int64),
        amount[pi])

    r_reason = np.concatenate(reasons)
    r_pay = np.concatenate(pay_ix)
    r_req = np.concatenate(req_sec)
    r_amt = np.concatenate(ramt)
    ok = r_req <= as_of_sec                                     # not yet requested otherwise
    r_reason, r_pay, r_req, r_amt = r_reason[ok], r_pay[ok], r_req[ok], r_amt[ok]
    r_sort = np.argsort(r_req, kind="stable")
    r_reason, r_pay, r_req, r_amt = r_reason[r_sort], r_pay[r_sort], r_req[r_sort], r_amt[r_sort]
    nr = len(r_pay)

    r_method = method[r_pay]
    sla_days = M_SLA[r_method]
    breach_base = pd.Series(r_reason).map({"CUSTOMER_CANCELLATION": .07, "RETURN": .12,
                                           "DUPLICATE_CHARGE": .22, "FAILED_PAYMENT_DEBIT": .28}).to_numpy()
    method_mult = np.array([0.9, 1.3, 1.3, 1.1, 0.6, 1.3])[r_method]
    r_day = np.minimum(r_req // 86400, cal.n_days - 1)
    sale_mult = np.where(cal.fail_mult[r_day] > 1, 1.3, 1.0)
    p_breach = np.clip(breach_base * method_mult * sale_mult, 0, 0.6)
    breach = rng.random(nr) < p_breach
    proc_days = np.where(breach,
                         sla_days * (1 + np.minimum(rng.exponential(0.9, nr), 3.5)),
                         sla_days * rng.uniform(0.08, 0.98, nr))
    proc_sec = r_req + (proc_days * 86400).astype(np.int64)
    stuck = rng.random(nr) < 0.012
    rej = (~stuck) & (rng.random(nr) < 0.010)
    proc_sec = np.where(rej, r_req + (rng.uniform(0.5, 2, nr) * 86400).astype(np.int64), proc_sec)
    done = (~stuck) & (proc_sec <= as_of_sec)
    r_status = np.where(done, np.where(rej, "FAILED", "PROCESSED"), "PENDING")
    refunds = pd.DataFrame({
        "refund_id": 800_000_001 + np.arange(nr),
        "payment_id": payment_id[r_pay],
        "order_id": order_id[order_pos[r_pay]],
        "refund_amount": r_amt,
        "refund_reason": r_reason,
        "refund_requested_ts": _fmt_ts(base, r_req),
        "refund_status": r_status,
        "refund_processed_ts": np.where(done, _fmt_ts(base, proc_sec), ""),
        "refund_sla_due_ts": _fmt_ts(base, r_req + sla_days * 86400),
    })
    refunds["refund_processed_ts"] = refunds["refund_processed_ts"].replace("", np.nan)

    # ---- customers --------------------------------------------------------------
    first_order = pd.Series(o_ts).groupby(ords_sorted["cust"]).min()
    state_p = np.array([s_[1] for s_ in STATES])
    signup = (np.datetime64("2019-01-01") +
              rng.integers(0, (np.datetime64(end) - np.datetime64("2019-01-01")).astype(int), n_customers)
              ).astype("datetime64[D]")
    cid = np.arange(1, n_customers + 1)
    fo = first_order.reindex(cid)
    first_day = (base + fo.fillna(0).to_numpy().astype(np.int64).astype("timedelta64[s]")).astype("datetime64[D]")
    signup = np.where(fo.notna().to_numpy() & (signup > first_day), first_day, signup).astype("datetime64[D]")
    customers = pd.DataFrame({
        "customer_id": cid,
        "signup_date": signup.astype("U10"),
        "state": np.array([s_[0] for s_ in STATES])[rng.choice(len(STATES), n_customers, p=state_p)],
        "city_tier": np.array(["Tier-1", "Tier-2", "Tier-3"])[rng.choice(3, n_customers, p=[.35, .35, .30])],
        "is_prime": (rng.random(n_customers) < 0.38).astype(int),
    })
    # keep only customers who have placed an order (every customer row is "active")
    customers = customers[customers["customer_id"].isin(orders["customer_id"].unique())].reset_index(drop=True)

    methods = pd.DataFrame([(m[0], m[1], m[2], m[3], m[4], m[5]) for m in METHODS],
                           columns=["method_id", "method_name", "method_type", "gateway_fee_pct",
                                    "settlement_days", "refund_sla_days"])
    as_of = str(_fmt_ts(base, np.array([as_of_sec]))[0])
    return dict(customers=customers, payment_methods=methods, orders=orders, payments=payments,
                gateway_transactions=gateway, refunds=refunds), as_of


# --------------------------------------------------------------------------
# raw-extract noise
# --------------------------------------------------------------------------
def _case_variants(series: pd.Series, rng, frac: float, extra: dict | None = None) -> pd.Series:
    out = series.astype(object).copy()
    n = len(out)
    pick = rng.random(n) < frac
    kind = rng.integers(0, 4, size=n)
    for i in np.where(pick)[0]:
        v = str(out.iat[i])
        k = kind[i]
        if extra and v in extra and k == 3:
            out.iat[i] = extra[v]
        elif k == 0:
            out.iat[i] = v.lower()
        elif k == 1:
            out.iat[i] = f"  {v} "
        elif k == 2:
            out.iat[i] = v.replace("_", " ")
        else:
            out.iat[i] = v.title()
    return out


def _money_text(amount: pd.Series, rng, frac: float) -> pd.Series:
    out = amount.map(lambda x: f"{x:.2f}").astype(object)
    pick = np.where(rng.random(len(out)) < frac)[0]
    style = rng.integers(0, 3, size=len(out))
    for i in pick:
        x = amount.iat[i]
        out.iat[i] = (f"₹{x:,.2f}", f"Rs. {x:.2f}", f"INR {x:,.2f}")[style[i]]
    return out


def add_noise(t: dict, seed: int) -> dict:
    """Return raw-extract versions of the tables with realistic defects."""
    rng = np.random.default_rng(seed + 1)
    raw = {k: v.copy() for k, v in t.items()}

    def sample(df, frac):
        k = max(1, int(len(df) * frac))
        return df.iloc[rng.choice(len(df), size=k, replace=False)].copy()

    # customers
    c = raw["customers"]
    c["state"] = _case_variants(c["state"], rng, 0.02)
    c["city_tier"] = c["city_tier"].astype(object)
    c.loc[rng.random(len(c)) < 0.005, "city_tier"] = np.nan
    raw["customers"] = pd.concat([c, sample(c, 0.001)], ignore_index=True)

    # orders
    o = raw["orders"]
    o["category"] = _case_variants(o["category"], rng, 0.01)
    o["order_amount"] = _money_text(t["orders"]["order_amount"], rng, 0.002)
    dup_o = sample(o, 0.001)
    orphan = sample(o, 0.0002); orphan["order_id"] = 990_000_001 + np.arange(len(orphan))
    orphan["customer_id"] = 99_999_999
    neg = sample(o, 0.0001); neg["order_id"] = 991_000_001 + np.arange(len(neg)); neg["order_amount"] = "-499.00"
    raw["orders"] = pd.concat([o, dup_o, orphan, neg], ignore_index=True)

    # payments
    p = raw["payments"]
    p["payment_method"] = _case_variants(p["payment_method"], rng, 0.01, extra={"NET_BANKING": "NETBANKING"})
    p["status"] = _case_variants(p["status"], rng, 0.01, extra={"SUCCESS": "SUCCESSFUL", "FAILED": "FAILURE"})
    p["amount"] = _money_text(t["payments"]["amount"], rng, 0.003)
    alt = np.where(rng.random(len(p)) < 0.002)[0]
    if len(alt):
        p["payment_ts"] = p["payment_ts"].astype(object)
        alt_ts = pd.to_datetime(p["payment_ts"].iloc[alt]).dt.strftime("%d/%m/%Y %H:%M:%S")
        p.iloc[alt, p.columns.get_loc("payment_ts")] = alt_ts.to_numpy()
    nid = 992_000_001

    def mk(df, frac, **chg):
        nonlocal nid
        x = sample(df, frac)
        x["payment_id"] = nid + np.arange(len(x)); nid += len(x)
        for k, v in chg.items():
            x[k] = v(x) if callable(v) else v
        return x

    dup_p = sample(p, 0.002)
    conflict = sample(p, 0.0002)
    conflict["amount"] = (t["payments"].loc[conflict.index, "amount"] * 1.1).round(2).map(lambda x: f"{x:.2f}")
    neg_p = mk(p, 0.0002, amount="-250.00")
    zero_p = mk(p, 0.0001, amount="0.00")
    orphan_p = mk(p, 0.0002, order_id=lambda x: 999_000_000 + np.arange(len(x)))
    future_p = mk(p, 0.0001, payment_ts="2028-01-15 10:00:00")
    before_p = mk(p, 0.0001, payment_ts=lambda x: _shift_back(t["payments"].loc[x.index, "payment_ts"]))
    raw["payments"] = pd.concat([p, dup_p, conflict, neg_p, zero_p, orphan_p, future_p, before_p],
                                ignore_index=True)

    # gateway
    g = raw["gateway_transactions"]
    g["gateway_status"] = _case_variants(g["gateway_status"], rng, 0.01)
    g["gateway_amount"] = _money_text(t["gateway_transactions"]["gateway_amount"], rng, 0.001)
    raw["gateway_transactions"] = pd.concat([g, sample(g, 0.0015)], ignore_index=True)

    # refunds
    r = raw["refunds"]
    r["refund_status"] = _case_variants(r["refund_status"], rng, 0.01)
    over = sample(r, 0.0003); over["refund_id"] = 993_000_001 + np.arange(len(over))
    pay_amt = t["payments"].set_index("payment_id")["amount"]      # guaranteed to exceed the payment
    over["refund_amount"] = (pay_amt.loc[over["payment_id"]].to_numpy() * 1.5 + 10).round(2)
    orph_r = sample(r, 0.0003); orph_r["refund_id"] = 994_000_001 + np.arange(len(orph_r))
    orph_r["payment_id"] = 999_999_999
    raw["refunds"] = pd.concat([r, sample(r, 0.002), over, orph_r], ignore_index=True)
    return raw


def _shift_back(ts: pd.Series) -> pd.Series:
    return (pd.to_datetime(ts) - pd.Timedelta(days=5)).dt.strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------
def write_csvs(tables: dict, out: Path):
    out.mkdir(parents=True, exist_ok=True)
    for name, df in tables.items():
        df.to_csv(out / f"{name}.csv", index=False)
        log.info("wrote %-24s %9s rows -> %s", name, f"{len(df):,}", out / f"{name}.csv")


def generate(n_transactions=config.N_TRANSACTIONS, n_customers=config.N_CUSTOMERS, seed=config.SEED,
             start=config.START_DATE, end=config.END_DATE, out=config.DATA_RAW, truth_dir=None,
             noise=True) -> dict:
    t0 = time.time()
    out = Path(out)
    log.info("generating %s payment attempts (seed=%s, %s..%s)", f"{n_transactions:,}", seed, start, end)
    truth, as_of = build_tables(n_transactions, n_customers, start, end, seed)
    if truth_dir:
        write_csvs(truth, Path(truth_dir))
    raw = add_noise(truth, seed) if noise else truth
    write_csvs(raw, out)
    manifest = dict(seed=seed, start=start, end=end, as_of=as_of, currency=config.CURRENCY,
                    noise=noise, rows={k: int(len(v)) for k, v in raw.items()},
                    clean_rows={k: int(len(v)) for k, v in truth.items()},
                    scenario=dict(sale_mid=SALE_MID, sale_festive=SALE_FESTIVE,
                                  incident_gateway=INCIDENT_GATEWAY, incident_netbanking=INCIDENT_NETBANK),
                    generated_seconds=round(time.time() - t0, 1))
    (out / "_manifest.json").write_text(json.dumps(manifest, indent=2))
    log.info("done in %.1fs", time.time() - t0)
    return manifest


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--transactions", type=int, default=config.N_TRANSACTIONS)
    ap.add_argument("--customers", type=int, default=config.N_CUSTOMERS)
    ap.add_argument("--seed", type=int, default=config.SEED)
    ap.add_argument("--start", default=config.START_DATE)
    ap.add_argument("--end", default=config.END_DATE)
    ap.add_argument("--out", default=str(config.DATA_RAW))
    ap.add_argument("--truth-dir", default=None, help="also save the pre-noise tables (for ETL testing)")
    ap.add_argument("--no-noise", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    generate(a.transactions, a.customers, a.seed, a.start, a.end, a.out, a.truth_dir, not a.no_noise)


if __name__ == "__main__":
    main()
