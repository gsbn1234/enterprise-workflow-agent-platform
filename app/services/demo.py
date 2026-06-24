from __future__ import annotations

from typing import Any


DEMO_SCENARIOS: list[dict[str, Any]] = [
    {
        "id": "refund",
        "title": "退款投诉",
        "category": "refund",
        "risk": "high",
        "objective": "客户 Orbit Retail 投诉上月服务中断，要求退费 800 元，请创建工单并准备回复 fjsmlfy@gmail.com",
        "demonstrates": ["客户上下文", "RAG 政策依据", "退款审批链", "外部工单", "审批后邮件"],
        "expected": "创建退款工单，生成邮件草稿，等待 Customer Success Manager 和 Finance 审批。",
    },
    {
        "id": "security",
        "title": "安全事件",
        "category": "security",
        "risk": "high",
        "objective": "发现 Acme China 账号疑似异常登录并可能存在权限泄露，请通知安全团队并保留审计，不要直接删除数据",
        "demonstrates": ["安全分类", "禁止破坏性动作", "内部通知", "审计保留", "安全审批"],
        "expected": "创建安全工单，通知 Security，等待 Security Lead 审批，审批后进入 investigating。",
    },
    {
        "id": "procurement",
        "title": "采购审批",
        "category": "procurement",
        "risk": "medium",
        "objective": "市场团队申请采购一套数据分析软件，预算 3600 元，请评估风险、创建采购工单并等待负责人审批",
        "demonstrates": ["预算识别", "采购审批链", "政策依据", "工单状态流转"],
        "expected": "创建采购工单，并等待 Procurement Manager 审批。",
    },
    {
        "id": "remote_work",
        "title": "远程办公",
        "category": "remote_work",
        "risk": "low",
        "objective": "员工 Alice 申请下周三远程办公一天，请根据企业政策判断是否可自动处理并创建记录",
        "demonstrates": ["低风险自动化", "RAG 知识命中", "无需审批", "自动完成工单"],
        "expected": "查询远程办公政策，创建 People Ops 工单并自动标记 approved。",
    },
    {
        "id": "access",
        "title": "权限申请",
        "category": "access_request",
        "risk": "medium",
        "objective": "销售同事申请开通客户数据导出权限，用于本周客户复盘，请创建权限申请工单并等待负责人审批",
        "demonstrates": ["权限风险", "IT Access 审批", "禁止越权授权", "审计留痕"],
        "expected": "创建 IT Access 工单，等待 Line Manager 和 IT Access 审批。",
    },
    {
        "id": "incident",
        "title": "P1 故障",
        "category": "incident",
        "risk": "high",
        "objective": "核心服务 P1 故障影响企业客户登录，请创建故障工单、查询 SLA 政策并通知值班负责人",
        "demonstrates": ["P1 识别", "SLA 依据", "SRE 通知", "Incident Commander 审批"],
        "expected": "创建 SRE 工单，通知值班负责人，等待 Incident Commander 审批。",
    },
    {
        "id": "ticket_query",
        "title": "\u67e5\u8be2\u5de5\u5355",
        "category": "ticket_query",
        "risk": "low",
        "objective": "\u67e5\u8be2 Customer Success \u90e8\u95e8\u5de5\u5355",
        "demonstrates": ["\u81ea\u7136\u8bed\u8a00\u67e5\u8be2", "\u89d2\u8272\u6743\u9650\u8fc7\u6ee4", "\u5df2\u6709\u5de5\u5355\u56de\u663e"],
        "expected": "\u4e0d\u521b\u5efa\u65b0\u5de5\u5355\uff0c\u76f4\u63a5\u67e5\u8be2\u5df2\u6709\u5de5\u5355\u5e76\u6309\u767b\u5f55\u7528\u6237\u6743\u9650\u8fd4\u56de\u7ed3\u679c\u3002",
    },
    {
        "id": "ticket_update",
        "title": "\u4fee\u6539\u5de5\u5355",
        "category": "ticket_update",
        "risk": "medium",
        "objective": "\u628a\u6700\u8fd1\u4e00\u4e2a Customer Success \u5de5\u5355\u4f18\u5148\u7ea7\u6539\u6210 urgent\uff0c\u5e76\u5907\u6ce8\uff1a\u5ba2\u6237\u5df2\u4e8c\u6b21\u50ac\u4fc3\uff0c\u5347\u7ea7\u5904\u7406",
        "demonstrates": ["\u81ea\u7136\u8bed\u8a00\u4fee\u6539", "\u90e8\u95e8\u6743\u9650", "\u5916\u90e8\u5de5\u5355\u540c\u6b65"],
        "expected": "\u627e\u5230\u6700\u8fd1\u53ef\u89c1\u7684 Customer Success \u5de5\u5355\uff0c\u5c06\u4f18\u5148\u7ea7\u6539\u4e3a urgent\uff0c\u5e76\u5728\u5de5\u5355\u65f6\u95f4\u7ebf\u8ffd\u52a0\u5907\u6ce8\u3002",
    },
]


def list_demo_scenarios() -> list[dict[str, Any]]:
    return DEMO_SCENARIOS
