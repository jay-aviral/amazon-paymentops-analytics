# Power BI dashboard specification

A `.pbix` file can only be authored in Power BI Desktop (Windows), so this repo ships everything needed to
build it in about an hour: the data model, every DAX measure, page-by-page visual specs and a theme.
Every figure on the dashboard has a matching SQL query in `sql/03-06` and a sheet in the Excel report, so the
three can be cross-checked.

## 1. Connect to data (pick one)

| Option | Steps |
|---|---|
| **A. MySQL (production-like)** | Get data → MySQL database → server/db from your `.env` → Import → tables `v_payment_facts`, `v_reconciliation`, `v_refund_pipeline`, `v_order_payment_summary`, `payment_methods`. Aggregate in Power Query or use the queries below. |
| **B. CSV (no database)** | Run `python -m src.run_pipeline`; then Get data → Folder → `output/powerbi_data/`. Seven small CSVs, 25 MB total, no 1M-row table needed. |

Power Query steps for option B: promote headers; set `payment_date`, `order_date`, `date` to **Date**;
`refund_requested_ts`, `refund_sla_due_ts`, `refund_processed_ts`, `payment_ts` to **Date/Time**; add
`RequestedDate = DateTime.Date([refund_requested_ts])` to `fact_refunds`.

## 2. Data model (star schema)

```
                dim_date ──────────────┬── fact_payments_daily  (payment_date)
                                       ├── fact_reconciliation  (payment_date)
                                       ├── fact_refunds         (RequestedDate)
                                       └── fact_orders_daily    (order_date)
dim_payment_method[method_name] ─┬── fact_payments_daily / fact_reconciliation / fact_refunds   (1 → *)
dim_gateway[gateway_name] ───────┴── fact_payments_daily / fact_reconciliation / fact_refunds   (1 → *)
```

All relationships: one-to-many, single direction, dimension → fact. Mark `dim_date` as the date table.
Hide key columns on facts; sort `dim_date[month_name]` by `month`.

| Table | Grain | Rows |
|---|---|---|
| fact_payments_daily | date × method × gateway × device × prime × status × failure code | ~76 K |
| fact_reconciliation | one row per payment that is not a clean MATCH | ~15.6 K |
| fact_refunds | one row per refund | ~91.8 K |
| fact_orders_daily | order date × category × prime | ~6 K |

## 3. DAX measures

Create a table `_Measures` (Enter data, blank) and add:

```DAX
-- Payments (Q1, Q2)
Attempts          = SUM ( fact_payments_daily[attempts] )
Success Attempts  = CALCULATE ( [Attempts], fact_payments_daily[status] = "SUCCESS" )
Failed Attempts   = CALCULATE ( [Attempts], fact_payments_daily[status] = "FAILED" )
Success Rate %    = DIVIDE ( [Success Attempts], [Attempts] )
Failure Rate %    = DIVIDE ( [Failed Attempts], [Attempts] )
Failed Value      = CALCULATE ( SUM ( fact_payments_daily[amount] ), fact_payments_daily[status] = "FAILED" )
Failure Rate vs Avg (pp) =
    ( [Failure Rate %] - CALCULATE ( [Failure Rate %], ALLSELECTED ( dim_payment_method ), ALLSELECTED ( dim_gateway ) ) ) * 100
Failure Rate MoM (pp) =
    VAR prev = CALCULATE ( [Failure Rate %], DATEADD ( dim_date[date], -1, MONTH ) )
    RETURN IF ( NOT ISBLANK ( prev ), ( [Failure Rate %] - prev ) * 100 )

-- Reconciliation (Q3, Q6)
Unresolved Statuses = { "OPEN", "NO_REFUND_RAISED", "REFUND_IN_PROGRESS" }
Match Rate %      = 1 - DIVIDE ( COUNTROWS ( fact_reconciliation ), [Attempts] )
Material Breaks   = CALCULATE ( COUNTROWS ( fact_reconciliation ), fact_reconciliation[severity] IN { "HIGH", "MEDIUM" } )
Unresolved Value  =
    CALCULATE ( SUM ( fact_reconciliation[exposure_amount] ),
                fact_reconciliation[resolution_status] IN { "OPEN", "NO_REFUND_RAISED", "REFUND_IN_PROGRESS" },
                fact_reconciliation[severity] IN { "HIGH", "MEDIUM" } )
Resolved Value    = CALCULATE ( SUM ( fact_reconciliation[exposure_amount] ), fact_reconciliation[resolution_status] = "RESOLVED_REFUNDED" )
Breaks per 10K    = DIVIDE ( [Material Breaks], [Attempts] ) * 10000
Unresolved > 90d  =
    CALCULATE ( [Unresolved Value], fact_reconciliation[age_days] > 90 )

-- Refunds (Q4)
Refunds           = COUNTROWS ( fact_refunds )
Pending Refunds   = CALCULATE ( [Refunds], fact_refunds[refund_status] = "PENDING" )
Overdue Pending   = SUM ( fact_refunds[is_overdue_pending] )
Late Processed    = SUM ( fact_refunds[is_late_processed] )
Overdue Value     = CALCULATE ( SUM ( fact_refunds[refund_amount] ), fact_refunds[is_overdue_pending] = 1 )
Refund SLA Breach % = DIVIDE ( [Overdue Pending] + [Late Processed], [Refunds] )
Avg Days to Process = CALCULATE ( AVERAGE ( fact_refunds[elapsed_days] ), fact_refunds[refund_status] = "PROCESSED" )

-- Customer impact (Q5)
Orders            = SUM ( fact_orders_daily[orders] )
Orders Hit %      = DIVIDE ( SUM ( fact_orders_daily[orders_hit] ), [Orders] )
Abandon Rate After Failure = DIVIDE ( SUM ( fact_orders_daily[abandoned_orders] ), SUM ( fact_orders_daily[orders_hit] ) )
Lost Order Value  = SUM ( fact_orders_daily[lost_order_value] )

-- What-if (Q7): create parameter "Failure reduction %" 0-50 step 5 (generates [Failure reduction % Value])
Recovered Value (what-if) = [Lost Order Value] * [Failure reduction % Value]
```

