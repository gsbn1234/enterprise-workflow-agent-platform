from __future__ import annotations

import hashlib
import smtplib
from email.message import EmailMessage
from email.utils import parseaddr

from app.config import settings
from app.db import get_connection, row_to_dict, rows_to_dicts
from app.services.audit import record_audit
from app.services.outbox import create_outbox_event, mark_outbox_completed, mark_outbox_failed, mark_outbox_running
from app.services.tenancy import effective_tenant_id
from app.utils import json_dumps, new_id, utc_now


def draft_email(to_address: str, subject: str, body: str) -> dict:
    return {
        "to_address": to_address,
        "subject": subject,
        "body": body,
        "status": "draft",
    }


def send_email(to_address: str, subject: str, body: str, approval_id: str | None = None, tenant_id: str | None = None) -> dict:
    provider = _email_provider()
    tenant = effective_tenant_id(tenant_id)
    idempotency_key = _email_idempotency_key(
        to_address=to_address,
        subject=subject,
        body=body,
        approval_id=approval_id,
        provider=provider,
        tenant_id=tenant,
    )
    existing = _find_email_by_idempotency_key(idempotency_key)
    if existing:
        record_audit(
            "email.idempotent_reuse",
            "email",
            existing["id"],
            {"idempotency_key": idempotency_key, "provider": provider, "to_address": to_address},
            tenant_id=tenant,
        )
        return existing

    outbox = create_outbox_event(
        "email.send",
        provider,
        {
            "to_address": to_address,
            "subject": subject,
            "body": body,
            "approval_id": approval_id,
            "tenant_id": tenant,
        },
        idempotency_key=idempotency_key,
        target_type="email",
    )
    mark_outbox_running(outbox["id"])

    if provider == "mock":
        email = _record_email(
            to_address,
            subject,
            body,
            status="sent",
            approval_id=approval_id,
            provider=provider,
            external_message_id=None,
            error_message=None,
            idempotency_key=idempotency_key,
            tenant_id=tenant,
        )
        mark_outbox_completed(
            outbox["id"],
            target_id=email["id"],
            response={"email_id": email["id"], "provider": provider, "status": email["status"]},
        )
        return email
    if provider != "smtp":
        error = f"Unsupported email provider: {provider}"
        mark_outbox_failed(outbox["id"], error)
        raise ValueError(error)

    message_id = _message_id_for_key(idempotency_key)
    try:
        _validate_smtp_settings()
        _ensure_allowed_recipient(to_address)
        message = EmailMessage()
        message["From"] = settings.smtp_from_email
        message["To"] = to_address
        message["Subject"] = subject
        message["Message-ID"] = message_id
        message.set_content(body)

        if settings.smtp_use_ssl:
            with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout_seconds) as smtp:
                _smtp_login_if_needed(smtp)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout_seconds) as smtp:
                smtp.ehlo()
                if settings.smtp_use_tls:
                    smtp.starttls()
                    smtp.ehlo()
                _smtp_login_if_needed(smtp)
                smtp.send_message(message)
    except Exception as exc:
        failed_email = _record_email(
            to_address,
            subject,
            body,
            status="failed",
            approval_id=approval_id,
            provider=provider,
            external_message_id=message_id,
            error_message=str(exc),
            idempotency_key=None,
            tenant_id=tenant,
        )
        mark_outbox_failed(outbox["id"], str(exc), target_id=failed_email["id"])
        raise

    email = _record_email(
        to_address,
        subject,
        body,
        status="sent",
        approval_id=approval_id,
        provider=provider,
        external_message_id=message_id,
        error_message=None,
        idempotency_key=idempotency_key,
        tenant_id=tenant,
    )
    mark_outbox_completed(
        outbox["id"],
        target_id=email["id"],
        response={
            "email_id": email["id"],
            "provider": provider,
            "status": email["status"],
            "external_message_id": message_id,
        },
    )
    return email


