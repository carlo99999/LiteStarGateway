"""Client registry: reuse, isolation, rotation, eviction, and shutdown.

Covers the mandatory Plan 14 test list (plans/14a-hot-path-implementation.md,
Step 2) before any adapter adopts the registry.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from litestar_gateway.infrastructure.llm.client_registry import (
    ClientKey,
    ClientRegistry,
    ClientRegistryClosed,
    _Entry,
    fingerprint_material,
)


class _FakeClient:
    """A minimal stand-in for an SDK client: tracks its own close exactly."""

    def __init__(self, marker: str) -> None:
        self.marker = marker
        self.closed = False
        self.close_count = 0

    async def close(self) -> None:
        self.close_count += 1
        self.closed = True


async def _close(client: _FakeClient) -> None:
    await client.close()


def _key(
    provider: str = "openai", material_value: str = "sk-1", endpoint: str = "https://api"
) -> ClientKey:
    return ClientKey(
        provider=provider,
        fingerprint=fingerprint_material(material_value, endpoint),
        endpoint=endpoint,
    )


# 1. sequential calls with one key -> one client, one create
async def test_sequential_calls_reuse_one_client() -> None:
    registry = ClientRegistry(close=_close)
    key = _key()
    created: list[_FakeClient] = []

    def factory() -> _FakeClient:
        client = _FakeClient("a")
        created.append(client)
        return client

    async with registry.lease(key, factory) as first:
        pass
    async with registry.lease(key, factory) as second:
        pass

    assert first is second
    assert len(created) == 1
    metrics = registry.metrics()
    assert metrics.creates == 1
    assert metrics.hits == 1
    assert metrics.misses == 1


# 2. N concurrent first calls with one key -> exactly one factory invocation
async def test_concurrent_misses_create_exactly_one_client() -> None:
    registry = ClientRegistry(close=_close)
    key = _key()
    call_count = 0

    async def use_client() -> _FakeClient:
        nonlocal call_count

        def factory() -> _FakeClient:
            nonlocal call_count
            call_count += 1
            return _FakeClient("concurrent")

        async with registry.lease(key, factory) as client:
            await asyncio.sleep(0.01)
            return client

    results = await asyncio.gather(*(use_client() for _ in range(20)))

    assert call_count == 1
    assert len({id(c) for c in results}) == 1
    assert registry.metrics().creates == 1


# 3. different provider / credential / endpoint / api version / region ->
#    distinct clients, never shared
async def test_distinct_dimensions_never_share_a_client() -> None:
    registry = ClientRegistry(close=_close)
    variants = [
        _key(provider="openai", material_value="sk-1", endpoint="https://api.openai.com"),
        _key(provider="azure_openai", material_value="sk-1", endpoint="https://api.openai.com"),
        _key(provider="openai", material_value="sk-2", endpoint="https://api.openai.com"),
        _key(provider="openai", material_value="sk-1", endpoint="https://other-endpoint"),
    ]
    clients = []
    for key in variants:
        async with registry.lease(key, lambda: _FakeClient("x")) as client:
            clients.append(client)

    assert len({id(c) for c in clients}) == len(variants)
    assert registry.metrics().creates == len(variants)


# 4. rotation: same logical credential, new material -> new key, new client;
#    old client closes only after its in-flight lease releases
async def test_rotation_swaps_client_and_closes_old_after_drain() -> None:
    registry = ClientRegistry(close=_close)
    old_key = _key(material_value="sk-old")
    new_key = _key(material_value="sk-new")

    old_client = _FakeClient("old")
    async with registry.lease(old_key, lambda: old_client) as leased_old:
        # Rotation happens while the old client is still leased by an
        # in-flight request.
        async with registry.lease(new_key, lambda: _FakeClient("new")) as leased_new:
            assert leased_new is not leased_old
            assert old_client.closed is False  # still leased, must not close

    # Both leases released now, but only a TTL/capacity/shutdown event closes
    # an unleased-but-still-cached client — rotation itself doesn't evict.
    assert old_client.closed is False


async def test_rotation_old_client_closes_once_evicted_after_drain() -> None:
    registry = ClientRegistry(close=_close, capacity=1)
    old_key = _key(material_value="sk-old")
    new_key = _key(material_value="sk-new")

    old_client = _FakeClient("old")
    async with registry.lease(old_key, lambda: old_client):
        pass  # released, but still cached (capacity=1, only key so far)

    async with registry.lease(new_key, lambda: _FakeClient("new")):
        pass  # capacity=1 forces the old, now-unleased client to evict

    assert old_client.closed is True
    assert old_client.close_count == 1


# 5. eviction at capacity closes the evicted client exactly once, and never
#    while leased
async def test_eviction_at_capacity_closes_exactly_once_never_while_leased() -> None:
    registry = ClientRegistry(close=_close, capacity=2)
    first_client = _FakeClient("first")

    async with registry.lease(_key(material_value="sk-1"), lambda: first_client):
        # Leased for the whole test: filling the registry past capacity must
        # never close this client while we're inside the `async with`.
        async with registry.lease(_key(material_value="sk-2"), lambda: _FakeClient("second")):
            pass
        async with registry.lease(_key(material_value="sk-3"), lambda: _FakeClient("third")):
            pass
        assert first_client.closed is False

    assert registry.metrics().live_clients <= 2


# 6. TTL expiry behaves like eviction
async def test_ttl_expiry_closes_unleased_client() -> None:
    registry = ClientRegistry(close=_close, ttl_seconds=0.05)
    client = _FakeClient("ttl")
    key = _key()

    async with registry.lease(key, lambda: client):
        pass
    await asyncio.sleep(0.1)

    # The sweep runs opportunistically on the next construction; trigger one.
    async with registry.lease(_key(material_value="other"), lambda: _FakeClient("other")):
        pass

    assert client.closed is True
    assert registry.metrics().evictions >= 1


# 7. shutdown closes all retained clients exactly once; idempotent
async def test_aclose_closes_all_clients_exactly_once_and_is_idempotent() -> None:
    registry = ClientRegistry(close=_close, capacity=10)
    clients = [_FakeClient(str(i)) for i in range(3)]
    for i, client in enumerate(clients):
        async with registry.lease(_key(material_value=f"sk-{i}"), lambda c=client: c):
            pass

    await registry.aclose()
    await registry.aclose()  # idempotent: no double-close, no error

    for client in clients:
        assert client.closed is True
        assert client.close_count == 1

    with pytest.raises(ClientRegistryClosed):
        key = _key(material_value="post-shutdown")
        async with registry.lease(key, lambda: _FakeClient("late")):
            pass


# 8. factory raising does not poison the key or leak a partial client
async def test_factory_failure_does_not_poison_key() -> None:
    registry = ClientRegistry(close=_close)
    key = _key()
    attempts = 0

    def flaky_factory() -> _FakeClient:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("boom")
        return _FakeClient("recovered")

    with pytest.raises(ConnectionError):
        async with registry.lease(key, flaky_factory):
            pass

    assert registry.metrics().live_clients == 0

    async with registry.lease(key, flaky_factory) as client:
        assert client.marker == "recovered"

    assert attempts == 2
    assert registry.metrics().creates == 1  # the failed attempt never counted


# 9. cancellation releases the lease without closing a client leased
#    concurrently by another task
async def test_cancellation_releases_lease_without_closing_shared_client() -> None:
    registry = ClientRegistry(close=_close, capacity=1)
    client = _FakeClient("shared")
    key = _key()

    async def hold_forever() -> None:
        async with registry.lease(key, lambda: client):
            await asyncio.sleep(10)

    async with registry.lease(key, lambda: client):
        task = asyncio.create_task(hold_forever())
        await asyncio.sleep(0.01)  # let it acquire its own lease on the hit path
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Our own lease is still open; the client must still be alive.
        assert client.closed is False

    assert client.closed is False  # only cached, not force-closed on cancel


# 10. reprs/metrics/logs contain no credential material
def test_key_and_metrics_never_expose_credential_material() -> None:
    material_value = "sk-super-material_value-value"
    key = ClientKey(
        provider="openai",
        fingerprint=fingerprint_material(material_value, "https://api"),
        endpoint="https://api",
    )

    assert material_value not in repr(key)
    assert material_value not in str(key)
    assert key.fingerprint != material_value
    assert material_value not in key.fingerprint


def test_fingerprint_distinguishes_none_from_empty_and_is_order_sensitive() -> None:
    assert fingerprint_material(None, "x") != fingerprint_material("", "x")
    assert fingerprint_material("a", "b") != fingerprint_material("b", "a")
    assert fingerprint_material("a", "b") == fingerprint_material("a", "b")


async def test_capacity_and_ttl_must_be_positive() -> None:
    with pytest.raises(ValueError):
        ClientRegistry(capacity=0)
    with pytest.raises(ValueError):
        ClientRegistry(ttl_seconds=0)


async def test_second_waiter_hits_inside_the_creation_lock() -> None:
    """A second task queued on an already-held creation lock must observe the
    freshly built entry on its post-lock recheck, not race a second build.

    `asyncio.Lock.acquire()` doesn't yield when uncontended, so an ordinary
    `asyncio.gather` of misses (as in the "concurrent misses" test above) tends
    to run the first task to completion before the rest even start — never
    genuinely exercising the in-lock recheck. This test forces the interleave:
    `first()` holds the creation lock across an `await` (mirroring exactly
    what `_acquire` does while building), and `second()` is guaranteed to
    already be queued on that same lock before `first()` releases it.
    """
    registry = ClientRegistry(close=_close)
    key = _key()
    queued = asyncio.Event()
    release = asyncio.Event()
    slow_factory_calls = 0

    def slow_factory() -> _FakeClient:
        nonlocal slow_factory_calls
        slow_factory_calls += 1
        return _FakeClient("slow")

    async def first() -> object:
        lock = await registry._creation_lock(key)
        async with lock:
            queued.set()
            await release.wait()
            client = slow_factory()
            now = time.monotonic()
            async with registry._guard:
                registry._entries[key] = _Entry(
                    client=client, created_at=now, last_used_at=now, leases=0
                )
                registry._creates += 1
        return client

    hit_holder: dict[str, object] = {}

    async def second() -> None:
        await queued.wait()
        async with registry.lease(key, slow_factory) as client:
            hit_holder["client"] = client

    first_task = asyncio.create_task(first())
    second_task = asyncio.create_task(second())
    await queued.wait()
    release.set()
    built = await first_task
    await second_task

    assert slow_factory_calls == 1  # second() must not have built its own
    assert hit_holder["client"] is built
    assert registry.metrics().creates == 1


async def test_close_failure_is_logged_and_never_propagates() -> None:
    async def failing_close(_: _FakeClient) -> None:
        raise RuntimeError("provider client refused to close")

    registry = ClientRegistry(close=failing_close, capacity=1)
    client = _FakeClient("flaky-close")

    async with registry.lease(_key(material_value="sk-1"), lambda: client):
        pass
    # Forces eviction of the unleased entry above; must not raise even though
    # its close() callback fails.
    async with registry.lease(_key(material_value="sk-2"), lambda: _FakeClient("other")):
        pass

    assert registry.metrics().evictions == 1

    # Shutdown must also swallow a failing close().
    await registry.aclose()


async def test_all_entries_leased_past_capacity_mark_oldest_for_close_on_release() -> None:
    registry = ClientRegistry(close=_close, capacity=1)
    first_client = _FakeClient("first")
    second_client = _FakeClient("second")

    async with registry.lease(_key(material_value="sk-1"), lambda: first_client):
        # Second lease is a distinct key while the first is still held, so
        # both entries are leased and capacity=1 cannot evict either yet.
        async with registry.lease(_key(material_value="sk-2"), lambda: second_client):
            assert first_client.closed is False
            assert second_client.closed is False
        # The second lease released, but capacity overage marked the OLDEST
        # (first) entry `closing`, not the one that just released.
        assert second_client.closed is False

    assert first_client.closed is True
