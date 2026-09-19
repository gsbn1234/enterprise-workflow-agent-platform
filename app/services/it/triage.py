"""Deterministic IT request triage.

Everything here is plain keyword rules evaluated in a fixed order. No LLM is
consulted, so the same sentence always produces the same structured result and
every branch is unit-testable. ``TriageResult.mode`` is always
``"deterministic"`` so a later phase can layer an LLM fallback on top without
silently changing the meaning of the field.

Rule order is itself a rule: ``PERMISSION_REQUEST -> SOFTWARE_REQUEST ->
ASSET_REQUEST -> IT_INCIDENT``. Without a fixed order, "申请生产数据库只读权限"
would be claimed by whichever keyword table happened to be consulted first.

A note on ``asset_type``: the uppercase service codes returned here
(``REDIS``/``VPN``/``DATABASE``/...) are deliberately the upper-case form of the
``assets.asset_type`` values in ``app.services.it.mock_data``, so the intake
loop can turn a triage result into an asset lookup without a mapping table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Annotation only. ``classify`` and everything above it stay free of the
    # HTTP client that :mod:`app.services.llm` pulls in; the fallback receives
    # an outcome object and reads two attributes off it.
    from app.services.llm import LlmOutcome


INTENTS: tuple[str, ...] = (
    "IT_INCIDENT",
    "PERMISSION_REQUEST",
    "ASSET_REQUEST",
    "SOFTWARE_REQUEST",
)

# Mirrors ``app.services.tools.ticketing.TICKET_PRIORITIES``. Duplicated rather
# than imported so this module stays free of the heavier ticketing/db import
# chain; ``it_smoke_test`` asserts the two stay in sync.
PRIORITIES: tuple[str, ...] = ("low", "normal", "high", "urgent")

MODES: tuple[str, ...] = ("deterministic", "llm_assisted")

DEFAULT_INCIDENT_CATEGORY = "GENERAL"
DEFAULT_PERMISSION_CATEGORY = "GENERAL_PERMISSION"
DEFAULT_SOFTWARE_CATEGORY = "SOFTWARE"
ASSET_CATEGORY = "ASSET"

# Services whose production incidents (and production access requests) are
# upgraded to ``urgent``.
CRITICAL_SERVICES: tuple[str, ...] = ("REDIS", "VPN", "DATABASE", "SERVER", "NETWORK")

PERMISSION_KEYWORDS: tuple[str, ...] = (
    "权限",
    "开通",
    "授权",
    "只读",
    "读写",
    "白名单",
    "加白",
    "access",
    "permission",
    "readonly",
    "read-only",
    "read only",
)

ASSET_KEYWORDS: tuple[str, ...] = (
    "电脑",
    "笔记本",
    "显示器",
    "台式机",
    "设备",
    "资产",
    "手机",
    "键盘",
    "鼠标",
    "耳机",
    "laptop",
    "notebook",
    "monitor",
    "desktop",
    "workstation",
    "asset",
)

INCIDENT_KEYWORDS: tuple[str, ...] = (
    "连不上",
    "连不了",
    "无法连接",
    "连接失败",
    "无法访问",
    "无法使用",
    "无法登录",
    "打不开",
    "故障",
    "挂了",
    "宕机",
    "崩溃",
    "报错",
    "异常",
    "失败",
    "超时",
    "很慢",
    "太慢",
    "卡住",
    "不可用",
    "起不来",
    "启动不了",
    "启动失败",
    "用不了",
    "访问不了",
    "进不去",
    "坏了",
    "问题",
    "timeout",
    "error",
    "fail",
    "issue",
    "down",
)

# A request verb marks the sentence as an ask rather than a fault report. It is
# what separates "docker 起不来" (incident) from "我要装 docker" (request).
REQUEST_VERBS: tuple[str, ...] = (
    "申请",
    "开通",
    "安装",
    "装一个",
    "给我装",
    "需要",
    "想要",
    "领用",
    "request",
    "install",
    "need",
)

# Symptoms that raise a non-production incident to ``high``.
SEVERE_SYMPTOMS: tuple[str, ...] = (
    "连不上",
    "连不了",
    "无法连接",
    "连接失败",
    "无法访问",
    "无法使用",
    "无法登录",
    "宕机",
    "挂了",
    "不可用",
    "崩溃",
    "超时",
    "timeout",
    "down",
)

PAID_KEYWORDS: tuple[str, ...] = (
    "付费",
    "采购",
    "购买",
    "收费",
    "商业版",
    "商业授权",
    "license",
    "paid",
    "commercial",
)

# Scored, not ordered. ``_match_service`` weighs every hit in this table and
# returns the heaviest code, so the table's order is only the last tie-break
# rather than the deciding rule. Before Phase 4 this table was read
# first-hit-wins, which meant "生产环境数据库连不上，怀疑是 Redis 缓存雪崩" came
# back REDIS — the reporter's guess at a cause outranked the service they
# actually said was unreachable, purely because REDIS is declared first.
SERVICE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("REDIS", ("redis", "缓存", "cache")),
    ("VPN", ("vpn", "虚拟专用网")),
    (
        "DATABASE",
        ("数据库", "database", "mysql", "postgres", "postgresql", "mongodb", "oracle", "sqlserver", "sql server"),
    ),
    ("DOCKER", ("docker", "容器", "k8s", "kubernetes")),
    ("SERVER", ("服务器", "server", "主机", "虚机", "虚拟机")),
    ("NETWORK", ("网络", "网速", "断网", "wifi", "无线网", "network", "dns", "内网")),
    ("EMAIL", ("邮箱", "邮件", "email", "outlook", "smtp")),
    ("OS", ("操作系统", "linux", "windows", "蓝屏")),
    ("GIT", ("gitlab", "github", "代码库", "代码仓库")),
    ("PRINTER", ("打印机", "printer", "扫描仪")),
    ("ENDPOINT", ("电脑", "笔记本", "显示器", "台式机", "键盘", "鼠标", "laptop", "desktop")),
    ("ACCOUNT", ("账号", "账户", "域账号", "sso", "密码错误")),
)

# How ``_match_service`` weighs one keyword hit, by the clause it sits in. A
# report names what is broken and then guesses at why; the two must not count
# the same. "数据库连不上" is the observation, "怀疑是 Redis 缓存雪崩" is the
# hypothesis, and a hypothesis that merely mentions more words must not win.
CLAUSE_SPLITTERS = "，,。；;！!？?、"

# Markers that turn a clause into a guess rather than an observation.
HEDGE_MARKERS: tuple[str, ...] = (
    "怀疑",
    "疑似",
    "可能是",
    "可能",
    "大概",
    "也许",
    "有可能",
    "猜测",
    "估计",
    "或许",
    "是不是",
    "会不会",
    "不确定",
)

# Markers that make a clause an observation. Deliberately the existing
# ``SEVERE_SYMPTOMS`` table rather than a second, drifting vocabulary: these are
# already the symptoms this module treats as a real outage.
SYMPTOM_MARKERS: tuple[str, ...] = SEVERE_SYMPTOMS

OCCURRENCE_SYMPTOM = 3
OCCURRENCE_PLAIN = 2
OCCURRENCE_HEDGED = 1

# Same idea, but for the *resource* a permission is requested on.
RESOURCE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "DATABASE",
        ("数据库", "database", "mysql", "postgres", "postgresql", "mongodb", "oracle", "sql server"),
    ),
    ("REDIS", ("redis", "缓存")),
    ("SERVER", ("服务器", "主机", "server", "虚机", "虚拟机")),
    ("VPN", ("vpn",)),
    ("CODE_REPO", ("代码库", "代码仓库", "仓库", "gitlab", "github", "repo")),
    ("FILE_SHARE", ("共享盘", "文件共享", "网盘", "share", "ftp", "文件服务器")),
    ("CLOUD", ("云平台", "云控制台", "aws", "aliyun", "azure", "k8s")),
    ("ADMIN_CONSOLE", ("管理后台", "后台", "admin", "控制台", "console", "dashboard")),
    ("PROD_ACCOUNT", ("运维账号", "生产账号", "跳板机", "root")),
)

SOFTWARE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("DOCKER", ("docker", "容器")),
    ("IDE", ("ide", "pycharm", "idea", "vscode", "vs code", "goland", "eclipse")),
    ("GIT", ("git", "gitlab", "github")),
    ("OFFICE", ("office", "wps", "excel", "word", "ppt")),
    ("COLLABORATION", ("slack", "zoom", "teams", "飞书", "钉钉")),
    ("DB_CLIENT", ("navicat", "dbeaver", "datagrip")),
    ("RUNTIME", ("java", "jdk", "python", "nodejs", "node", "golang", "maven")),
    ("API_CLIENT", ("postman", "apifox")),
    ("SECURITY", ("杀毒", "antivirus", "vpn 客户端")),
)

SOFTWARE_GENERIC_KEYWORDS: tuple[str, ...] = ("软件", "安装")

ENVIRONMENT_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("production", ("生产", "线上", "正式环境", "production", " prod", "prod环境", "prod 环境")),
    ("staging", ("预发", "灰度", "测试环境", "测试服", "staging", " stage", "stage环境")),
    ("dev", ("开发环境", "本地环境", "本地", "sandbox", " development", "dev环境", "dev 环境")),
)

READ_ONLY_KEYWORDS: tuple[str, ...] = ("只读", "只查询", "查询权限", "readonly", "read-only", "read only")
READ_WRITE_KEYWORDS: tuple[str, ...] = ("读写", "写权限", "可写", "增删改", "readwrite", "read-write", "write")

_CONFIDENCE_BASE = 0.55
_CONFIDENCE_PER_ENTITY = 0.1
_CONFIDENCE_MAX = 0.95
_CONFIDENCE_UNMATCHED = 0.2


@dataclass(frozen=True)
class TriageResult:
    intent: str
    category: str
    priority: str
    entities: dict[str, Any] = field(default_factory=dict)
    needs_approval: bool = False
    missing_information: list[str] = field(default_factory=list)
    confidence: float = 0.0
    mode: str = "deterministic"

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "category": self.category,
            "priority": self.priority,
            "entities": dict(self.entities),
            "needs_approval": self.needs_approval,
            "missing_information": list(self.missing_information),
            "confidence": self.confidence,
            "mode": self.mode,
        }


def classify(text: str) -> TriageResult:
    """Turn a free-text IT request into a structured triage decision."""
    lowered = str(text or "").lower()

    service = _match_service(lowered)
    resource = _match_table(lowered, RESOURCE_KEYWORDS)
    software = _match_table(lowered, SOFTWARE_KEYWORDS)
    environment = _match_environment(lowered)

    permission_hit = _contains_any(lowered, PERMISSION_KEYWORDS)
    software_hit = software is not None or _contains_any(lowered, SOFTWARE_GENERIC_KEYWORDS)
    asset_hit = _contains_any(lowered, ASSET_KEYWORDS)
    incident_hit = _contains_any(lowered, INCIDENT_KEYWORDS)

    intent = _resolve_intent(
        permission_hit=permission_hit,
        software_hit=software_hit,
        asset_hit=asset_hit,
        incident_hit=incident_hit,
        resource_hit=resource is not None,
        request_verb_hit=_contains_any(lowered, REQUEST_VERBS),
        # "授权" is the one word that means both "grant this access" and "buy
        # this licence". These two signals are what tell the two apart: a
        # procurement marker (PAID_KEYWORDS already contains 商业授权/license)
        # and an explicit access level, neither of which the other reading has.
        paid_hit=_contains_any(lowered, PAID_KEYWORDS),
        access_level_hit=_access_level(lowered) is not None,
    )

    entities: dict[str, Any] = {}
    category = DEFAULT_INCIDENT_CATEGORY

    if intent == "PERMISSION_REQUEST":
        category = f"{resource}_PERMISSION" if resource else DEFAULT_PERMISSION_CATEGORY
        if resource:
            entities["resource"] = resource
        access_level = _access_level(lowered)
        if access_level:
            entities["access_level"] = access_level
    elif intent == "SOFTWARE_REQUEST":
        category = software or DEFAULT_SOFTWARE_CATEGORY
        if software:
            entities["software"] = software
    elif intent == "ASSET_REQUEST":
        category = ASSET_CATEGORY
    else:
        category = service or DEFAULT_INCIDENT_CATEGORY
        if service:
            entities["service"] = service

    if environment:
        entities["environment"] = environment

    needs_approval = _needs_approval(intent, environment, lowered)
    missing_information = _missing_information(intent, entities)
    priority = _priority(intent, environment, entities, lowered)
    confidence = _confidence(intent_matched=permission_hit or software_hit or asset_hit or incident_hit, entities=entities)

    return TriageResult(
        intent=intent,
        category=category,
        priority=priority,
        entities=entities,
        needs_approval=needs_approval,
        missing_information=missing_information,
        confidence=confidence,
    )


def _resolve_intent(
    *,
    permission_hit: bool,
    software_hit: bool,
    asset_hit: bool,
    incident_hit: bool,
    resource_hit: bool,
    request_verb_hit: bool,
    paid_hit: bool,
    access_level_hit: bool,
) -> str:
    """First matching rule wins; the order is the specification.

    One deliberate refinement sits ahead of the ordering: a sentence that
    reports a fault and contains no request verb is an incident, whatever nouns
    it happens to contain. Without this, "我的 docker 起不来" would be filed as a
    software *request* merely because it names Docker, and "我的电脑坏了" as an
    asset request. A request verb (申请/安装/需要/...) restores the documented
    order, so "我要申请生产数据库只读权限" still resolves as a permission request.
    """
    if incident_hit and not request_verb_hit:
        return "IT_INCIDENT"
    # "申请 Docker 授权" mentions 授权 but asks for software, not for access to a
    # resource, so a software signal without a permission *resource* wins — and
    # Phase 4 widened that from "no resource at all" to the three questions that
    # actually separate a purchase from an access request:
    #
    # * the business intent: a procurement marker. "申请采购付费数据库客户端授权
    #   Navicat" names a resource word too, but only because 数据库 is part of the
    #   product's name — the request is to *buy Navicat*. PAID_KEYWORDS already
    #   contains 商业授权 and license, so the codebase already reads 授权 in a
    #   procurement context as a licence rather than a grant.
    # * the requested object: a product from SOFTWARE_KEYWORDS.
    # * the action: no explicit access level. "申请采购 Navicat 数据库读写权限"
    #   does state one, and stays a permission request.
    software_acquisition = (
        software_hit and (paid_hit or not resource_hit) and not access_level_hit
    )
    if permission_hit and software_acquisition:
        return "SOFTWARE_REQUEST"
    if permission_hit:
        return "PERMISSION_REQUEST"
    if software_hit:
        return "SOFTWARE_REQUEST"
    if asset_hit:
        return "ASSET_REQUEST"
    # Anything else is treated as an incident report. An unrecognised sentence
    # lands here with a low confidence and an explicit ``missing_information``,
    # which is more useful to a service desk than refusing to classify it.
    return "IT_INCIDENT"


def _priority(intent: str, environment: str | None, entities: dict[str, Any], lowered: str) -> str:
    code = entities.get("service") or entities.get("resource") or ""
    if environment == "production" and code in CRITICAL_SERVICES:
        return "urgent"
    if environment == "production":
        return "high"
    if intent == "IT_INCIDENT" and _contains_any(lowered, SEVERE_SYMPTOMS):
        return "high"
    return "normal"


def _needs_approval(intent: str, environment: str | None, lowered: str) -> bool:
    if intent == "PERMISSION_REQUEST":
        return True
    if intent == "ASSET_REQUEST":
        return True
    if intent == "SOFTWARE_REQUEST":
        # Free tooling is self-service; only paid/licensed software needs a human.
        return _contains_any(lowered, PAID_KEYWORDS)
    # Incidents are handled directly; only production impact needs a human gate.
    return environment == "production"


def _missing_information(intent: str, entities: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    if intent == "IT_INCIDENT":
        if not entities.get("service"):
            missing.append("service")
        if not entities.get("environment"):
            missing.append("environment")
    elif intent == "PERMISSION_REQUEST":
        if not entities.get("resource"):
            missing.append("resource")
        if not entities.get("environment"):
            missing.append("environment")
    elif intent == "SOFTWARE_REQUEST":
        if not entities.get("software"):
            missing.append("software")
    return missing


def _confidence(*, intent_matched: bool, entities: dict[str, Any]) -> float:
    if not intent_matched:
        return _CONFIDENCE_UNMATCHED
    score = _CONFIDENCE_BASE + _CONFIDENCE_PER_ENTITY * len(entities)
    return round(min(_CONFIDENCE_MAX, score), 2)


def _access_level(lowered: str) -> str | None:
    if _contains_any(lowered, READ_ONLY_KEYWORDS):
        return "read_only"
    if _contains_any(lowered, READ_WRITE_KEYWORDS):
        return "read_write"
    return None


def _match_table(
    lowered: str, table: tuple[tuple[str, tuple[str, ...]], ...]
) -> str | None:
    for code, keywords in table:
        if any(keyword in lowered for keyword in keywords):
            return code
    return None


def _match_service(lowered: str) -> str | None:
    """The service a fault report is about, by weight of evidence.

    A report says what is broken and then may guess at why. Counting keyword
    hits alone does not separate the two — "生产环境数据库连不上，怀疑是 Redis
    缓存雪崩" hits Redis twice (redis, 缓存) and the database once, so a plain
    tally would still answer REDIS. What separates them is the *clause*: the
    database is named in an observation, Redis in a hypothesis. So each hit is
    weighed by the clause it sits in, a hypothesis losing to an observation.

    Ties are broken deterministically, and table order is only the last resort:
    score, then symptom-clause hits, then earliest position, then declaration
    order. Removing the order dependence is the point — the same sentence must
    not classify differently because a keyword list was reordered.
    """
    clauses = _clauses(lowered)
    scored: list[tuple[int, int, int, int, str]] = []
    for position, (code, keywords) in enumerate(SERVICE_KEYWORDS):
        score = 0
        symptom_hits = 0
        earliest: int | None = None
        for index, clause in enumerate(clauses):
            weight = _clause_weight(clause)
            if weight == OCCURRENCE_SYMPTOM:
                symptom_clause = True
            else:
                symptom_clause = False
            for keyword in keywords:
                count = clause.count(keyword)
                if not count:
                    continue
                score += weight * count
                if symptom_clause:
                    symptom_hits += count
                if earliest is None:
                    earliest = index
        if score:
            scored.append((score, symptom_hits, earliest if earliest is not None else 0, position, code))
    if not scored:
        return None
    # Highest score, most symptom evidence, earliest mention, then table order.
    best = max(
        scored,
        key=lambda item: (item[0], item[1], -item[2], -item[3]),
    )
    return best[4]


def _clauses(lowered: str) -> list[str]:
    """Split into clauses so a hit can be read in the context it appeared in."""
    clauses = [lowered]
    for splitter in CLAUSE_SPLITTERS:
        clauses = [part for clause in clauses for part in clause.split(splitter)]
    return [clause for clause in (item.strip() for item in clauses) if clause]


def _clause_weight(clause: str) -> int:
    """How much one keyword hit in this clause is worth.

    The hedge is checked first, and that order is the whole point: "怀疑是 Redis
    挂了" contains a symptom word, but the reporter has told us it is a guess
    about the cause rather than the thing they observed. Letting the symptom word
    outrank the hedge would hand the clause full symptom weight and, on a tie,
    let the tie-break below — position, then table order — decide the answer,
    which is the first-hit-wins behaviour this function exists to replace.
    """
    if _contains_any(clause, HEDGE_MARKERS):
        return OCCURRENCE_HEDGED
    if _contains_any(clause, SYMPTOM_MARKERS):
        return OCCURRENCE_SYMPTOM
    return OCCURRENCE_PLAIN


def _match_environment(lowered: str) -> str | None:
    for environment, keywords in ENVIRONMENT_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return environment
    return None


def _contains_any(lowered: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in lowered for keyword in keywords)


# --- LLM fallback (Phase 5-1) ------------------------------------------------
#
# Everything above is deterministic and stays that way: ``classify`` consults no
# model, and the two functions below are never called from it. They exist for
# the one case keyword rules genuinely cannot serve — a request written in words
# this module has no vocabulary for — and they are reached only from
# ``_it_triage_node`` after ``classify`` has already produced an answer.
#
# The shape of the fallback is what keeps it safe. ``classify`` always returns a
# usable result, so the model is never asked to *produce* a triage from nothing;
# it is asked to improve one that came back unsure, and the improvement is then
# merged into the deterministic result rather than replacing it.

# The vocabularies the model's answer must be spelled in. Built from the
# keyword tables above rather than written out again, so a service added to
# ``SERVICE_KEYWORDS`` is automatically a service the fallback may name — and,
# more importantly, one it may not invent.
SERVICE_CODES: tuple[str, ...] = tuple(code for code, _ in SERVICE_KEYWORDS)
RESOURCE_CODES: tuple[str, ...] = tuple(code for code, _ in RESOURCE_KEYWORDS)
SOFTWARE_CODES: tuple[str, ...] = tuple(code for code, _ in SOFTWARE_KEYWORDS)
ENVIRONMENTS: tuple[str, ...] = tuple(code for code, _ in ENVIRONMENT_KEYWORDS)


def needs_llm_fallback(result: TriageResult, threshold: float) -> bool:
    """Whether a deterministic triage came back unsure enough to ask a model.

    Pure, and deliberately narrow: only a result produced by ``classify`` is
    eligible. An ``llm_assisted`` result is not asked again — one fallback per
    request, so a model that answers badly cannot trigger a second opinion that
    answers differently.
    """
    if result.mode != "deterministic":
        return False
    return float(result.confidence) < float(threshold)


def llm_fallback(
    objective: str,
    base: TriageResult,
    outcome: "LlmOutcome",
) -> TriageResult | None:
    """Merge a model's reading of an unsure request into the deterministic one.

    Returns ``None`` when the answer cannot be used, which leaves the caller
    holding ``base`` — the deterministic result is never made worse by asking.

    Three rules, in the order they are applied:

    1. **The intent must be one this platform has.** §七 asks for exactly this:
       a model may pick from ``INTENTS``, it may not add to it. An unrecognised
       intent fails the whole answer, because a result whose intent is not in
       the enum would go on to be counted as an incident by code that assumes
       the enum holds.
    2. **Entities are filled, not replaced.** A field ``classify`` already
       determined wins; the model supplies only what was missing. Both
       directions of that rule are fail-closed. A model cannot downgrade a
       production reading to dev, and where it raises one — naming production
       on a request that did not say — the result is more approval, not less.
       Names outside the vocabularies above are dropped rather than stored, so
       an invented service code cannot reach an asset lookup.
    3. **Every derived field is recomputed here, in code.** ``category``,
       ``priority``, ``missing_information`` and ``needs_approval`` come from
       the same deterministic helpers ``classify`` uses, over the merged
       entities. None of them is read from the model's answer. Approvals in
       particular are ``or``-ed with the deterministic value, so this function
       cannot be the reason a human is removed from a request.

    ``confidence`` is the deterministic formula applied to the fuller entity
    set, not the model's self-report — the claim is recorded by the caller in
    the audit detail and is not allowed to stand in for a computed number.
    """
    if not outcome.ok:
        return None
    value = outcome.value or {}

    intent = str(value.get("intent") or "").strip().upper()
    if intent not in INTENTS:
        return None

    entities = dict(base.entities)
    _fill_entity(entities, "service", value.get("service"), SERVICE_CODES)
    _fill_entity(entities, "resource", value.get("resource"), RESOURCE_CODES)
    _fill_entity(entities, "software", value.get("software"), SOFTWARE_CODES)
    _fill_entity(entities, "environment", value.get("environment"), ENVIRONMENTS)

    environment = entities.get("environment")
    lowered = str(objective or "").lower()

    if intent == "PERMISSION_REQUEST":
        resource = entities.get("resource")
        category = f"{resource}_PERMISSION" if resource else DEFAULT_PERMISSION_CATEGORY
    elif intent == "SOFTWARE_REQUEST":
        category = entities.get("software") or DEFAULT_SOFTWARE_CATEGORY
    elif intent == "ASSET_REQUEST":
        category = ASSET_CATEGORY
    else:
        category = entities.get("service") or DEFAULT_INCIDENT_CATEGORY

    priority = _priority(intent, environment, entities, lowered)
    if PRIORITIES.index(base.priority) > PRIORITIES.index(priority):
        priority = base.priority
    return TriageResult(
        intent=intent,
        category=category,
        priority=priority,
        entities=entities,
        # Escalate-only, for the same reason the risk votes are: a model that
        # reads the request as harmless must not be able to remove a gate the
        # keyword rules already raised.
        needs_approval=bool(base.needs_approval or _needs_approval(intent, environment, lowered)),
        missing_information=_missing_information(intent, entities),
        confidence=_confidence(intent_matched=True, entities=entities),
        mode="llm_assisted",
    )


def _fill_entity(
    entities: dict[str, Any], key: str, candidate: object, vocabulary: tuple[str, ...]
) -> None:
    """Set ``key`` from the model only when it is empty and the value is known.

    The membership test is the point. ``category`` for an incident is the
    service code and doubles as the ``assets.asset_type`` lookup key, so an
    unchecked string here would travel from a language model to a database
    query. An unknown name is dropped, which leaves the field missing — and a
    missing field shows up in ``missing_information``, where a service desk can
    see it, rather than silently becoming a lookup for something that is not
    there.
    """
    if entities.get(key):
        return
    text = str(candidate or "").strip()
    if not text:
        return
    upper = text.upper()
    if upper in vocabulary:
        entities[key] = upper
    elif text in vocabulary:
        entities[key] = text