def _record_email(
    to_address: str,
    subject: str,
    body: str,
    *,
    status: str,
    approval_id: str | None,
    provider: str,
    external_message_id: str | None,
    error_message: str | None,
    idempotency_key: str | None,
    tenant_id: str | None,
) -> dict:
    email_id = new_id("email")
    now = utc_now()
    sent_at = now if status == "sent" else None
    tenant = effective_tenant_id(tenant_id)
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO emails
            (id, to_address, subject, body, tenant_id, status, approval_id, provider,
             external_message_id, error_message, idempotency_key, created_at, sent_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                email_id,
                to_address,
                subject,
                body,
                tenant,
                status,
                approval_id,
                provider,
                external_message_id,
                error_message,
                idempotency_key,
                now,
                sent_at,
            ),
        )
    event = "email.send" if status == "sent" else "email.send_failed"
    record_audit(
        event,
        "email",
        email_id,
        {
            "to_address": to_address,
            "approval_id": approval_id,
            "provider": provider,
            "external_message_id": external_message_id,
            "idempotency_key": idempotency_key,
            "error": error_message,
        },
        tenant_id=tenant,
    )
    return get_email(email_id)


def _find_email_by_idempotency_key(idempotency_key: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT * FROM emails
            WHERE idempotency_key = ? AND status = 'sent'
            """,
            (idempotency_key,),
        ).fetchone()
    return row_to_dict(row)


def _email_provider() -> str:
    if settings.email_provider:
        return settings.email_provider
    return "smtp" if settings.tool_mode == "real" else "mock"


def _validate_smtp_settings() -> None:
    if not settings.smtp_host:
        raise ValueError("SMTP_HOST is required when AGENT_EMAIL_PROVIDER=smtp.")
    if not settings.smtp_from_email:
        raise ValueError("SMTP_FROM_EMAIL or SMTP_USERNAME is required when AGENT_EMAIL_PROVIDER=smtp.")
    if not settings.email_allowlist:
        raise ValueError("AGENT_EMAIL_ALLOWLIST is required for real email sends. Use * only in a controlled test environment.")


def _smtp_login_if_needed(smtp: smtplib.SMTP) -> None:
    if settings.smtp_username or settings.smtp_password:
        smtp.login(settings.smtp_username, settings.smtp_password)


def _ensure_allowed_recipient(to_address: str) -> None:
    parsed = parseaddr(to_address)[1].lower()
    if not parsed or "@" not in parsed:
        raise ValueError(f"Invalid recipient email address: {to_address}")
    domain = parsed.split("@", 1)[1]
    for allowed in settings.email_allowlist:
        lowered = allowed.lower()
        if lowered == "*" or lowered == parsed:
            return
        if lowered.startswith("@") and domain == lowered[1:]:
            return
        if "@" not in lowered and domain == lowered:
            return
    raise PermissionError(f"Recipient {parsed} is not in AGENT_EMAIL_ALLOWLIST.")


def get_email(email_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM emails WHERE id = ?", (email_id,)).fetchone()
    return row_to_dict(row)


def list_emails(limit: int = 100, tenant_id: str | None = None) -> list[dict]:
    with get_connection() as conn:
        if tenant_id:
            rows = conn.execute(
                """
                SELECT * FROM emails
                WHERE tenant_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (tenant_id, max(1, min(limit, 500))),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM emails
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (max(1, min(limit, 500)),),
            ).fetchall()
    return rows_to_dicts(rows)


def _email_idempotency_key(
    *,
    to_address: str,
    subject: str,
    body: str,
    approval_id: str | None,
    provider: str,
    tenant_id: str,
) -> str:
    payload = {
        "provider": provider,
        "tenant_id": tenant_id,
        "approval_id": approval_id,
        "to_address": parseaddr(to_address)[1].lower(),
        "subject": subject,
        "body": body,
    }
    return f"email_{hashlib.sha256(json_dumps(payload).encode('utf-8')).hexdigest()}"


def _message_id_for_key(idempotency_key: str) -> str:
    domain = settings.smtp_from_email.split("@")[-1] if "@" in settings.smtp_from_email else "agent.local"
    return f"<{idempotency_key}@{domain}>"
