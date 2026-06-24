from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db import database_status  # noqa: E402
from app.services.observability import configure_observability, observability_status  # noqa: E402
from app.services.oidc import oidc_status  # noqa: E402
from app.services.operations import operations_status  # noqa: E402
from app.services.queue import queue_status  # noqa: E402


def main() -> None:
    configure_observability()
    database = database_status()
    operations = operations_status()
    observability = observability_status()
    queue = queue_status(check_connection=True)
    oidc = oidc_status()

    print(f"database_status={database['status']}")
    print(f"database_backend={database['backend']}")
    print(f"database_auto_migrate={str(bool(database.get('auto_migrate'))).lower()}")
    profiles = database.get("connection_profiles") or {}
    if profiles:
        for key in ("runtime_configured", "migration_configured", "worker_configured", "readonly_configured"):
            print(f"database_{key}={str(bool(profiles.get(key))).lower()}")
        print(f"database_current_purpose={profiles.get('current_purpose') or ''}")
    rls = database.get("postgres_rls") or {}
    if rls:
        print(f"postgres_rls_enabled={str(bool(rls.get('enabled'))).lower()}")
        print(f"postgres_rls_bypass_role={rls.get('bypass_role') or ''}")
        if "tenant_policy_count" in rls:
            print(f"postgres_rls_tenant_policy_count={rls['tenant_policy_count']}")
        if "bypass_role_exists" in rls:
            print(f"postgres_rls_bypass_role_exists={str(bool(rls['bypass_role_exists'])).lower()}")
        if "current_user_has_bypass_role" in rls:
            print(f"postgres_rls_current_user_has_bypass_role={str(bool(rls['current_user_has_bypass_role'])).lower()}")
    if database.get("latest_migration"):
        print(f"latest_migration={database['latest_migration']['id']}")
    print(f"environment={operations['environment']}")
    print(f"log_format={observability['log_format']}")
    print(f"log_level={observability['log_level']}")
    print(f"otel_enabled={str(bool(observability['otel']['enabled'])).lower()}")
    print(f"otel_configured={str(bool(observability['otel']['configured'])).lower()}")
    if observability["otel"].get("error"):
        print(f"otel_error={observability['otel']['error']}")
    print(f"queue_backend={queue['backend']}")
    print(f"queue_enabled={str(bool(queue['enabled'])).lower()}")
    print(f"queue_redis_url_configured={str(bool(queue['redis_url_configured'])).lower()}")
    print(f"queue_redis_dependency_available={str(bool(queue['redis_dependency_available'])).lower()}")
    if "reachable" in queue:
        print(f"queue_reachable={str(bool(queue['reachable'])).lower()}")
    if queue.get("error"):
        print(f"queue_error={queue['error']}")
    print(f"oidc_enabled={str(bool(oidc['enabled'])).lower()}")
    print(f"oidc_browser_login_enabled={str(bool(oidc['browser_login_enabled'])).lower()}")
    if oidc.get("missing"):
        print(f"oidc_missing={','.join(oidc['missing'])}")
    print(f"production_ready={str(operations['production_ready']).lower()}")
    print(f"blocking_count={operations['blocking_count']}")
    for warning in operations["warnings"]:
        print(f"warning={warning['severity']}:{warning['code']}:{warning['message']}")

    if database["status"] != "ok" or operations["blocking_count"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
