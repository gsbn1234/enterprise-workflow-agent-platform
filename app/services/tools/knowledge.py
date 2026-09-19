from __future__ import annotations

import re

try:
    import httpx
except ModuleNotFoundError:  # pragma: no cover - optional integration dependency
    httpx = None

from app.config import settings
from app.db import get_connection, row_to_dict, rows_to_dicts
from app.services.audit import record_audit
from app.utils import compact_text, new_id, utc_now


def create_article(title: str, category: str, content: str, tags: str = "") -> dict:
    """Create one knowledge article.

    There is deliberately no ``visibility`` argument here, and no such field on
    the request schema either. **This version does not implement
    visibility-based access control.** ``knowledge_articles.visibility`` is not
    read by :func:`get_article`, :func:`list_articles` or
    :func:`search_knowledge` -- all three are unconditional ``SELECT *`` -- so a
    parameter named ``visibility`` on the writer advertised a control that did
    not exist. Anything marked ``restricted`` was still returned to everyone.

    The column itself stays for now: dropping it is a migration this version
    does not need, and the conservative change is to stop pretending rather than
    to move data. Every row written here is ``'internal'``, which is what the
    old default produced anyway, so nothing already stored or retrieved changes.
    Do not read access-control meaning into the value.
    """
    article_id = new_id("kb")
    now = utc_now()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO knowledge_articles
            (id, title, category, content, tags, visibility, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'internal', ?, ?)
            """,
            (article_id, title, category, content, tags, now, now),
        )
    record_audit("knowledge.create", "knowledge_article", article_id, {"title": title, "category": category})
    return get_article(article_id)


def get_article(article_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM knowledge_articles WHERE id = ?", (article_id,)).fetchone()
    return row_to_dict(row)


def list_articles(limit: int = 100) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM knowledge_articles
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (max(1, min(limit, 500)),),
        ).fetchall()
    return rows_to_dicts(rows)


def query_enterprise_rag(
    question: str,
    top_k: int = 3,
    *,
    user_id: str | None = None,
    user_department: str | None = None,
    user_role: str | None = None,
) -> dict:
    if not settings.rag_base_url:
        return {
            "source": "enterprise_rag",
            "available": False,
            "reason": "KNOWLEDGE_RAG_BASE_URL is not configured.",
            "query": question,
            "results": [],
            "citations": [],
            "retrieved_chunks": [],
            "can_answer": False,
        }
    if httpx is None:
        return {
            "source": "enterprise_rag",
            "available": False,
            "reason": "httpx is not installed.",
            "query": question,
            "results": [],
            "citations": [],
            "retrieved_chunks": [],
            "can_answer": False,
        }
    payload = {
        "question": question,
        "top_k": max(1, min(top_k, 20)),
        "user_id": user_id,
        "user_department": user_department,
        "user_role": user_role,
    }
    try:
        headers = {}
        if settings.rag_service_token:
            headers["X-RAG-Service-Token"] = settings.rag_service_token
        response = httpx.post(
            f"{settings.rag_base_url}/api/chat/ask",
            json=payload,
            headers=headers,
            timeout=settings.rag_timeout_seconds,
        )
        response.raise_for_status()
        rag_payload = response.json()
    except Exception as exc:
        return {
            "source": "enterprise_rag",
            "available": False,
            "reason": str(exc),
            "query": question,
            "results": [],
            "citations": [],
            "retrieved_chunks": [],
            "can_answer": False,
        }

    citations = rag_payload.get("citations", [])
    retrieved_chunks = rag_payload.get("retrieved_chunks", [])
    answer = rag_payload.get("answer", "")
    results = [
        {
            "article_id": item.get("chunk_id") or item.get("document_id"),
            "title": item.get("document_name", "RAG citation"),
            "category": "enterprise_rag",
            "snippet": compact_text(_chunk_content(item, answer), 240),
            "score": item.get("score", 0),
            "source": "enterprise_rag",
            "document_id": item.get("document_id"),
            "chunk_id": item.get("chunk_id"),
            "page_number": item.get("page_number"),
            "section_title": item.get("section_title"),
        }
        for item in (retrieved_chunks or citations)[: max(1, min(top_k, 20))]
    ]
    return {
        "source": "enterprise_rag",
        "available": True,
        "query": question,
        "answer": answer,
        "can_answer": bool(rag_payload.get("can_answer", bool(answer))),
        "refusal_reason": rag_payload.get("refusal_reason"),
        "log_id": rag_payload.get("log_id"),
        "citations": citations,
        "retrieved_chunks": [
            {
                **item,
                "content": compact_text(item.get("content", ""), 320),
            }
            for item in retrieved_chunks[: max(1, min(top_k, 20))]
        ],
        "results": results,
        "metrics": {
            "citation_count": len(citations),
            "retrieved_chunk_count": len(retrieved_chunks),
            "result_count": len(results),
        },
        "eval_reports_url": f"{settings.rag_base_url}/api/eval-reports",
    }


def search_knowledge(query: str, limit: int = 3) -> dict:
    terms = _extract_terms(query)
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM knowledge_articles").fetchall()

    scored = []
    lowered_query = query.lower()
    for row in rows:
        article = row_to_dict(row)
        haystack = " ".join([article["title"], article["category"], article["content"], article["tags"]]).lower()
        score = 0
        for term in terms:
            if term in haystack:
                score += 2 if term in article["title"].lower() or term in article["tags"].lower() else 1
        if article["category"].lower() in lowered_query:
            score += 2
        if score > 0:
            scored.append((score, article))

    scored.sort(key=lambda item: item[0], reverse=True)
    results = [
        {
            "article_id": article["id"],
            "title": article["title"],
            "category": article["category"],
            "snippet": compact_text(article["content"], 220),
            "score": score,
            "source": "local_policy_db",
        }
        for score, article in scored[: max(1, min(limit, 10))]
    ]
    return {"query": query, "results": results, "source": "local_policy_db", "available": True}


def _chunk_content(item: dict, fallback: str) -> str:
    content = item.get("content") or item.get("snippet")
    if content:
        return str(content)
    return fallback


def _extract_terms(query: str) -> set[str]:
    lowered = query.lower()
    terms = set(re.findall(r"[a-zA-Z0-9_\-]{2,}", lowered))
    business_terms = [
        "退款",
        "审批",
        "客户",
        "工单",
        "故障",
        "采购",
        "邮件",
        "安全",
        "权限",
        "合同",
        "赔偿",
        "外部",
        "隐私",
        # IT operations vocabulary. The IT corpus and the incidents filed
        # against it are written in Chinese, while the seeded article tags are
        # ASCII — without these, a report like "缓存压力很大，需要清理" extracts
        # no term at all and retrieves nothing. Kept to words absent from the
        # business corpus so a business query cannot pull in an IT article.
        "缓存",
        "清理",
        "重启",
        "服务器",
        "数据库",
    ]
    for term in business_terms:
        if term in query:
            terms.add(term)
    return terms
