from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ["AGENT_DB_PATH"] = str(ROOT / "data" / "it_triage_smoke_test.sqlite3")
os.environ["KNOWLEDGE_RAG_BASE_URL"] = ""
os.environ["AGENT_TOOL_MODE"] = "mock"
os.environ["AGENT_TICKET_PROVIDER"] = "mock"
os.environ["AGENT_EMAIL_PROVIDER"] = "mock"
sys.path.insert(0, str(ROOT))

from app.services.it.triage import INTENTS, PRIORITIES, classify  # noqa: E402
from app.services.tools.ticketing import TICKET_PRIORITIES  # noqa: E402


def main() -> None:
    # Triage is pure code: no database, no LLM, no optional dependency needed.
    assert set(INTENTS) == {"IT_INCIDENT", "PERMISSION_REQUEST", "ASSET_REQUEST", "SOFTWARE_REQUEST"}, INTENTS
    assert set(PRIORITIES) == TICKET_PRIORITIES, (PRIORITIES, TICKET_PRIORITIES)

    redis_outage = classify("我的 Redis 连不上了")
    assert redis_outage.intent == "IT_INCIDENT", redis_outage.to_dict()
    assert redis_outage.category == "REDIS", redis_outage.to_dict()
    assert redis_outage.priority == "high", redis_outage.to_dict()
    assert redis_outage.mode == "deterministic", redis_outage.to_dict()
    assert "environment" in redis_outage.missing_information, redis_outage.to_dict()

    vpn_outage = classify("我的 VPN 无法连接")
    assert vpn_outage.intent == "IT_INCIDENT", vpn_outage.to_dict()
    assert vpn_outage.category == "VPN", vpn_outage.to_dict()

    permission = classify("我要申请生产数据库只读权限")
    assert permission.intent == "PERMISSION_REQUEST", permission.to_dict()
    assert permission.category == "DATABASE_PERMISSION", permission.to_dict()
    assert permission.needs_approval is True, permission.to_dict()
    assert permission.entities["access_level"] == "read_only", permission.to_dict()
    assert permission.entities["environment"] == "production", permission.to_dict()
    # Production access on a critical service escalates.
    assert permission.priority == "urgent", permission.to_dict()
    assert permission.missing_information == [], permission.to_dict()

    software = classify("我要申请 Docker")
    assert software.intent == "SOFTWARE_REQUEST", software.to_dict()
    assert software.category == "DOCKER", software.to_dict()
    # Free tooling is self-service; only paid software needs a human gate.
    assert software.needs_approval is False, software.to_dict()

    paid_software = classify("我要申请购买 Office 商业授权")
    assert paid_software.intent == "SOFTWARE_REQUEST", paid_software.to_dict()
    assert paid_software.needs_approval is True, paid_software.to_dict()

    asset = classify("我要申请一台新电脑")
    assert asset.intent == "ASSET_REQUEST", asset.to_dict()
    assert asset.category == "ASSET", asset.to_dict()
    assert asset.needs_approval is True, asset.to_dict()

    # A fault report naming software is an incident, not a software request.
    docker_fault = classify("我的本地开发环境 docker 起不来")
    assert docker_fault.intent == "IT_INCIDENT", docker_fault.to_dict()
    assert docker_fault.category == "DOCKER", docker_fault.to_dict()
    assert docker_fault.entities["environment"] == "dev", docker_fault.to_dict()

    # A request verb restores the documented ordering even when a fault word is present.
    explicit_ask = classify("我要申请生产服务器的 root 权限")
    assert explicit_ask.intent == "PERMISSION_REQUEST", explicit_ask.to_dict()
    assert explicit_ask.category == "SERVER_PERMISSION", explicit_ask.to_dict()

    # Unrecognised text is still classified, but honestly: low confidence and
    # an explicit list of what a service desk would need to ask for.
    unknown = classify("帮我看看")
    assert unknown.intent == "IT_INCIDENT", unknown.to_dict()
    assert unknown.category == "GENERAL", unknown.to_dict()
    assert unknown.confidence == 0.2, unknown.to_dict()
    assert unknown.missing_information == ["service", "environment"], unknown.to_dict()

    # Determinism: identical input always yields an identical structured result.
    assert classify("我的 Redis 连不上了").to_dict() == redis_outage.to_dict()

    print("it_triage_smoke_test passed")


if __name__ == "__main__":
    main()
