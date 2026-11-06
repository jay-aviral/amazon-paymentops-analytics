import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import db, generate_data, etl_pipeline  # noqa: E402

N_TX, N_CUST = 40_000, 6_000


@pytest.fixture(scope="session")
def pipeline(tmp_path_factory):
    """Generate -> ETL -> SQLite once for the whole test session (small, seeded)."""
    root = tmp_path_factory.mktemp("paymentops")
    raw, clean, truth, out = (root / "raw", root / "clean", root / "truth", root / "out")
    # dense 11-day window containing the seeded 15-Jul PayGate-B outage (hourly detectors need volume)
    generate_data.generate(N_TX, N_CUST, 7, start="2026-07-10", end="2026-07-20", out=raw, truth_dir=truth)
    summary = etl_pipeline.run_etl(raw, clean, out, "sqlite", True, True)
    return dict(root=root, raw=raw, clean=clean, truth=truth, out=out, summary=summary,
                db=out / "paymentops.db")


@pytest.fixture()
def conn(pipeline):
    c = db.connect_sqlite(pipeline["db"])
    yield c
    c.close()
