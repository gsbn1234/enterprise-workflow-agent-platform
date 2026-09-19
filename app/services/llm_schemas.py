"""Pydantic schemas for the six places an LLM answer becomes a structured value.

These live beside :mod:`app.services.llm` rather than in :mod:`app.schemas`
because they are not HTTP contract: nothing here is ever accepted from a client
or returned to one. They are the shape an *untrusted* producer's answer has to
have before any code downstream will read a field out of it.

**Shape here, vocabulary in the code.** A schema in this module checks that a
field is a string, that a list is a list, that a number sits in range. It does
not check that ``intent`` is one this platform has, that ``service`` is a code
its corpus uses, or that ``action_type`` is one the risk gate knows. Those
checks stay in the deterministic modules that own the vocabulary, and the split
is deliberate: "the model answered in the wrong shape" and "the model named
something that does not exist" are different events, they are audited under
different codes, and only the second one is a statement about the business.

Two fields are exceptions, and both are conservatively biased:

* :attr:`RiskVoteSuggestion.needs_approval` refuses to read an unrecognised value
  as "no". A risk vote that cannot be parsed must not be able to remove a human.
* :attr:`RunbookStep.requires_approval` defaults to ``True``, so a step the model
  did not describe is treated as one that needs a human rather than one that
  does not.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class IntentFallbackResult(BaseModel):
    """Scenario 1: what the model says when deterministic triage is unsure.

    Every field is optional except the intent itself, because a model that
    recognises the request but not the service is still useful — the caller
    keeps whichever entities it already had.
    """

    model_config = ConfigDict(extra="ignore")

    intent: str = Field(min_length=1, max_length=60)
    service: str | None = Field(default=None, max_length=60)
    resource: str | None = Field(default=None, max_length=60)
    environment: str | None = Field(default=None, max_length=40)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = Field(default="", max_length=500)


class RunbookStep(BaseModel):
    """One proposed step. ``action_type`` is a *proposal*, not a command.

    The value has to survive two later gates before anything runs: the
    deterministic mapping in the resolution agent, which drops any name that is
    not a key of ``ACTION_CLASSES``, and then the risk gate. ``requires_approval``
    is a claim the model makes and the gate ignores — it defaults to ``True`` so
    that an omitted field reads as caution rather than as permission.
    """

    model_config = ConfigDict(extra="ignore")

    description: str = Field(min_length=1, max_length=400)
    action_type: str = Field(min_length=1, max_length=60)
    requires_approval: bool = True
    citation: str | None = Field(default=None, max_length=200)


class RunbookCandidate(BaseModel):
    """Scenario 2: candidate steps grounded in already-retrieved formal evidence.

    Capped at five because this is a suggestion beside a deterministic action,
    not a plan of record. An empty list is a valid answer and means the model
    found nothing in the evidence worth proposing.
    """

    model_config = ConfigDict(extra="ignore")

    steps: list[RunbookStep] = Field(default_factory=list, max_length=5)


class EvidenceSynthesisResult(BaseModel):
    """The two retrieval channels reconciled into one short statement."""

    model_config = ConfigDict(extra="ignore")

    summary: str = Field(default="", max_length=1200)
    conflicts: list[str] = Field(default_factory=list, max_length=10)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class RiskVoteSuggestion(BaseModel):
    """An independent risk vote. Advisory: it can raise risk, never lower it."""

    model_config = ConfigDict(extra="ignore")

    risk_level: str = Field(default="", max_length=20)
    needs_approval: bool | None = None
    warnings: list[str] = Field(default_factory=list, max_length=12)
    reason: str = Field(default="", max_length=500)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("needs_approval", mode="before")
    @classmethod
    def _refuse_to_read_no(cls, value: object) -> object:
        """Only an explicit claim of escalation raises risk.

        A missing field and an unreadable one are both ``None`` — the model made
        no claim, and no claim is not a permission. Reading an unrecognised
        string as ``False`` here would be the one place in this file where a
        typo could quietly delete a human, so unrecognised values do not become
        ``False``; they become silence, and the caller keeps the deterministic
        approval requirement it already had.
        """
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, str):
            text = value.strip().lower()
            if text in {"true", "yes", "on", "t", "y", "1"}:
                return True
            if text in {"false", "no", "off", "f", "n", "0"}:
                return False
            return None
        if isinstance(value, (int, float)):
            return bool(value)
        return None


class CriticFinding(BaseModel):
    """One concrete quality gap. ``code`` and ``severity`` are checked by code."""

    model_config = ConfigDict(extra="ignore")

    severity: str = Field(default="", max_length=20)
    code: str = Field(default="", max_length=80)
    message: str = Field(default="", max_length=500)


class CriticReviewResult(BaseModel):
    """The critic's independent read of the trace.

    The model may report gaps; it may not report a pass, and it may not invent a
    tool call. ``findings`` therefore has no "passed" counterpart to set — the
    score is computed from these findings by deterministic code, never supplied.
    """

    model_config = ConfigDict(extra="ignore")

    findings: list[CriticFinding] = Field(default_factory=list, max_length=8)
