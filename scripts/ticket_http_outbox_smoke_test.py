from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
AGENT_DB = DATA_DIR / "ticket_http_outbox_smoke_test.sqlite3"
EXTERNAL_DB = DATA_DIR / "external_http_outbox_smoke_test.sqlite3"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_health(url: str, timeout_seconds: float = 15) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if json.loads(response.read().decode("utf-8")).get("status") == "ok":
                    return
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            time.sleep(0.2)
    raise RuntimeError("External ticket service did not become healthy.")


def cleanup(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        candidate.unlink(missing_ok=True)


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cleanup(AGENT_DB)
    cleanup(EXTERNAL_DB)
    port = free_port()
    token = "ticket-http-outbox-smoke-token"
    base_url = f"http://127.0.0.1:{port}"
    service_env = os.environ.copy()
    service_env.update(
        {
            "TICKET_SERVICE_DB_PATH": str(EXTERNAL_DB),
            "TICKET_SERVICE_TOKEN": token,
            "TICKET_SERVICE_PUBLIC_BASE_URL": base_url,
            "TICKET_DASHBOARD_USERNAME": "ticket-desk",
            "TICKET_DASHBOARD_PASSWORD": "TicketDeskPass123",
        }
    )
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    service = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "external_ticket_service.server:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        env=service_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creation_flags,
    )
    try:
        wait_for_health(f"{base_url}/api/health")
        os.environ.update(
            {
                "AGENT_DB_PATH": str(AGENT_DB),
                "AGENT_AUTO_SEED": "false",
                "AGENT_TOOL_MODE": "real",
                "AGENT_TICKET_PROVIDER": "http",
                "TICKET_API_URL": f"{base_url}/api/tickets",
                "TICKET_API_TOKEN": token,
            }
        )
        sys.path.insert(0, str(ROOT))
        from app.db import reset_database
        from app.services.outbox import list_outbox_events, retry_outbox_event
        from app.services.tools import ticketing
        from app.services.tools.ticketing import create_ticket, update_ticket

        reset_database(seed=False)
        ticket = create_ticket(
            "HTTP outbox integration",
            "Validate Agent to external ticket create and update.",
            tenant_id="tenant-http",
            priority="high",
        )
        assert str(ticket.get("external_id", "")).startswith("EXT-"), ticket
        updated = update_ticket(
            ticket["id"],
            status="investigating",
            comment="External update through durable Outbox.",
            tenant_id="tenant-http",
        )
        assert updated and updated["status"] == "investigating", updated
        request = urllib.request.Request(
            f"{base_url}/api/tickets/{ticket['external_id']}",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            external = json.loads(response.read().decode("utf-8"))
        assert external["status"] == "investigating", external
        actions = {event["action_type"]: event["status"] for event in list_outbox_events(tenant_id="tenant-http")}
        assert actions["ticket.create"] == "completed", actions
        assert actions["ticket.update"] == "completed", actions

        original_update = ticketing._update_http_ticket
        ticketing._update_http_ticket = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("simulated outage"))
        try:
            try:
                update_ticket(
                    ticket["id"],
                    status="waiting_customer",
                    comment="This update must be retried.",
                    tenant_id="tenant-http",
                )
            except RuntimeError as exc:
                assert "simulated outage" in str(exc)
            else:
                raise AssertionError("The simulated external outage did not fail the update.")
        finally:
            ticketing._update_http_ticket = original_update
        failed = next(
            event
            for event in list_outbox_events(status="failed", tenant_id="tenant-http")
            if event["action_type"] == "ticket.update"
        )
        retried = retry_outbox_event(failed["id"], tenant_id="tenant-http")
        assert retried["status"] == "completed", retried
        assert ticketing.get_ticket(ticket["id"])["status"] == "waiting_customer"
    finally:
        service.terminate()
        try:
            service.wait(timeout=5)
        except subprocess.TimeoutExpired:
            service.kill()
            service.wait(timeout=5)
        cleanup(AGENT_DB)
        cleanup(EXTERNAL_DB)

    print("ticket_http_outbox_smoke_test passed")


if __name__ == "__main__":
    main()
