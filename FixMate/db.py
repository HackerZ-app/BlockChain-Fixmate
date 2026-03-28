import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "troubleshooter.db"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create tables and indexes if they do not already exist (idempotent)."""
    conn = get_conn()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS fix_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_problem TEXT,
                matched_problem TEXT,
                solution_command TEXT,
                solution_description TEXT,
                result TEXT,
                timestamp TEXT
            );

            CREATE TABLE IF NOT EXISTS command_executions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_problem TEXT,
                matched_problem TEXT,
                solution_description TEXT,
                command_executed TEXT,
                execution_success INTEGER,
                return_code INTEGER,
                output TEXT,
                error TEXT,
                simulation_mode INTEGER,
                session_id TEXT,
                timestamp TEXT
            );

            -- Speeds up get_solution_success_rate() which filters on this column.
            CREATE INDEX IF NOT EXISTS idx_fix_outcomes_solution_command
                ON fix_outcomes (solution_command);

            -- Speeds up ORDER BY timestamp DESC queries in get_recent_command_logs().
            CREATE INDEX IF NOT EXISTS idx_command_executions_timestamp
                ON command_executions (timestamp DESC);

            -- Speeds up get_top_problems() GROUP BY / COUNT.
            CREATE INDEX IF NOT EXISTS idx_command_executions_matched_problem
                ON command_executions (matched_problem);
            """
        )
        conn.commit()
    finally:
        conn.close()


def log_fix_outcome(
    user_problem: str,
    matched_problem: str,
    solution_command: str,
    solution_description: str,
    result: str,
) -> None:
    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO fix_outcomes
            (user_problem, matched_problem, solution_command, solution_description, result, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                user_problem,
                matched_problem,
                solution_command,
                solution_description,
                result,
                datetime.now().isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_solution_success_rate(solution_command: str) -> float:
    """Return the historical success rate for a given command (0.0–1.0).

    Returns the default rate of 0.85 when the command is empty, unknown, or
    has never been recorded in the database.
    """
    command = (solution_command or "").strip()
    if not command:
        return 0.85

    conn = get_conn()
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN result = 'fixed' THEN 1 ELSE 0 END) AS successes
            FROM fix_outcomes
            WHERE solution_command = ?
            """,
            (command,),
        ).fetchone()
    finally:
        conn.close()

    if not row or (row["total"] or 0) == 0:
        return 0.85
    return round((row["successes"] or 0) / row["total"], 2)


def log_command_execution(
    user_problem: str,
    matched_problem: str,
    description: str,
    command: str,
    result: dict,
    session_id: str,
) -> None:
    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO command_executions
            (user_problem, matched_problem, solution_description, command_executed, execution_success,
             return_code, output, error, simulation_mode, session_id, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_problem,
                matched_problem,
                description,
                command,
                1 if result.get("success") else 0,
                int(result.get("return_code", -1)),
                (result.get("output") or "")[:4000],
                (result.get("error") or "")[:4000],
                1 if result.get("simulation_mode") else 0,
                session_id,
                datetime.now().isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_recent_command_logs(limit: int = 50) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT timestamp, user_problem, matched_problem, solution_description, command_executed,
                   execution_success, return_code, output, error, simulation_mode, session_id
            FROM command_executions
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def get_recent_24h_count() -> int:
    conn = get_conn()
    try:
        cutoff = (datetime.now().replace(microsecond=0) - __import__('datetime').timedelta(hours=24)).isoformat()
        row = conn.execute(
            "SELECT COUNT(*) FROM command_executions WHERE timestamp >= ?", (cutoff,)
        ).fetchone()
        return int(row[0] or 0)
    except Exception:
        return 0
    finally:
        conn.close()


def get_stats() -> dict:
    conn = get_conn()
    try:
        total = conn.execute("SELECT COUNT(*) FROM command_executions").fetchone()[0]
        success = conn.execute(
            "SELECT COUNT(*) FROM command_executions WHERE execution_success = 1"
        ).fetchone()[0]
    finally:
        conn.close()
    return {
        "total_executions": int(total or 0),
        "successful_executions": int(success or 0),
        "success_rate": round(((success / total) * 100) if total else 0, 1),
    }


def get_top_problems(limit: int = 5) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT matched_problem, COUNT(*) AS count
            FROM command_executions
            WHERE matched_problem IS NOT NULL AND matched_problem != ''
            GROUP BY matched_problem
            ORDER BY count DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def get_recent_feedback_logs(limit: int = 50) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT timestamp, user_problem, matched_problem, solution_command, solution_description, result
            FROM fix_outcomes
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]