**Reconciliation check:** with no filters the card values must equal the SQL: `Success Rate %` 94.05 %,
`Match Rate %` 98.44 %, `Unresolved Value` INR 12.09 M, `Overdue Pending` 1,240, `Orders Hit %` 5.64 %.
(Figures are for the seed-42 / 1 M run. The test-suite and `output/` regenerate them deterministically.)

## 4. Pages

| # | Page | Business question | Visuals |
|---|---|---|---|
| 1 | **Executive overview** | all | 6 KPI cards (Success Rate %, Match Rate %, Unresolved Value, Overdue Pending, Orders Hit %, Lost Order Value); line chart Success Rate % by `dim_date[month_name]`; bar chart of Unresolved Value by `recon_status`; slicers: date range, gateway. Text box with the 3 headline findings. |
| 2 | **Payment performance** | Q1 | Daily success-rate line with sale-day dips (reference line at average); column chart of Failure Rate % by hour (needs hour: use `kpi_hourly_profile` query or add to model); Success Rate by month table with conditional-format data bars. |
| 3 | **Failure analysis** | Q2 | Bar: Failure Rate % by method (sorted, colour by `Failure Rate vs Avg (pp)`); matrix method × gateway with heat-map formatting; Pareto (column = Failed Attempts by `failure_code`, line = cumulative % via quick measure *running total*); stacked bar: failures by `failure_category` per method; decomposition tree on Failed Attempts. Slicers: device, prime. |
| 4 | **Reconciliation** | Q3, Q6 | Cards: Match Rate %, Material Breaks, Unresolved Value, Unresolved > 90d; column: Unresolved Value by `age_days` bucket (create a bucket column: 0-7, 8-30, 31-90, 91-180, 180+); table: top breaks (payment_id, gateway, recon_status, exposure_amount) with Top N filter = 100; bar: Breaks per 10K by gateway. |
| 5 | **Refund operations** | Q4 | Cards: Pending Refunds, Overdue Pending, Overdue Value, Refund SLA Breach %; column: SLA breach % by `refund_reason`; clustered bar: SLA breach % by `method_name`; table (drill-through) of overdue refunds sorted by `refund_amount`. |
| 6 | **Customer impact** | Q5 | Funnel: Orders → Orders hit → Recovered → Abandoned; Orders Hit % and Abandon Rate by month; column: Lost Order Value by `category`; Prime vs non-Prime comparison. |
| 7 | **Improvement scenarios** | Q7 | What-if slider "Failure reduction %"; card Recovered Value (what-if); table from `proc_gateway_routing_opportunity` (import as a query) with data bars on `avoidable_failures`; text box with the action plan from the Excel *Recommendations* sheet. |

## 5. Design rules

* Theme: **View → Themes → Browse for themes → `powerbi/theme.json`** (navy / orange, colour-blind-safe order).
* One question per page, KPI cards across the top, detail below. Same slicer set (date, gateway, method) synced on pages 2-6.
* Format rates as `0.00%`, money as INR in lakh/crore using format string `[>=10000000]#,##0.00,,"Cr";[>=100000]#,##0.00,"L";#,##0`.
* Use **bookmarks + a page navigator** for the Executive-overview buttons; add tooltips pages for the method bar and the Pareto.
* Alerts (Power BI Service): data alert on card `Unresolved > 90d` and `Overdue Pending`.
