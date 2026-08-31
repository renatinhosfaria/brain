"""Value types for the deterministic lifecycle engine.

Spec section 2.5: the transitions themselves are code, never a model judgment.
These types carry the evidence a transition is allowed to rest on, and nothing
else.
"""

from __future__ import annotations

from dataclasses import dataclass

CREATING_DECISION = "LEAD_NOVO_CADASTRADO"

PHASE_ACTIVE = "active"
PHASE_TERMINAL = "terminal"

FACT_CLIENT_CREATED = "client_created_sem_atendimento"
FACT_FIRST_T1_SEND_SUCCESS = "first_t1_send_success"
FACT_FIRST_HUMAN_INBOUND = "first_human_inbound"

SEM_ATENDIMENTO = "Sem Atendimento"
NAO_RESPONDEU = "Não Respondeu"
EM_ATENDIMENTO = "Em Atendimento"

# Spec section 16. No other status transition is authorised, in either
# direction: a status a human moved is never walked back by automation.
ALLOWED_TRANSITIONS = frozenset(
    {
        (SEM_ATENDIMENTO, NAO_RESPONDEU),
        (SEM_ATENDIMENTO, EM_ATENDIMENTO),
        (NAO_RESPONDEU, EM_ATENDIMENTO),
    }
)

EFFECT_PENDING = "pending"
EFFECT_SUPERSEDED = "superseded"
# States Brain owns and may still change on its own; anything else is either
# a writer's business or already final.
EFFECT_BRAIN_OWNED = frozenset({EFFECT_PENDING})

TRANSPORT_CTWA = "ctwa_candidate"
TRANSPORT_ORDINARY = "ordinary_inbound"

BIND_CREATED = "created"
BIND_NOOP = "noop"
BIND_CONFLICT = "conflict"
BIND_SKIPPED = "skipped"


@dataclass(frozen=True)
class BindResult:
    """Outcome of trying to bind one terminal Cadastro run to a lifecycle.

    ``skipped`` is the ordinary answer for evidence that does not authorise a
    lifecycle, and is not an error: most Cadastro runs legitimately create
    nothing. ``conflict`` is different — it means the same origin was already
    bound to another client, which needs a human.
    """

    status: str
    lifecycle_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class LifecycleRecord:
    lifecycle_id: str
    origin_event_id: str
    wa_turn_id: str
    contact_key: str
    client_id: int
    phase: str
    last_proven_status: str | None


@dataclass(frozen=True)
class DeliveryMatch:
    """Outcome of looking for the one delivered obligation that proves T1.

    ``not_proven`` and ``ambiguous`` are both refusals: spec section 15 allows
    no guess between indistinguishable ledger rows, because the consequence is
    a CRM status the lead never earned.
    """

    status: str
    obligation_id: str | None = None
    delivered_at: float | None = None
    reason: str | None = None


def desired_status(facts: set[str] | frozenset[str]) -> str:
    """The status the evidence implies, as a pure function of the fact set.

    Spec section 14. Deriving from a set rather than from a sequence is what
    makes out-of-order evidence safe: a human reply that Brain learns about
    before the T1 proof yields the same answer as the other order.
    """
    if FACT_FIRST_HUMAN_INBOUND in facts:
        return EM_ATENDIMENTO
    if FACT_FIRST_T1_SEND_SUCCESS in facts:
        return NAO_RESPONDEU
    return SEM_ATENDIMENTO
