"""Build the management Excel report (output/PaymentOps_Management_Report.xlsx).

Every number comes from the named SQL queries in sql/03-06 - the same SQL the
Power BI dashboard uses - so the report, dashboard and ad-hoc SQL cannot disagree.
The 'Recommendations' sheet carries live Excel formulas: change an assumption
(yellow cells) and the recovered-value estimate recalculates.

Run:  python -m src.build_report [--target sqlite|mysql]
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

import pandas as pd
import xlsxwriter

from . import config, db

log = logging.getLogger("report")

NAVY, ORANGE, GREY, RED, GREEN = "#232F3E", "#FF9900", "#F2F3F3", "#C0392B", "#2E7D32"


def inr(x: float) -> str:
    """Indian-style short money text."""
    x = float(x)
    if abs(x) >= 1e7:
        return f"INR {x / 1e7:,.2f} Cr"
    if abs(x) >= 1e5:
        return f"INR {x / 1e5:,.2f} L"
    return f"INR {x:,.0f}"


def load_all(conn) -> dict[str, pd.DataFrame]:
    return {name: db.run_query(conn, name) for name, spec in db.all_queries().items()
            if not name.startswith("dq_")}


# --------------------------------------------------------------------------
# key findings (generated from the data, never hand-typed numbers)
# --------------------------------------------------------------------------
def key_findings(d: dict) -> list[tuple[str, str]]:
    k, fa = d["kpi_headline"].iloc[0], d["kpi_first_attempt_success"].iloc[0]
    fm, pareto = d["fail_by_method"], d["fail_reason_pareto"]
    rh, rs = d["recon_headline"].iloc[0], d["recon_by_status"]
    rg, rf = d["recon_ageing"], d["refund_headline"].iloc[0]
    ci = d["cust_order_impact"].iloc[0]
    held = d["recon_customer_money_held"]
    sc = d["proc_failure_reduction_scenarios"]
    ratio = fm.iloc[0].failure_rate_pct / fm.sort_values("failure_rate_pct").iloc[0].failure_rate_pct
    top3 = pareto.head(3)
    inc = d["fail_incident_hours"]
    inc_txt = ""
    if len(inc):
        g = inc.iloc[0]
        same = inc[(inc.gateway_name == g.gateway_name) & (inc.payment_date == g.payment_date)]
        inc_txt = (f" Incident detector flags {g.gateway_name} on {g.payment_date} "
                   f"({int(same.payment_hour.min())}:00-{int(same.payment_hour.max())}:59, "
                   f"up to {same.failure_rate_pct.max():.0f}% failures vs {g.gateway_baseline_pct:.1f}% norm).")
    old = rg[rg.bucket_no >= 4]
    refunds_late = d["refund_by_reason"].iloc[0]
    return [
        ("Payment performance",
         f"{k.success_rate_pct:.2f}% of {int(k.attempts):,} attempts succeed ({k.failure_rate_pct:.2f}% fail, "
         f"{int(k.pending_attempts):,} still pending). {fa.first_attempt_success_pct:.2f}% of orders pay first time; "
         f"{fa.eventual_success_pct:.2f}% eventually."),
        ("Failure hot-spots",
         f"{fm.iloc[0].method_name} fails most ({fm.iloc[0].failure_rate_pct:.2f}%), {ratio:.1f}x the best method "
         f"({fm.sort_values('failure_rate_pct').iloc[0].method_name}). Top 3 reasons "
         f"({', '.join(top3.failure_code)}) are {top3.share_pct.sum():.0f}% of all failures.{inc_txt}"),
        ("Reconciliation",
         f"{rh.match_rate_pct:.2f}% of payments reconcile cleanly. {int(rh.breaks):,} breaks; "
         f"{inr(rh.unresolved_value_material)} across {int(rs[rs.severity != 'LOW'].unresolved_items.sum()):,} material breaks "
         f"is still unresolved. Largest bucket: {rs.iloc[0].recon_status} ({inr(rs.iloc[0].unresolved_value)})."),
        ("Customer money held",
         f"Gateway captured money for {int(held['items'].sum()):,} payments the order shows as failed or duplicated; "
         f"{inr(held.still_with_us_value.sum())} has not yet been refunded - the highest-priority customer-harm item."),
        ("Ageing risk",
         f"{inr(old.unresolved_value.sum())} ({int(old['items'].sum()):,} items) of unresolved breaks is older than 90 days; "
         f"old breaks are the hardest to recover from gateways."),
        ("Refund operations",
         f"{int(rf.pending):,} refunds pending, {int(rf.pending_overdue):,} past SLA worth {inr(rf.overdue_pending_value)}; "
         f"{rf.late_processed_pct:.1f}% of processed refunds were late. Worst reason: "
         f"{refunds_late.refund_reason} ({refunds_late.sla_breach_pct:.1f}% breach)."),
        ("Customer impact",
         f"{ci.pct_orders_hit:.2f}% of orders hit a payment failure; {100 - ci.retry_recovery_pct:.1f}% of them were abandoned, "
         f"losing {inr(ci.lost_order_value)} of order value."),
        ("Improvement sizing (assumption-based)",
         f"If failures fall by {', '.join(f'{r.failure_category} {int(r.reduction_assumption_pct)}%' for r in sc.itertuples() if r.reduction_assumption_pct)}, about "
         f"{inr(sc.potential_recovered_value.sum())} of lost order value is recoverable. Percentages are assumptions - edit them "
         f"on the Recommendations sheet."),
    ]


# --------------------------------------------------------------------------
# workbook helpers
# --------------------------------------------------------------------------
class Book:
    def __init__(self, path: Path):
        self.wb = xlsxwriter.Workbook(str(path), {"nan_inf_to_errors": True, "default_date_format": "yyyy-mm-dd"})
        w = self.wb
        self.f = dict(
            title=w.add_format({"bold": True, "font_size": 18, "font_color": NAVY}),
            sub=w.add_format({"italic": True, "font_color": "#666666"}),
            h2=w.add_format({"bold": True, "font_size": 12, "font_color": "white", "bg_color": NAVY}),
            head=w.add_format({"bold": True, "bg_color": ORANGE, "font_color": NAVY, "border": 1, "text_wrap": True, "valign": "vcenter"}),
            text=w.add_format({"border": 1}),
            wrap=w.add_format({"border": 1, "text_wrap": True, "valign": "top"}),
            int=w.add_format({"border": 1, "num_format": "#,##0"}),
            money=w.add_format({"border": 1, "num_format": "#,##0"}),
            pct=w.add_format({"border": 1, "num_format": "0.00"}),
            dec=w.add_format({"border": 1, "num_format": "0.00"}),
            input=w.add_format({"border": 1, "bg_color": "#FFF2CC", "num_format": "0", "bold": True}),
            kpi_v=w.add_format({"bold": True, "font_size": 20, "font_color": NAVY, "align": "center", "bg_color": GREY, "border": 1}),
            kpi_l=w.add_format({"font_size": 9, "font_color": "#555555", "align": "center", "bg_color": GREY, "border": 1, "text_wrap": True}),
            bold=w.add_format({"bold": True, "border": 1, "num_format": "#,##0", "valign": "top", "text_wrap": True}),
            link=w.add_format({"font_color": "blue", "underline": 1}),
        )

    def fmt_for(self, col: str, series: pd.Series):
        c = col.lower()
        if "pct" in c or c.endswith("_rate"):
            return self.f["pct"]
        if any(t in c for t in ("value", "amount", "diff", "exposure")):
            return self.f["money"]
        if pd.api.types.is_integer_dtype(series):
            return self.f["int"]
        if pd.api.types.is_float_dtype(series):
            return self.f["dec"]
        return self.f["text"]

    def table(self, ws, df: pd.DataFrame, row: int, col: int = 0, title: str | None = None,
              note: str | None = None) -> dict:
        """Write df at (row, col). Returns dict with first/last data row and column index map."""
        f = self.f
        if title:
            ws.merge_range(row, col, row, col + max(len(df.columns) - 1, 3), title, f["h2"])
            row += 1
        if note:
            ws.write(row, col, note, f["sub"])
            row += 1
        for j, c in enumerate(df.columns):
            ws.write(row, col + j, c.replace("_", " "), f["head"])
        for i, rec in enumerate(df.itertuples(index=False), start=1):
            for j, v in enumerate(rec):
                cf = self.fmt_for(df.columns[j], df.iloc[:, j])
                if pd.isna(v):
                    ws.write_blank(row + i, col + j, None, cf)
                else:
                    ws.write(row + i, col + j, v, cf)
        for j, c in enumerate(df.columns):
            longest = max([len(str(c)) * 0.9] + [len(str(x)) for x in df.iloc[:, j].head(200)])
            ws.set_column(col + j, col + j, min(max(ws_width.get((ws.get_name(), col + j), 0), longest + 2, 11), 60))
            ws_width[(ws.get_name(), col + j)] = min(max(longest + 2, 11), 60)
        return dict(first=row + 1, last=row + len(df), header=row, col={c: col + j for j, c in enumerate(df.columns)},
                    next_row=row + len(df) + 3)

    def chart(self, ws, kind: str, t: dict, cat: str, vals: list[str], title: str, anchor: str,
              ytitle: str = "", sub: str | None = None, size=(620, 300), name: str = None):
        sheet = ws.get_name()
        ch = self.wb.add_chart({"type": kind, **({"subtype": sub} if sub else {})})
        palette = [ORANGE, NAVY, "#146EB4", "#7F8C8D"]
        for i, v in enumerate(vals):
            series = {"name": [sheet, t["header"], t["col"][v]],
                      "categories": [sheet, t["first"], t["col"][cat], t["last"], t["col"][cat]],
                      "values": [sheet, t["first"], t["col"][v], t["last"], t["col"][v]]}
            if kind == "line":
                series["line"] = {"color": palette[i % 4], "width": 2.25}
            else:
                series["fill"] = {"color": palette[i % 4]}
            ch.add_series(series)
        ch.set_title({"name": title, "name_font": {"size": 11, "color": NAVY}})
        ch.set_y_axis({"name": ytitle, "major_gridlines": {"visible": True, "line": {"color": "#DDDDDD"}}})
        ch.set_legend({"none": True} if len(vals) == 1 else {"position": "bottom"})
        ch.set_size({"width": size[0], "height": size[1]})
        ws.insert_chart(anchor, ch)


ws_width: dict = {}


# --------------------------------------------------------------------------
def build(d: dict, dq: pd.DataFrame, as_of: str, out_path: Path, summary: dict | None = None) -> Path:
    bk = Book(out_path)
    f, wb = bk.f, bk.wb

    # ---------------- Summary ----------------
    ws = wb.add_worksheet("Summary")
    ws.hide_gridlines(2)
    ws.set_column(0, 0, 34); ws.set_column(1, 8, 17)
    ws.write(0, 0, "Amazon-style PaymentOps - Management Report", f["title"])
    ws.write(1, 0, f"Simulated marketplace data (not Amazon data) | as of {as_of} | currency INR", f["sub"])
    k, rh, rf, ci = (d["kpi_headline"].iloc[0], d["recon_headline"].iloc[0],
                     d["refund_headline"].iloc[0], d["cust_order_impact"].iloc[0])
    tiles = [("Payment success rate", f"{k.success_rate_pct:.2f}%"),
             ("Payment attempts", f"{int(k.attempts):,}"),
             ("Reconciliation match rate", f"{rh.match_rate_pct:.2f}%"),
             ("Unresolved recon value", inr(rh.unresolved_value_material)),
             ("Overdue pending refunds", f"{int(rf.pending_overdue):,}"),
             ("Orders hit by a failure", f"{ci.pct_orders_hit:.2f}%"),
             ("Lost order value", inr(ci.lost_order_value))]
    for j, (lab, val) in enumerate(tiles):
        ws.write(3, j, val, f["kpi_v"]); ws.write(4, j, lab, f["kpi_l"])
    ws.set_row(3, 34); ws.set_row(4, 28)
    ws.merge_range(6, 0, 6, 6, "Key findings", f["h2"])
    for i, (head, text) in enumerate(key_findings(d)):
        ws.write(7 + i, 0, head, f["bold"])
        ws.merge_range(7 + i, 1, 7 + i, 6, text, f["wrap"])
        ws.set_row(7 + i, 48)
    r = 7 + 8 + 1
    ws.merge_range(r, 0, r, 6, "Sheets", f["h2"])
    for i, (nm, desc) in enumerate([
            ("Payment Performance", "Q1 success rate, trends, hourly and attempt funnel"),
            ("Failures", "Q2 failure rate by method / gateway / device / reason; incidents"),
            ("Reconciliation", "Q3 + Q6 internal vs gateway breaks, unresolved value, ageing"),
            ("Refunds", "Q4 pending and delayed refunds"),
            ("Customer Impact", "Q5 orders affected, recovery and abandonment"),
            ("Recommendations", "Q7 sized operational changes (editable assumptions)"),
            ("Work Lists", "Largest open breaks and overdue refunds for the ops team"),
            ("Data Quality", "What the ETL rejected and standardised"),
            ("Definitions", "KPI definitions, reconciliation rules, assumptions")]):
        ws.write_url(r + 1 + i, 0, f"internal:'{nm}'!A1", f["link"], nm)
        ws.write(r + 1 + i, 1, desc)

    # ---------------- Payment Performance ----------------
    ws = wb.add_worksheet("Payment Performance"); ws.hide_gridlines(2)
    ws.write(0, 0, "Q1 - What percentage of payments succeed?", f["title"])
    t = bk.table(ws, d["kpi_monthly_trend"], 2, 0, "Monthly trend")
    bk.chart(ws, "line", t, "month", ["success_rate_pct"], "Success rate % by month", "I3", "%")
    t2 = bk.table(ws, d["kpi_attempt_funnel"], t["next_row"], 0, "Success by attempt number",
                  "Retry success is about the same as first-attempt success: failed customers are not being routed any differently on retry.")
    t3 = bk.table(ws, d["kpi_hourly_profile"], t2["next_row"], 0, "Failure rate by hour of day")
    bk.chart(ws, "column", t3, "payment_hour", ["failure_rate_pct"], "Failure rate % by hour", "I20", "%")
    t4 = bk.table(ws, d["kpi_first_attempt_success"], t3["next_row"], 0, "Orders: first-attempt vs eventual success")
    ws.freeze_panes(2, 0)

    # ---------------- Failures ----------------
    ws = wb.add_worksheet("Failures"); ws.hide_gridlines(2)
    ws.write(0, 0, "Q2 - Which methods have the highest failure rates?", f["title"])
    t = bk.table(ws, d["fail_by_method"], 2, 0, "By payment method")
    bk.chart(ws, "bar", t, "method_name", ["failure_rate_pct"], "Failure rate % by method", "I3", "%")
    t = bk.table(ws, d["fail_by_gateway"], t["next_row"], 0, "By gateway",
                 "PayGate-B is worse even outside the 15-Jul outage; see the incident detectors below for the outage itself.")
    t = bk.table(ws, d["fail_by_device"], t["next_row"], 0, "By device")
    t = bk.table(ws, d["fail_by_ticket_band"], t["next_row"], 0, "By order value band (INR)")
    t = bk.table(ws, d["fail_by_customer_segment"], t["next_row"], 0, "By customer segment")
    t = bk.table(ws, d["fail_method_gateway_matrix"], t["next_row"], 0, "Method x gateway")
    tp = bk.table(ws, d["fail_reason_pareto"], t["next_row"], 0, "Failure reasons (Pareto)")
    bk.chart(ws, "column", tp, "failure_code", ["failed_attempts"], "Failed attempts by reason", "I22", "attempts", size=(720, 320))
    t = bk.table(ws, d["fail_category_by_method"], tp["next_row"], 0, "Failure category mix per method")
    t = bk.table(ws, d["fail_incident_hours"], t["next_row"], 0, "Gateway incident detector (>=3x the gateway norm)")
    t = bk.table(ws, d["fail_incident_by_method_hour"], t["next_row"], 0, "Method incident detector (>=3x the method norm)")
    ws.freeze_panes(2, 0)

    # ---------------- Reconciliation ----------------
    ws = wb.add_worksheet("Reconciliation"); ws.hide_gridlines(2)
    ws.write(0, 0, "Q3 / Q6 - Do internal and gateway amounts match? What is unresolved?", f["title"])
    t = bk.table(ws, d["recon_headline"].T.reset_index().rename(columns={"index": "metric", 0: "value"}), 2, 0, "Scorecard")
    ws.set_column(0, 0, 30)
    t = bk.table(ws, d["recon_by_status"], t["next_row"], 0, "Breaks by type (value INR)")
    bk.chart(ws, "bar", t, "recon_status", ["unresolved_value"], "Unresolved value by break type (INR)", "L3", "INR", size=(700, 320))
    t = bk.table(ws, d["recon_by_resolution"], t["next_row"], 0, "Resolution status")
    ta = bk.table(ws, d["recon_ageing"], t["next_row"], 0, "Ageing of unresolved material breaks")
    bk.chart(ws, "column", ta, "age_bucket", ["unresolved_value"], "Unresolved value by age (INR)", "L22", "INR", size=(700, 300))
    t = bk.table(ws, d["recon_customer_money_held"], ta["next_row"], 0, "Customers charged but order not paid (needs refund)")
    t = bk.table(ws, d["recon_amount_mismatch_detail"], t["next_row"], 0, "Amount mismatches: direction and size")
    t = bk.table(ws, d["recon_by_gateway"], t["next_row"], 0, "By gateway")
    t = bk.table(ws, d["recon_by_method"], t["next_row"], 0, "By payment method")
    t = bk.table(ws, d["recon_monthly"], t["next_row"], 0, "By payment month")

    # ---------------- Refunds ----------------
    ws = wb.add_worksheet("Refunds"); ws.hide_gridlines(2)
    ws.write(0, 0, "Q4 - Which refunds are pending or delayed?", f["title"])
    t = bk.table(ws, d["refund_headline"].T.reset_index().rename(columns={"index": "metric", 0: "value"}), 2, 0, "Scorecard")
    ws.set_column(0, 0, 30)
    t = bk.table(ws, d["refund_by_reason"], t["next_row"], 0, "By refund reason")
    bk.chart(ws, "column", t, "refund_reason", ["sla_breach_pct"], "SLA breach % by reason", "L3", "%", size=(620, 300))
    t = bk.table(ws, d["refund_by_method"], t["next_row"], 0, "By original payment method",
                 "Slow rails (cards, EMI) have the longest SLAs but also the worst breach rates.")
    ta = bk.table(ws, d["refund_pending_ageing"], t["next_row"], 0, "Pending refunds by days past SLA")
    bk.chart(ws, "column", ta, "bucket", ["pending_value"], "Pending refund value by overdue bucket (INR)", "L22", "INR", size=(620, 300))
    t = bk.table(ws, d["refund_by_gateway"], ta["next_row"], 0, "By gateway")
    t = bk.table(ws, d["refund_monthly"], t["next_row"], 0, "By request month")

    # ---------------- Customer Impact ----------------
    ws = wb.add_worksheet("Customer Impact"); ws.hide_gridlines(2)
    ws.write(0, 0, "Q5 - How often do payment failures affect orders?", f["title"])
    t = bk.table(ws, d["cust_order_impact"].T.reset_index().rename(columns={"index": "metric", 0: "value"}), 2, 0, "Order impact")
    ws.set_column(0, 0, 30)
    t = bk.table(ws, d["cust_impact_by_segment"], t["next_row"], 0, "By Prime status")
    tc = bk.table(ws, d["cust_recovery_by_first_failure"], t["next_row"], 0, "Recovery by first failure type",
                  "RISK rejections rarely recover (by design); TECHNICAL failures recover best but are the most avoidable.")
    bk.chart(ws, "column", tc, "failure_category", ["lost_order_value"], "Lost order value by first failure type (INR)", "L3", "INR")
    t = bk.table(ws, d["cust_recovery_by_method"], tc["next_row"], 0, "Recovery by method of first failed attempt")

    # ---------------- Recommendations ----------------
    ws = wb.add_worksheet("Recommendations"); ws.hide_gridlines(2)
    ws.write(0, 0, "Q7 - What operational changes could reduce failures?", f["title"])
    ws.write(1, 0, "Yellow cells are ASSUMPTIONS. Change them and the recovered value recalculates (Excel formulas).", f["sub"])
    sc = d["proc_failure_reduction_scenarios"]
    heads = ["failure category", "lost order value (INR)", "assumed reduction %", "potential recovered (INR)", "proposed change"]
    ws.merge_range(3, 0, 3, 4, "Failure reduction scenarios", f["h2"])
    for j, h in enumerate(heads):
        ws.write(4, j, h, f["head"])
    for i, rec in enumerate(sc.itertuples(index=False)):
        r = 5 + i
        ws.write(r, 0, rec.failure_category, f["text"])
        ws.write(r, 1, rec.lost_order_value, f["money"])
        ws.write(r, 2, rec.reduction_assumption_pct, f["input"])
        ws.write_formula(r, 3, f"=B{r + 1}*C{r + 1}/100", f["money"], rec.potential_recovered_value)
        ws.write(r, 4, rec.proposed_change, f["wrap"])
    tr = 5 + len(sc)
    ws.write(tr, 0, "TOTAL", f["bold"])
    ws.write_formula(tr, 1, f"=SUM(B6:B{tr})", f["bold"], sc.lost_order_value.sum())
    ws.write_formula(tr, 3, f"=SUM(D6:D{tr})", f["bold"], sc.potential_recovered_value.sum())
    ws.set_column(0, 0, 24); ws.set_column(1, 3, 22); ws.set_column(4, 4, 70)
    t = bk.table(ws, d["proc_gateway_routing_opportunity"], tr + 3, 0, "Smart-routing opportunity (method x gateway vs best gateway for that method)",
                 "avoidable_failures = failures above what the best gateway achieves for the same method.")
    t = bk.table(ws, d["proc_sale_event_pressure"], t["next_row"], 0, "Capacity pressure: busiest days vs others")
    r = t["next_row"]
    ws.merge_range(r, 0, r, 4, "Prioritised action plan", f["h2"])
    plan = pd.DataFrame([
        (1, "Auto-refund captured-but-not-confirmed and duplicate captures", "Recon", "Customer harm, trust", "Low", "Daily job over v_reconciliation WHERE resolution_status <> RESOLVED_REFUNDED"),
        (2, "Gateway status-query job for STUCK_PENDING > 24h", "Recon", "Closes largest unresolved bucket", "Low", "Poll gateway API, update payment status, escalate residue"),
        (3, "Automatic failover / smart routing across gateways", "Failures", "Avoidable failures + outage protection", "Medium", "Route by method x gateway health; trip breaker at 3x baseline"),
        (4, "Real-time incident alert (gateway-hour and method-hour detectors)", "Failures", "Cuts outage duration", "Low", "Run fail_incident_* queries every 15 minutes"),
        (5, "Refund SLA escalation queue, oldest and largest first", "Refunds", "Overdue refund value", "Low", "Use refund_overdue_worklist; auto-escalate at SLA - 1 day"),
        (6, "Payment-method nudges after customer-action failures", "Customer", "Recovered orders", "Medium", "Offer UPI / wallet switch after INSUFFICIENT_FUNDS or INVALID_OTP"),
        (7, "Webhook retry + missing-gateway-record alert", "Recon", "Missing gateway records", "Medium", "Dead-letter queue for gateway callbacks; daily file diff")],
        columns=["priority", "action", "area", "benefit", "effort", "how"])
    for j, c in enumerate(plan.columns):
        ws.write(r + 1, j, c, f["head"])
    for i, rec in enumerate(plan.itertuples(index=False)):
        for j, v in enumerate(rec):
            ws.write(r + 2 + i, j, v, f["wrap"])

    # ---------------- Work Lists ----------------
    ws = wb.add_worksheet("Work Lists"); ws.hide_gridlines(2)
    ws.write(0, 0, "Work lists for the operations team", f["title"])
    t = bk.table(ws, d["recon_top_breaks"], 2, 0, "Top 100 unresolved reconciliation breaks")
    ws.autofilter(t["header"], 0, t["last"], len(d["recon_top_breaks"].columns) - 1)
    ws2 = wb.add_worksheet("Refund Work List"); ws2.hide_gridlines(2)
    ws2.write(0, 0, "Top 100 overdue pending refunds", f["title"])
    t = bk.table(ws2, d["refund_overdue_worklist"], 2, 0)
    ws2.autofilter(t["header"], 0, t["last"], len(d["refund_overdue_worklist"].columns) - 1)

    # ---------------- Data Quality ----------------
    ws = wb.add_worksheet("Data Quality"); ws.hide_gridlines(2)
    ws.write(0, 0, "ETL data-quality report", f["title"])
    ws.write(1, 0, "Rows rejected go to output/rejects/*.csv with the rule that caught them. 'standardised' = fixed in place.", f["sub"])
    bk.table(ws, dq, 3, 0)

    # ---------------- Definitions ----------------
    ws = wb.add_worksheet("Definitions"); ws.hide_gridlines(2)
    ws.write(0, 0, "Definitions, rules and assumptions", f["title"])
    defs = pd.DataFrame([
        ("Attempt", "One row in payments. An order can have several attempts (retries)."),
        ("Success rate", "SUCCESS attempts / all attempts. PENDING counts in the denominator."),
        ("Failure rate", "FAILED attempts / all attempts."),
        ("First-attempt success", "Orders whose attempt 1 succeeded / all orders."),
        ("Abandoned order", "Order with at least one failed attempt and no successful attempt."),
        ("MATCHED", "Internal status and gateway capture agree and amounts are equal."),
        ("ROUNDING_DIFF", f"Success with amount gap <= {config.RECON_TOLERANCE:.2f} (low severity, auto-adjust)."),
        ("AMOUNT_MISMATCH", f"Success with amount gap > {config.RECON_TOLERANCE:.2f}."),
        ("CAPTURED_NOT_CONFIRMED", "Gateway captured money but internal status is not SUCCESS. Customer charged, order unpaid."),
        ("DUPLICATE_CAPTURE", "More than one captured gateway row for one payment. Exposure = the extra capture."),
        ("SUCCESS_NOT_CAPTURED", "Internal SUCCESS but the gateway never captured. Revenue at risk."),
        ("SUCCESS_NO_GATEWAY_RECORD", "Internal SUCCESS and no gateway row at all."),
        ("STUCK_PENDING", f"PENDING for more than {config.PENDING_STUCK_HOURS} hours with no gateway outcome."),
        ("Unresolved", "resolution_status in OPEN, NO_REFUND_RAISED, REFUND_IN_PROGRESS."),
        ("Material break", "Severity HIGH or MEDIUM (ROUNDING_DIFF is LOW and excluded from 'material')."),
        ("Overdue pending refund", "PENDING and the as-of time is past refund_sla_due_ts."),
        ("Late processed refund", "PROCESSED after refund_sla_due_ts."),
        ("As-of time", "Latest payment timestamp in the data; ageing is measured against it."),
        ("Reduction assumptions", "TECHNICAL 60%, ISSUER_BANK 20%, CUSTOMER_ACTION 15%, RISK 0% - illustrative, not measured."),
        ("Data", "Fully simulated Amazon-style marketplace data. Not Amazon data; no real customers."),
    ], columns=["term", "definition"])
    for j, c in enumerate(defs.columns):
        ws.write(2, j, c, f["head"])
    for i, rec in enumerate(defs.itertuples(index=False)):
        ws.write(3 + i, 0, rec[0], f["bold"]); ws.write(3 + i, 1, rec[1], f["wrap"])
    ws.set_column(0, 0, 28); ws.set_column(1, 1, 110)

    for w in wb.worksheets():
        w.set_landscape(); w.fit_to_pages(1, 0); w.set_paper(9)
    wb.worksheets()[0].set_first_sheet(); wb.worksheets()[0].activate()
    wb.close()
    return out_path


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--target", choices=["sqlite", "mysql"], default="sqlite")
    ap.add_argument("--out", default=str(config.OUTPUT))
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    out = Path(a.out)
    conn = db.connect(a.target, path=out / "paymentops.db")
    try:
        d = load_all(conn)
        as_of = str(db.fetch_df(conn, "SELECT as_of_ts FROM v_as_of").iloc[0, 0])
    finally:
        conn.close()
    dq = pd.read_csv(out / "data_quality_report.csv") if (out / "data_quality_report.csv").exists() else pd.DataFrame()
    path = build(d, dq, as_of, out / "PaymentOps_Management_Report.xlsx")
    log.info("wrote %s", path)


if __name__ == "__main__":
    main()
