from __future__ import annotations

from app.db import get_connection, row_to_dict, rows_to_dicts
from app.utils import json_dumps, json_loads, new_id, utc_now


def add_memory(
    memory_type: str,
    memory_key: str,
    summary: str,
    detail: dict,
    *,
    source_run_id: str | None = None,
    score: float = 0,
) -> dict:
    memory_id = new_id("mem")
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO agent_memory
            (id, memory_type, memory_key, summary, detail_json, source_run_id, score, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (memory_id, memory_type, memory_key, summary, json_dumps(detail), source_run_id, score, utc_now()),
        )
    return get_memory(memory_id)


def get_memory(memory_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM agent_memory WHERE id = ?", (memory_id,)).fetchone()
    memory = row_to_dict(row)
    if memory:
        memory["detail"] = json_loads(memory.pop("detail_json"), {})
    return memory


def list_memories(limit: int = 100, memory_type: str | None = None) -> list[dict]:
    with get_connection() as conn:
        if memory_type:
            rows = conn.execute(
                """
                SELECT * FROM agent_memory
                WHERE memory_type = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (memory_type, max(1, min(limit, 500))),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM agent_memory
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (max(1, min(limit, 500)),),
            ).fetchall()
    memories = rows_to_dicts(rows)
    for memory in memories:
        memory["detail"] = json_loads(memory.pop("detail_json"), {})
    return memories


def search_similar_memories(query: str, limit: int = 5) -> list[dict]:
    terms = {part.lower() for part in query.split() if len(part) >= 2}
    memories = list_memories(limit=200)
    scored = []
    for memory in memories:
        haystack = f"{memory['memory_key']} {memory['summary']} {memory.get('detail', {})}".lower()
        score = sum(1 for term in terms if term in haystack)
        if score:
            scored.append((score, memory))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [memory for _, memory in scored[: max(1, min(limit, 20))]]
