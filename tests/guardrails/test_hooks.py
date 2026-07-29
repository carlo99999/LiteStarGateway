"""Guardrails wired into the call path: where the hooks sit, and what that costs.

Two placement decisions are the substance of this file, and each has a test that
fails if the hook is moved:

* the request hook runs BEFORE admission, so a blocked prompt reserves none of
  the team's budget — it never reached a provider;
* the response hook runs AFTER settlement, so a blocked answer is still billed —
  the provider really produced those tokens, and not billing them would hand
  anyone who can trip the response guardrail a free channel.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from litestar_gateway.application.completion_service import CompletionService
from litestar_gateway.application.guardrails.service import ChainedProvider
from litestar_gateway.application.usage_meter import UsageMeter
from litestar_gateway.domain.entities import (
    Budget,
    BudgetWindow,
    Model,
    ModelType,
    Provider,
    TraceRecord,
    UsageEvent,
)
from litestar_gateway.domain.exceptions import GuardrailBlocked
from litestar_gateway.domain.guardrails import (
    Decision,
    Direction,
    FailPolicy,
    GuardrailPayload,
    GuardrailVerdict,
)
from litestar_gateway.domain.ports.budget_reservation import Reservation, ReservationOutcome

TEAM_ID = uuid4()
KEY_ID = uuid4()


def _model() -> Model:
    return Model(
        id=uuid4(),
        team_id=TEAM_ID,
        name="m",
        provider=Provider.OPENAI,
        credential_id=uuid4(),
        type=ModelType.CHAT,
        provider_model_id="gpt-4o",
        params={},
        api_version=None,
        input_cost_per_token=0.001,
        output_cost_per_token=0.001,
        enabled=True,
        created_at=datetime.now(UTC),
    )


class FakeModels:
    def __init__(self, model: Model) -> None:
        self._model = model

    async def get_by_name(self, team_id: UUID, name: str) -> Model | None:
        return self._model if name == self._model.name else None


class FakeCredentials:
    async def get_values(self, credential_id: UUID) -> dict[str, str] | None:
        return {"api_key": "sk-x"}  # pragma: allowlist secret


class FakeUsage:
    def __init__(self) -> None:
        self.events: list[UsageEvent] = []

    async def record(self, event: UsageEvent) -> None:
        self.events.append(event)

    async def enqueue_pending(self, event: UsageEvent) -> None:  # pragma: no cover
        raise AssertionError("outbox must not be used in these tests")

    async def spend_since(self, team_id: UUID, since: datetime) -> Decimal:
        return sum((e.cost for e in self.events), Decimal(0))


class FakeBudgets:
    """A cap generous enough that admission always succeeds — the tests care
    about whether admission ran at all, not about its verdict."""

    async def get(self, team_id: UUID) -> Budget:
        return Budget(
            id=uuid4(),
            team_id=team_id,
            limit_cost=Decimal("1000"),
            window=BudgetWindow.MONTHLY,
            created_at=datetime.now(UTC),
        )


class SpyingReservations:
    def __init__(self) -> None:
        self.reserved: list[float] = []

    async def try_reserve(
        self, scope: str, amount: float, *, spent: float, limit: float, ttl_s: int
    ) -> ReservationOutcome:
        self.reserved.append(amount)
        return ReservationOutcome(
            reserved=amount,
            reservation=Reservation(id=uuid4(), scope=scope, amount=amount),
        )

    async def release(self, reservation: Reservation) -> None:
        return None


class RecordingGateway:
    """Answers every call, remembering the request it was handed."""

    def __init__(self, answer: str = "the answer") -> None:
        self.requests: list[dict[str, Any]] = []
        self._answer = answer

    async def achat_completion(self, request, model, credentials) -> dict[str, Any]:
        self.requests.append(request)
        return {
            "choices": [{"message": {"role": "assistant", "content": self._answer}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3},
        }


class _Provider:
    """A guardrail that returns a scripted verdict, counting its calls."""

    def __init__(
        self,
        verdict: GuardrailVerdict,
        *,
        directions: tuple[Direction, ...] = (Direction.REQUEST, Direction.RESPONSE),
    ) -> None:
        self.name = verdict.provider
        self._verdict = verdict
        self._directions = directions
        self.seen: list[GuardrailPayload] = []

    def supports(self, direction: Direction) -> bool:
        return direction in self._directions

    async def check(self, payload: GuardrailPayload) -> GuardrailVerdict:
        self.seen.append(payload)
        return self._verdict


def _blocker(name: str = "policy") -> _Provider:
    return _Provider(GuardrailVerdict(decision=Decision.BLOCK, provider=name, categories=("pii",)))


def _redactor(text: str, name: str = "scrubber") -> _Provider:
    return _Provider(GuardrailVerdict(decision=Decision.REDACT, provider=name, redacted_text=text))


def _service(
    gateway: Any,
    usage: FakeUsage,
    traces: list[TraceRecord],
    *,
    request_chain: tuple[ChainedProvider, ...] = (),
    response_chain: tuple[ChainedProvider, ...] = (),
    resolver_calls: list[Direction] | None = None,
) -> CompletionService:
    async def guardrails(
        team_id: UUID, api_key_id: UUID | None, model: Model, direction: Direction
    ) -> tuple[ChainedProvider, ...]:
        assert (team_id, api_key_id) == (TEAM_ID, KEY_ID)
        if resolver_calls is not None:
            resolver_calls.append(direction)
        return request_chain if direction is Direction.REQUEST else response_chain

    return CompletionService(
        models=FakeModels(_model()),  # type: ignore[arg-type]
        credentials=FakeCredentials(),  # type: ignore[arg-type]
        gateway=gateway,  # type: ignore[arg-type]
        meter=UsageMeter(usage=usage, emit_trace=traces.append),  # type: ignore[arg-type]
        guardrails=guardrails,
    )


def _chain(provider: _Provider) -> tuple[ChainedProvider, ...]:
    return (ChainedProvider(provider=provider, fail=FailPolicy.CLOSED),)  # type: ignore[arg-type]


REQUEST = {"model": "m", "messages": [{"role": "user", "content": "my ssn is 1234"}]}


# ── Off by default ────────────────────────────────────────────────────────────


async def test_no_resolver_leaves_the_call_path_untouched() -> None:
    # The default for every existing tenant: no resolver at all.
    gateway = RecordingGateway()
    usage, traces = FakeUsage(), []
    service = CompletionService(
        models=FakeModels(_model()),  # type: ignore[arg-type]
        credentials=FakeCredentials(),  # type: ignore[arg-type]
        gateway=gateway,  # type: ignore[arg-type]
        meter=UsageMeter(usage=usage, emit_trace=traces.append),  # type: ignore[arg-type]
    )

    response = await service.chat_completion(TEAM_ID, KEY_ID, dict(REQUEST))

    assert response["choices"][0]["message"]["content"] == "the answer"
    assert gateway.requests[0]["messages"][0]["content"] == "my ssn is 1234"


async def test_empty_chain_is_indistinguishable_from_no_guardrails() -> None:
    # A configured-but-empty chain must be just as free as no resolver: this is
    # what a team with the feature enabled and no providers looks like.
    gateway = RecordingGateway()
    usage, traces, calls = FakeUsage(), [], []
    service = _service(gateway, usage, traces, resolver_calls=calls)

    response = await service.chat_completion(TEAM_ID, KEY_ID, dict(REQUEST))

    assert response["choices"][0]["message"]["content"] == "the answer"
    assert calls == [Direction.REQUEST, Direction.RESPONSE]
    assert [t.status for t in traces] == ["ok"]


# ── Request side: blocked before anything is spent ────────────────────────────


async def test_blocked_request_never_reaches_the_provider() -> None:
    gateway = RecordingGateway()
    usage, traces = FakeUsage(), []
    provider = _blocker()
    service = _service(gateway, usage, traces, request_chain=_chain(provider))

    with pytest.raises(GuardrailBlocked, match="policy"):
        await service.chat_completion(TEAM_ID, KEY_ID, dict(REQUEST))

    assert gateway.requests == []
    # Nothing was billed: the call never happened, so there is nothing to bill.
    assert usage.events == []
    assert provider.seen[0].text == "my ssn is 1234"
    assert provider.seen[0].direction is Direction.REQUEST


async def test_blocked_request_never_reserves_budget() -> None:
    """The placement test for the request hook.

    Move `_guard_request` after `_meter.admit` and this fails: the store records
    a reservation for a call that never happens. It would be given back at
    release, but until then it counts against the team's fleet-wide in-flight
    total — so a caller sending blocked prompts in a loop could squeeze real
    traffic out of the budget gate without ever reaching a provider.
    """
    reservations = SpyingReservations()
    usage, traces = FakeUsage(), []
    service = CompletionService(
        models=FakeModels(_model()),  # type: ignore[arg-type]
        credentials=FakeCredentials(),  # type: ignore[arg-type]
        gateway=RecordingGateway(),  # type: ignore[arg-type]
        meter=UsageMeter(
            usage=usage,  # type: ignore[arg-type]
            emit_trace=traces.append,
            budgets=FakeBudgets(),  # type: ignore[arg-type]
            reservations=reservations,  # type: ignore[arg-type]
        ),
        guardrails=_resolver_for(request_chain=_chain(_blocker())),
    )

    with pytest.raises(GuardrailBlocked):
        await service.chat_completion(TEAM_ID, KEY_ID, dict(REQUEST))

    assert reservations.reserved == []


async def test_redacted_request_is_what_the_provider_receives() -> None:
    gateway = RecordingGateway()
    usage, traces = FakeUsage(), []
    service = _service(
        gateway, usage, traces, request_chain=_chain(_redactor("my ssn is [REDACTED]"))
    )

    await service.chat_completion(TEAM_ID, KEY_ID, dict(REQUEST))

    # The whole point of redacting rather than blocking: the call goes through,
    # without the sensitive span. Nothing downstream ever sees the original.
    assert gateway.requests[0]["messages"][0]["content"] == "my ssn is [REDACTED]"


async def test_redaction_on_an_unrewritable_request_escalates_to_a_block() -> None:
    # Fail closed: a redaction we cannot apply exactly must not be silently
    # skipped, because that would send the original.
    gateway = RecordingGateway()
    usage, traces = FakeUsage(), []
    service = _service(gateway, usage, traces, request_chain=_chain(_redactor("[REDACTED]")))
    multimodal = {
        "model": "m",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "my ssn is 1234"}]}],
    }

    with pytest.raises(GuardrailBlocked, match="cannot be rewritten"):
        await service.chat_completion(TEAM_ID, KEY_ID, multimodal)

    assert gateway.requests == []


# ── Response side: blocked, but billed ────────────────────────────────────────


async def test_blocked_response_is_withheld_but_still_billed() -> None:
    gateway = RecordingGateway(answer="here is the leak")
    usage, traces = FakeUsage(), []
    provider = _blocker("leak-detector")
    service = _service(gateway, usage, traces, response_chain=_chain(provider))

    with pytest.raises(GuardrailBlocked, match="leak-detector"):
        await service.chat_completion(TEAM_ID, KEY_ID, dict(REQUEST))

    # The provider call happened and the tokens were really consumed. Refusing
    # to bill would make the response guardrail a free channel for anyone who
    # can trip it deliberately.
    assert len(usage.events) == 1
    assert (usage.events[0].prompt_tokens, usage.events[0].completion_tokens) == (2, 3)
    # The trace stays 'ok': it records what the provider did and what was
    # billed. The refusal is a guardrail event, not a failed call.
    assert [t.status for t in traces] == ["ok"]
    assert provider.seen[0].text == "here is the leak"
    assert provider.seen[0].direction is Direction.RESPONSE


async def test_redacted_response_is_what_the_caller_receives() -> None:
    gateway = RecordingGateway(answer="the account is 1234")
    usage, traces = FakeUsage(), []
    service = _service(
        gateway, usage, traces, response_chain=_chain(_redactor("the account is [REDACTED]"))
    )

    response = await service.chat_completion(TEAM_ID, KEY_ID, dict(REQUEST))

    assert response["choices"][0]["message"]["content"] == "the account is [REDACTED]"
    assert len(usage.events) == 1  # billed normally


async def test_request_only_provider_is_never_asked_about_the_response() -> None:
    gateway = RecordingGateway()
    usage, traces = FakeUsage(), []
    provider = _Provider(
        GuardrailVerdict(decision=Decision.ALLOW, provider="req-only"),
        directions=(Direction.REQUEST,),
    )
    service = _service(
        gateway, usage, traces, request_chain=_chain(provider), response_chain=_chain(provider)
    )

    await service.chat_completion(TEAM_ID, KEY_ID, dict(REQUEST))

    assert [p.direction for p in provider.seen] == [Direction.REQUEST]


def _resolver_for(
    *,
    request_chain: tuple[ChainedProvider, ...] = (),
    response_chain: tuple[ChainedProvider, ...] = (),
) -> Any:
    async def guardrails(
        team_id: UUID, api_key_id: UUID | None, model: Model, direction: Direction
    ) -> tuple[ChainedProvider, ...]:
        return request_chain if direction is Direction.REQUEST else response_chain

    return guardrails
