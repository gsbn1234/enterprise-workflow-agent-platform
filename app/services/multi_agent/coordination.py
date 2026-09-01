from __future__ import annotations

from app.db import get_connection, rows_to_dicts
from app.utils import json_dumps, json_loads, new_id, utc_now_precise


def register_task_graph(run_id: str, task_graph: list[dict], *, attempt: int = 0) -> None:
    for task in task_graph:
        ensure_task(
            run_id,
            str(task["task_key"]),
            str(task["agent"]),
            attempt=attempt,
            description=str(task.get("description") or ""),
            dependencies=list(task.get("depends_on") or []),
        )


def ensure_task(
    run_id: str,
    task_key: str,
    assigned_agent: str,
    *,
    attempt: int = 0,
    description: str = "",
    dependencies: list[str] | None = None,
) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO multi_agent_tasks
            (id, run_id, task_key, attempt, assigned_agent, description,
             dependencies_json, status, input_json, output_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', '{}', '{}', ?)
            ON CONFLICT (run_id, task_key, attempt) DO NOTHING
            """,
            (
                new_id("task"),
                run_id,
                task_key,
                attempt,
                assigned_agent,
                description,
                json_dumps(dependencies or []),
                utc_now_precise(),
            ),
        )


def start_task(
    run_id: str,
    task_key: str,
    assigned_agent: str,
    *,
    attempt: int = 0,
    task_input: dict | None = None,
) -> None:
    ensure_task(run_id, task_key, assigned_agent, attempt=attempt)
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE multi_agent_tasks
            SET status = 'running', input_json = ?, started_at = ?, completed_at = NULL
            WHERE run_id = ? AND task_key = ? AND attempt = ?
            """,
            (json_dumps(task_input or {}), utc_now_precise(), run_id, task_key, attempt),
        )


def finish_task(
    run_id: str,
    task_key: str,
    *,
    attempt: int = 0,
    output: dict | None = None,
    status: str = "completed",
    duration_ms: int = 0,
) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE multi_agent_tasks
            SET status = ?, output_json = ?, duration_ms = ?, completed_at = ?
            WHERE run_id = ? AND task_key = ? AND attempt = ?
            """,
            (
                status,
                json_dumps(output or {}),
                max(0, int(duration_ms)),
                utc_now_precise(),
                run_id,
                task_key,
                attempt,
            ),
        )


def record_handoff(
    run_id: str,
    from_agent: str,
    to_agent: str,
    task_key: str,
    payload: dict,
) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO multi_agent_handoffs
            (id, run_id, from_agent, to_agent, task_key, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("handoff"),
                run_id,
                from_agent,
                to_agent,
                task_key,
                json_dumps(payload),
                utc_now_precise(),
            ),
        )


def list_tasks(run_id: str) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM multi_agent_tasks
            WHERE run_id = ?
            ORDER BY attempt ASC, created_at ASC, id ASC
            """,
            (run_id,),
        ).fetchall()
    tasks = rows_to_dicts(rows)
    for task in tasks:
        task["dependencies"] = json_loads(task.pop("dependencies_json"), [])
        task["input"] = json_loads(task.pop("input_json"), {})
        task["output"] = json_loads(task.pop("output_json"), {})
    return tasks


def list_handoffs(run_id: str) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM multi_agent_handoffs
            WHERE run_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (run_id,),
        ).fetchall()
    handoffs = rows_to_dicts(rows)
    for handoff in handoffs:
        handoff["payload"] = json_loads(handoff.pop("payload_json"), {})
    return handoffs
