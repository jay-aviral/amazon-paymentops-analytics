# Interview guide - Amazon PaymentOps Analytics & Automation

Use this to talk about the project honestly and sharply. Read it with the repo open.

## 0. Ground rules

* Say **"independent portfolio project on simulated data"** in the first sentence. Never imply it is Amazon data or work experience.
* You can defend every number: each one traces to a named SQL query in `sql/` and a test in `tests/`.
* Amazon postings for Business Analyst roles (including payments teams) consistently stress: SQL, advanced Excel, dashboards (QuickSight / Tableau / Power BI),
  defining metrics, gathering requirements, and making recommendations that influence stakeholders. Check the exact posting you are applying to,
  its experience and degree requirements, and map this project to *its* bullets (table below is a template).

## 1. The 60-second pitch

> "I built an end-to-end payment-operations analytics platform on one million simulated e-commerce payment attempts.
> A Python generator creates deliberately messy data; an ETL pipeline validates, deduplicates and loads it into MySQL with hard integrity gates;
> SQL views implement reconciliation between our internal ledger and the gateway; and the outputs are a management Excel report and a Power BI model.
> It answers seven operational questions: success rate, which methods fail, whether amounts reconcile, which refunds are late, customer impact,
> the value of unresolved items, and what to change. For example, 98.4 % of payments reconcile, but INR 1.2 crore of material breaks is unresolved
> and about 63 % of it is older than 90 days, so I recommended an automated gateway-status job and a refund escalation queue."

## 2. Job-description mapping (template)

| Typical responsibility | Evidence in this project | Where |
|---|---|---|
| Write complex SQL to analyse large datasets | CTEs, window functions (`ROW_NUMBER`, `NTILE`, running totals), multi-join views on 1 M rows | `sql/02`-`06` |
| Own metrics, reports, dashboards | Single metric definitions, Excel report, Power BI model with DAX | `sql/03`, `src/build_report.py`, `powerbi/` |
| Reconcile / audit financial data | Internal vs gateway reconciliation with 7 break types, tolerance, severity, resolution | `sql/02_views.sql`, `sql/05` |
| Data quality and root cause | ETL reject rules with counts, 12 integrity checks, incident detectors | `src/etl_pipeline.py`, `sql/07`, `sql/04` |
| Automate manual processes | One-command pipeline, auto work-lists, recommended auto-refund / status-poll jobs | `src/run_pipeline.py`, *Work Lists* sheet |
| Advanced Excel | Native charts, live formulas, editable assumptions, autofilters | *Recommendations* sheet |
| Recommend and influence | Prioritised action plan sized in INR with explicit assumptions | *Recommendations* sheet |
| Ambiguity, requirements | Seven business questions turned into definitions (`Definitions` sheet) | `docs/`, README |

## 3. Questions you will get, and how to answer

**"How do you define payment success rate?"**
Successful attempts over all attempts; pending stays in the denominator. I also track *first-attempt order success* (94.03 %) and *eventual order success* (97.26 %)
because the customer experience metric is orders, not attempts. Retries succeed at about the same rate as first attempts, so I would not expect them to rescue failures without routing changes.

**"Walk me through reconciliation."**
Per payment I compare our status and amount with the gateway's captured rows (rolled up per payment because a duplicate capture is two rows). Seven break types, e.g.
*captured but not confirmed* (customer charged, order unpaid, HIGH) and *success with no gateway record* (MEDIUM). Exposure = money at risk; resolution joins refunds that fix the break.
Rules live in one view so Excel, Power BI and ad-hoc SQL cannot disagree. I verified the view against an independent pandas implementation.

**"Why a LEFT JOIN there?"** Payments with no gateway row are themselves a finding (risk-rejected, stuck, or missing record). An inner join would silently drop them.

**"How did you handle bad data?"**
Injected defects mirror real extracts: exact duplicates, same key with different values, orphans, negative/zero amounts, future timestamps, payment-before-order, case and format drift.
Each rule is counted, rejects are saved with the rule name, and the load fails if row counts, control totals or integrity checks break. The generator keeps the clean truth so I can prove the ETL recovers it (test).
I also found and fixed a defect in my own generator (a "refund exceeds payment" noise row that sometimes stayed valid) because the truth comparison caught a one-row difference.

**"What is the biggest finding?"**
Customer harm first: 2,521 payments where money was captured but the order shows failed or duplicated; INR 7.6 L is still not refunded. Then volume: INR 1.21 Cr of material breaks open, 76.7 L older than 90 days.
Then process: 701 pending refunds are 90+ days past SLA.

**"How did you size the improvements? Is the number real?"**
It is a scenario, not a measurement. Lost order value by first failure category × an assumed reduction % (TECHNICAL 60, ISSUER_BANK 20, CUSTOMER_ACTION 15, RISK 0).
The assumptions are yellow input cells with live formulas, so a stakeholder can challenge them. I'd validate with an A/B test of gateway failover before committing.

**"Limitations?"**
Simulated data: the patterns exist because I put them there; real data would add seasonality, fraud, partial captures and FX. The value shown is method: definitions, detection, sizing, recommendation, tests.

**"What would you do next with real data?"**
Add settlement-file reconciliation (T+1/T+2), chargebacks, partial refunds, cohort retention after a failed payment, and alerting (the incident queries are written to run every 15 minutes).

## 4. SQL you should be able to rewrite on a whiteboard

1. **Failure rate by method** - `SUM(CASE WHEN status='FAILED' ...) / COUNT(*)`, group by method.
2. **First failure per order** - `ROW_NUMBER() OVER (PARTITION BY order_id ORDER BY attempt_no)` then filter `rn = 1` (`cust_recovery_by_first_failure`).
3. **Duplicate captures** - group gateway rows by `payment_id` having `COUNT(*) > 1` where status CAPTURED.
4. **Pareto** - `SUM(n) OVER (ORDER BY n DESC ROWS UNBOUNDED PRECEDING) / total` (`fail_reason_pareto`).
5. **Incident detector** - hourly failure rate vs baseline rate, flag at 3x (`fail_incident_hours`).
6. **Ageing buckets** - CASE or join to a bucket table on `DATEDIFF`-style day arithmetic (`recon_ageing`).
7. **Best-vs-rest gap** - `MIN(rate) OVER (PARTITION BY method)` to size routing gains (`proc_gateway_routing_opportunity`).

## 5. Leadership Principles -> stories from this project

| Principle | Story |
|---|---|
| Customer Obsession | Prioritised "customer charged but unpaid" above larger-value internal breaks because it is direct customer harm. |
| Dive Deep | Compared ETL output to ground truth, found a one-row refund discrepancy, traced it to a bug in the noise generator and fixed it. Found PayGate-B is structurally worse even outside the outage. |
| Ownership | Built the full chain (data, ETL, SQL, report, dashboard spec, tests) and documented limitations instead of hiding them. |
| Invent and Simplify | One-command pipeline; reconciliation rules in one view; incident detectors that need no model. |
| Deliver Results | INR-denominated, prioritised plan with effort and owner area, not a list of charts. |
| Are Right, A Lot | Independent pandas re-implementation of recon rules; cross-query tie-out tests. |
| Bias for Action | Auto-generated work lists the ops team can start on immediately. |

## 6. Pitfalls

* Do not call the numbers "Amazon's". Do not claim production experience.
* Do not say "MySQL-tested" unless you have run `--target mysql` on your own MySQL 8 (see README).
* Know that `.pbix` is a build-yourself item; have it built and screenshotted before the interview.
* Be ready to explain every assumption (tolerance INR 1, stuck > 24 h, reduction %).
