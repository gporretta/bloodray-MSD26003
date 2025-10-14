# app/db.py
import os
import json
import sqlite3
from contextlib import closing

# Store DB in app/data/
BASE_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "tooltest.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS test_runs (
    id TEXT PRIMARY KEY,              -- timestamp e.g. '2025-10-14 15:22:33.123'
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,             -- PASSED / FAILED / ABORTED_SEAL_WARNING / ERROR
    metrics_json TEXT NOT NULL,       -- full metrics dict (JSON)

    baseline_p95 REAL,
    guard_band REAL,
    effective_threshold REAL,
    max_brightness REAL,

    frames_total INTEGER,
    frames_baseline INTEGER,
    frames_analysis INTEGER,
    read_errors INTEGER,

    total_time REAL,
    baseline_time REAL,
    analysis_time REAL,
    rotation_time_accum REAL
);
"""

def _connect():
    os.makedirs(DATA_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10.0, isolation_level=None)  # autocommit
    with closing(con.cursor()) as cur:
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute("PRAGMA synchronous=NORMAL;")
    return con

def init_db():
    con = _connect()
    try:
        with closing(con.cursor()) as cur:
            cur.executescript(SCHEMA)
    finally:
        con.close()

def _dur(s, e):
    if s is None or e is None:
        return None
    return max(0.0, e - s)

def save_run(timestamp_id, status, metrics, dynamic_threshold, guard, eff_thr):
    """
    Persist a single run. `metrics` is a dict (JSON-encoded for metrics_json).
    """
    total_time    = _dur(metrics.get("total_start"),    metrics.get("total_end"))
    baseline_time = _dur(metrics.get("baseline_start"), metrics.get("baseline_end"))
    analysis_time = _dur(metrics.get("analysis_start"), metrics.get("analysis_end"))

    payload_json = json.dumps(metrics, ensure_ascii=False)

    rows = (
        timestamp_id,                    # id
        timestamp_id,                    # created_at
        status,                          # status
        payload_json,                    # metrics_json
        float(dynamic_threshold) if dynamic_threshold is not None else None,
        float(guard) if guard is not None else None,
        float(eff_thr) if eff_thr is not None else None,
        float(metrics.get("max_brightness", 0.0)) if metrics.get("max_brightness") is not None else None,
        int(metrics.get("frames_total", 0)) if metrics.get("frames_total") is not None else None,
        int(metrics.get("frames_baseline", 0)) if metrics.get("frames_baseline") is not None else None,
        int(metrics.get("frames_analysis", 0)) if metrics.get("frames_analysis") is not None else None,
        int(metrics.get("read_errors", 0)) if metrics.get("read_errors") is not None else None,
        float(total_time) if total_time is not None else None,
        float(baseline_time) if baseline_time is not None else None,
        float(analysis_time) if analysis_time is not None else None,
        float(metrics.get("rotation_time_accum", 0.0)) if metrics.get("rotation_time_accum") is not None else None,
    )

    con = _connect()
    try:
        with closing(con.cursor()) as cur:
            cur.execute(
                """
                INSERT OR REPLACE INTO test_runs
                (id, created_at, status, metrics_json,
                 baseline_p95, guard_band, effective_threshold, max_brightness,
                 frames_total, frames_baseline, frames_analysis, read_errors,
                 total_time, baseline_time, analysis_time, rotation_time_accum)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows
            )
    finally:
        con.close()

