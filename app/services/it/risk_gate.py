"""Deterministic risk gate for IT actions.

This module decides whether a proposed IT action may run automatically, needs a
human, or is refused outright. It is **the** authority on that question: the
resolution agent's ``confidence`` is carried through to the audit record but is
never read by any branch here, so a model that talks itself into 0.99 certainty
still cannot move a production restart past a human.

Three properties are deliberate:

* **Pure.** No ``app.*`` imports, no database, no I/O, no LLM. The same inputs
  always produce the same decision, and the whole rule set is unit-testable
  without a fixture. This mirrors ``app.services.it.triage``.
* **Fail-closed.** An action type that is not in :data:`ACTION_CLASSES` is
  denied rather than defaulted to something permissive. This mirrors
  ``app.services.it.rbac.DEFAULT_REQUIRED_ROLE``.
* **The rule order is the specification.** First matching rule wins and becomes
  ``rule_id``, so the audit trail names the exact clause that forced the
  outcome. Without a fixed order, "production restart" could be justified by
  whichever check happened to run first.

The ladder, in order:

===== ========================================== ==================
Rule  Condition                                  Outcome
===== ========================================== ==================
R0    unknown ``action_type``                    DENY
R1    action class not allowed                   DENY
R2    action class always needs a human          REQUIRE_APPROVAL
R3    side effect on a production asset          REQUIRE_APPROVAL
R3b   side effect on a critical asset            REQUIRE_APPROVAL
R4    triage flagged ``needs_approval``          REQUIRE_APPROVAL
R5    no knowledge evidence                      REQUIRE_APPROVAL, not executable
R6    missing information + side effect          REQUIRE_APPROVAL, not executable
R7    action class is informational              REQUIRE_APPROVAL, not executable
===== ========================================== ==================

R5 exists because of the "never invent an answer" rule: an action proposed with
no retrieved evidence is not a plan, it is a guess, and it goes to a human
instead of to a tool.

``executable`` is the second, independent bit: a decision can require approval
and still be executable once that approval arrives (a production restart), or
require approval and be *un*-executable regardless (no evidence, no concrete
tool). ``app.services.it.execution`` is what reads it, and it refuses to run
anything whose ``executable`` is false — approval alone does not unlock it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


DECISION_AUTO_EXECUTE = "auto_execute"
DECISION_REQUIRE_APPROVAL = "require_approval"
DECISION_DENY = "deny"

DECISIONS: tuple[str, ...] = (DECISION_AUTO_EXECUTE, DECISION_REQUIRE_APPROVAL, DECISION_DENY)

MODE = "deterministic"

DEFAULT_ENVIRONMENT = "dev"

INFORMATIONAL_RISK_CLASS = "informational"
READ_ONLY_RISK_CLASS = "read_only"

# Severity ladders, least severe first. The gate reads the *most* severe value
# it is given, never the most specific source, so a caller can add evidence
# without ever being able to talk the gate down.
ENVIRONMENT_SEVERITY: tuple[str, ...] = ("dev", "staging", "production")
CRITICALITY_SEVERITY: tuple[str, ...] = ("normal", "important", "critical")

# Fail-closed defaults for when the target asset's own metadata cannot be read
# at all — no asset id, no such row, or a refused read. "We could not verify
# this target" is treated as the worst case, not as a licence to skip the check.
UNKNOWN_ENVIRONMENT = "production"
UNKNOWN_CRITICALITY = "critical"


@dataclass(frozen=True)
class ActionClass:
    """What kind of thing the agent is proposing to do.

    ``tool_name`` is the registry tool that performs it (``None`` for classes
    that have no tool because they are never executed). ``requires_approval``
    marks classes that can *never* be automatic regardless of environment —
    the "service restart -> Human Approval" and "permission grant -> Human
    Approval" requirements are expressed here, not as a special case in
    :func:`evaluate`.
    """

    action_type: str
    tool_name: str | None
    risk_class: str
    side_effect: bool
    reversible: bool
    requires_approval: bool
    allowed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_type": self.action_type,
            "tool_name": self.tool_name,
            "risk_class": self.risk_class,
            "side_effect": self.side_effect,
            "reversible": self.reversible,
            "requires_approval": self.requires_approval,
            "allowed": self.allowed,
        }


def _action_class(
    action_type: str,
    tool_name: str | None,
    risk_class: str,
    *,
    side_effect: bool,
    reversible: bool,
    requires_approval: bool,
    allowed: bool = True,
) -> ActionClass:
    return ActionClass(
        action_type=action_type,
        tool_name=tool_name,
        risk_class=risk_class,
        side_effect=side_effect,
        reversible=reversible,
        requires_approval=requires_approval,
        allowed=allowed,
    )


ACTION_CLASSES: dict[str, ActionClass] = {
    # Read-only diagnosis. The one class that is automatic even in production.
    "diagnostic_read": _action_class(
        "diagnostic_read", "diagnose_service", "read_only",
        side_effect=False, reversible=True, requires_approval=False,
    ),
    # Reversible write: automatic outside production, human inside it.
    "cache_flush": _action_class(
        "cache_flush", "flush_cache", "reversible_write",
        side_effect=True, reversible=True, requires_approval=False,
    ),
    # Restarting a service always needs a human, in every environment.
    "service_restart": _action_class(
        "service_restart", "restart_service", "service_restart",
        side_effect=True, reversible=True, requires_approval=True,
    ),
    # Granting access always needs a human, in every environment.
    "permission_grant": _action_class(
        "permission_grant", "grant_permission", "permission_change",
        side_effect=True, reversible=False, requires_approval=True,
    ),
    # Nothing to run: the ticket itself is the deliverable.
    "no_action": _action_class(
        "no_action", None, INFORMATIONAL_RISK_CLASS,
        side_effect=False, reversible=True, requires_approval=False,
    ),
    # Denied classes. They exist so the gate can *name* what it refused; no
    # tool is registered for any of them, so there is nothing to run even if a
    # future caller ignored the decision.
    "data_delete": _action_class(
        "data_delete", None, "destructive",
        side_effect=True, reversible=False, requires_approval=True, allowed=False,
    ),
    "permission_revoke": _action_class(
        "permission_revoke", None, "destructive",
        side_effect=True, reversible=False, requires_approval=True, allowed=False,
    ),
    "account_disable": _action_class(
        "account_disable", None, "destructive",
        side_effect=True, reversible=False, requires_approval=True, allowed=False,
    ),
}


@dataclass(frozen=True)
class RiskGateDecision:
    decision: str
    executable: bool
    action_type: str
    tool_name: str | None
    risk_class: str
    environment: str | None
    rule_id: str
    reasons: tuple[str, ...] = ()
    llm_confidence: float | None = None
    llm_confidence_used: bool = False
    mode: str = MODE
    inputs: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "executable": self.executable,
            "action_type": self.action_type,
            "tool_name": self.tool_name,
            "risk_class": self.risk_class,
            "environment": self.environment,
            "rule_id": self.rule_id,
            "reasons": list(self.reasons),
            "llm_confidence": self.llm_confidence,
            "llm_confidence_used": self.llm_confidence_used,
            "mode": self.mode,
            "inputs": dict(self.inputs),
        }


def evaluate(
    *,
    action_type: Any,
    environment: Any = None,
    criticality: Any = None,
    triage_needs_approval: bool = False,
    evidence_count: int = 0,
    missing_information: Any = (),
    llm_confidence: Any = None,
    actor_role: Any = None,
) -> RiskGateDecision:
    """Decide what may happen to the proposed action.

    ``llm_confidence`` is accepted purely so it can be echoed into the audit
    row next to ``llm_confidence_used: False``. It is not read by any branch —
    see the module docstring.
    """
    requested = str(action_type or "").strip().lower()
    normalized_environment = _normalize_environment(environment)
    normalized_criticality = str(criticality or "").strip().lower() or "normal"
    missing = [str(item) for item in (missing_information or ())]
    try:
        evidence = int(evidence_count or 0)
    except (TypeError, ValueError):
        evidence = 0

    inputs = {
        "action_type": requested or None,
        "environment": normalized_environment,
        "criticality": normalized_criticality,
        "triage_needs_approval": bool(triage_needs_approval),
        "evidence_count": evidence,
        "missing_information": missing,
        "actor_role": actor_role,
    }

    def decide(
        decision: str, rule_id: str, reasons: list[str], *, executable: bool
    ) -> RiskGateDecision:
        return RiskGateDecision(
            decision=decision,
            executable=executable,
            action_type=requested,
            tool_name=cls.tool_name if cls else None,
            risk_class=cls.risk_class if cls else "unknown",
            environment=normalized_environment,
            rule_id=rule_id,
            reasons=tuple(reasons),
            llm_confidence=_clamp(llm_confidence),
            llm_confidence_used=False,
            inputs=inputs,
        )

    cls = ACTION_CLASSES.get(requested)
    # R0: fail closed on anything the gate does not recognise.
    if cls is None:
        return decide(
            DECISION_DENY, "unknown_action_type", ["unknown_action_type"], executable=False
        )
    # R1: classes the platform refuses outright.
    if not cls.allowed:
        rule = f"denied_action_class:{cls.risk_class}"
        return decide(DECISION_DENY, rule, [rule], executable=False)

    reasons: list[str] = []
    decision = DECISION_AUTO_EXECUTE
    executable = cls.tool_name is not None
    rule_id = _automatic_rule(cls)

    def escalate(rule: str, *, runnable: bool | None = None) -> None:
        nonlocal decision, rule_id, executable
        if not reasons:
            # First rule to fire becomes the headline; later ones still accrue.
            rule_id = rule
        decision = DECISION_REQUIRE_APPROVAL
        if rule not in reasons:
            reasons.append(rule)
        if runnable is False:
            executable = False

    # R2: classes that can never be automatic.
    if cls.requires_approval:
        escalate(f"action_class_requires_approval:{cls.risk_class}")
    # R3: a side effect on a production asset always gets a human.
    if cls.side_effect and normalized_environment == "production":
        escalate("production_side_effect")
    # R3b: nor does one on an asset the directory marks critical.
    if cls.side_effect and normalized_criticality == "critical":
        escalate("critical_asset_side_effect")
    # R4: the deterministic triage already decided a human belongs here.
    if triage_needs_approval:
        escalate("triage_requires_approval")
    # R5: no evidence means no automatic action — the plan is a guess.
    if evidence <= 0:
        escalate("no_knowledge_handoff", runnable=False)
    # R6: an under-specified request gets a question, not a tool call.
    if missing and cls.side_effect:
        escalate("request_more_information", runnable=False)
    # R7: nothing to execute; the ticket is the deliverable.
    if cls.risk_class == INFORMATIONAL_RISK_CLASS:
        escalate("no_executable_action", runnable=False)

    if decision == DECISION_AUTO_EXECUTE:
        executable = cls.tool_name is not None
    else:
        # Approval can unlock an executable action, but approval alone must not
        # turn an un-runnable one into a runnable one.
        executable = executable and cls.tool_name is not None

    return decide(decision, rule_id, reasons or [rule_id], executable=executable)


def _automatic_rule(cls: ActionClass) -> str:
    """The positive justification used as ``rule_id`` when nothing escalated."""
    if cls.risk_class == READ_ONLY_RISK_CLASS:
        return "read_only_action"
    if not cls.side_effect:
        return "no_side_effect_action"
    return "non_production_reversible_action"


def merge_environment(*values: Any) -> str | None:
    """The most severe of the environments given, ignoring the unknown ones.

    This is how the target asset's own metadata is combined with what the
    reporter said. Most-severe rather than "asset wins" is deliberate: strict
    precedence would also *downgrade* — a ticket that says production but
    resolves to a staging asset would drop from a human gate to automatic — and
    a risk gate must not be talked downwards by any single source. Adding the
    asset row can only ever raise the answer.
    """
    return _most_severe(values, ENVIRONMENT_SEVERITY)


def merge_criticality(*values: Any) -> str | None:
    """The most severe of the criticalities given. See :func:`merge_environment`."""
    return _most_severe(values, CRITICALITY_SEVERITY)


def _most_severe(values: tuple[Any, ...], ladder: tuple[str, ...]) -> str | None:
    known = [str(value).strip().lower() for value in values if value is not None]
    ranked = [item for item in known if item in ladder]
    if not ranked:
        # Values were supplied but none is on the ladder. Unknown is not the
        # same as absent, so this is the worst case rather than ``None``.
        return ladder[-1] if any(known) else None
    return max(ranked, key=ladder.index)


def _normalize_environment(environment: Any) -> str | None:
    value = str(environment or "").strip().lower()
    return value or None


def _clamp(value: Any) -> float | None:
    """Echo the model's own confidence, bounded. Never consulted by a branch."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(max(0.0, min(number, 1.0)), 4)
