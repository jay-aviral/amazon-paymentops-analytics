"""One command, whole platform:  generate -> ETL/load -> report -> Power BI export.

    python -m src.run_pipeline                       # 1,000,000 transactions, SQLite
    python -m src.run_pipeline --target mysql        # load into MySQL 8 (see README)
    python -m src.run_pipeline --transactions 50000  # quick smoke run
"""
from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

import pandas as pd

from . import build_report, config, db, etl_pipeline, export_powerbi, generate_data

log = logging.getLogger("pipeline")


def run(transactions=config.N_TRANSACTIONS, customers=config.N_CUSTOMERS, seed=config.SEED,
        target="sqlite", root: Path = config.ROOT, skip_generate=False) -> dict:
    root = Path(root)
    raw, clean, truth, out = root / "data" / "raw", root / "data" / "clean", root / "data" / "truth", root / "output"
    timings, t0 = {}, time.time()

    def stage(name, fn):
        t = time.time()
        res = fn()
        timings[name] = round(time.time() - t, 1)
        log.info("== %s finished in %.1fs", name, timings[name])
        return res

    if not skip_generate:
        stage("generate", lambda: generate_data.generate(transactions, customers, seed, out=raw, truth_dir=truth))
    summary = stage("etl+load", lambda: etl_pipeline.run_etl(raw, clean, out, target))

    def report():
        conn = db.connect(target, path=out / "paymentops.db")
        try:
            d = build_report.load_all(conn)
            as_of = str(db.fetch_df(conn, "SELECT as_of_ts FROM v_as_of").iloc[0, 0])
            pbi = export_powerbi.export(conn, out / "powerbi_data")
        finally:
            conn.close()
        dq = pd.read_csv(out / "data_quality_report.csv")
        build_report.build(d, dq, as_of, out / "PaymentOps_Management_Report.xlsx")
        return d, pbi

    d, pbi = stage("report+powerbi", report)
    timings["total"] = round(time.time() - t0, 1)
    k = d["kpi_headline"].iloc[0]
    log.info("DONE in %.1fs | success rate %.2f%% | recon unresolved %.0f | outputs in %s",
             timings["total"], k.success_rate_pct, d["recon_headline"].iloc[0].unresolved_value_material, out)
    return dict(timings=timings, etl=summary, powerbi_rows=pbi)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--transactions", type=int, default=config.N_TRANSACTIONS)
    ap.add_argument("--customers", type=int, default=None, help="default scales with --transactions")
    ap.add_argument("--seed", type=int, default=config.SEED)
    ap.add_argument("--target", choices=["sqlite", "mysql"], default="sqlite")
    ap.add_argument("--skip-generate", action="store_true", help="reuse data/raw from a previous run")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    customers = a.customers or max(1000, int(a.transactions * config.N_CUSTOMERS / config.N_TRANSACTIONS))
    run(a.transactions, customers, a.seed, a.target, skip_generate=a.skip_generate)


if __name__ == "__main__":
    main()
