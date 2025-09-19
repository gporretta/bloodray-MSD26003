import sqlite3
import pandas as pd
from datetime import datetime
from pathlib import Path
from config import DB_FILE, EXPORT_XLSX

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_type TEXT NOT NULL,
    tester_name TEXT NOT NULL,
    result TEXT NOT NULL,
    timestamp TEXT NOT NULL
);
"""

def _ensure_db_dir():
    Path(DB_FILE).parent.mkdir(parents=True, exist_ok=True)

def init_db():
    _ensure_db_dir()
    conn = sqlite3.connect(DB_FILE)
    try:
        conn.execute(SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()

def save_test_result(tool_type, tester_name, result):
    conn = sqlite3.connect(DB_FILE)
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO results (tool_type, tester_name, result, timestamp) VALUES (?, ?, ?, ?)",
            (tool_type, tester_name, result, ts),
        )
        conn.commit()
    finally:
        conn.close()

def export_to_excel(filename=EXPORT_XLSX):
    _ensure_db_dir()
    conn = sqlite3.connect(DB_FILE)
    try:
        df = pd.read_sql_query("SELECT * FROM results", conn)
    finally:
        conn.close()
    Path(filename).parent.mkdir(parents=True, exist_ok=True)
    df.to_excel(filename, index=False)
    print(f"[DEBUG] Exported database to {filename}")

