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
