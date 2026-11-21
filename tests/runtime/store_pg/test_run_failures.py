"""Terminal failures, replay, the dead-thread sweep and redelivery behave
identically on both database backends."""
from datetime import timedelta

from handler.runtime.store.deliveries import DEAD, PENDING
from handler.runtime.store.run_failures import FAILED_BEFORE_UPGRADE
from handler.runtime.types import Event, Lane, Run, RunStatus, Schedule, ThreadSpec, ThreadType

SPEC = ThreadSpec("automation:s1", ThreadType.AUTOMATION, Lane.BACKGROUND,
                  owner_user_id="u", harness="openai_agents")


def fired(key, clock):
    return Event("automation.fired", "test", {"name": "job"}, key,
                 thread_key=SPEC.key, user_id="u", received_at=clock())


async def fail_for_good(store, clock, run_id="run_1", *, also=()):
    """A run takes the pending events and fails with no retry left."""
    await store.put(fired("e1", clock), SPEC)
    (lease,) = await store.claim("w", Lane.BACKGROUND, 1, 60)
    taken = [e.id for e in await store.pending_events(SPEC.key)]
    run = Run(run_id, SPEC.key, lease.version, taken, RunStatus.RUNNING, "openai_agents", None,
              started_at=clock())
    assert await store.create_run(lease, run)
    for key in also:  # arrive while the run is going
        await store.put(fired(key, clock), SPEC)
    run.status, run.error, run.finished_at = RunStatus.FAILED, "HarnessError: down", clock()
    assert await store.finish_run(lease, run)
    assert await store.fail(lease, run.error, None, run_id=run_id, event_ids=taken)
    return taken


async def test_terminal_failure_consumes_its_events_and_keeps_the_thread_open(
        domain_store, clock):
    store = domain_store
    taken = await fail_for_good(store, clock, also=("e2",))
    thread = await store.get_thread(SPEC.key)
    assert not thread.dead and thread.attempts == 0 and thread.available_at is None
    assert thread.last_error == "HarnessError: down"
    # Only the event that arrived mid-run is left, and it runs at once.
    assert [e.idempotency_key for e in await store.pending_events(SPEC.key)] == ["e2"]
    assert thread.pending_since is not None
    (lease,) = await store.claim("w", Lane.BACKGROUND, 1, 60)
    pending = [e.id for e in await store.pending_events(SPEC.key)]
    assert await store.complete(lease, "run_2", pending)
    assert (await store.get_thread(SPEC.key)).last_error is None
    (row,) = await store.failed_runs()
    assert row["run_id"] == "run_1" and row["thread_key"] == SPEC.key
    assert row["thread_type"] == "automation" and row["attempts"] == 1
    assert row["event_ids"] == taken and row["error"] == "HarnessError: down"
    assert row["failed_at"] == clock() and row["replayed_at"] is None
    # A scheduled firing on the thread runs normally too.
    schedule = Schedule(name="job", prompt="go", spec="every 1h", next_run_at=clock())
    await store.create_schedule(schedule)
    thread_spec = ThreadSpec(SPEC.key, SPEC.type, SPEC.lane, harness="openai_agents")
    assert await store.fire_schedule(schedule, schedule.next_run_at,
                                     clock() + timedelta(hours=1), fired("e3", clock),
                                     thread_spec)
    assert len(await store.claim("w", Lane.BACKGROUND, 1, 60)) == 1


async def test_retryable_failure_keeps_its_events(domain_store, clock):
    store = domain_store
    await store.put(fired("e1", clock), SPEC)
    (lease,) = await store.claim("w", Lane.BACKGROUND, 1, 60)
    assert await store.fail(lease, "try again", 10, run_id="run_1",
                            event_ids=[e.id for e in await store.pending_events(SPEC.key)])
    thread = await store.get_thread(SPEC.key)
    assert thread.attempts == 1 and thread.available_at is not None and not thread.dead
    assert len(await store.pending_events(SPEC.key)) == 1
    assert await store.failed_runs() == []


async def test_replay_runs_the_events_once(domain_store, clock):
    store = domain_store
    taken = await fail_for_good(store, clock)
    assert await store.claim("w", Lane.BACKGROUND, 1, 60) == []  # nothing left to run
    result = await store.replay_failed_run("run_1", by="root")
    assert result["replayed"] and result["requeued"] == 1 and result["replayed_by"] == "root"
    assert [e.id for e in await store.pending_events(SPEC.key)] == taken
    (lease,) = await store.claim("w", Lane.BACKGROUND, 1, 60)
    assert await store.complete(lease, "run_2", taken)
    again = await store.replay_failed_run("run_1")
    assert again["replayed"] is False and again["replayed_at"] == clock()
    assert await store.pending_events(SPEC.key) == []
    assert await store.replay_failed_run("run_missing") is None
    (row,) = await store.failed_runs()
    assert row["replayed_at"] == clock() and row["replayed_by"] == "root"


async def test_failed_runs_page_newest_first(domain_store, clock):
    store = domain_store
    for index in range(3):
        await store.put(fired(f"e{index}", clock), SPEC)
        (lease,) = await store.claim("w", Lane.BACKGROUND, 1, 60)
        assert await store.fail(lease, f"boom {index}", None, run_id=f"run_{index}",
                                event_ids=[e.id for e in await store.pending_events(SPEC.key)])
        clock.advance(1)
    first = await store.failed_runs(limit=2)
    assert [r["run_id"] for r in first] == ["run_2", "run_1"]
    rest = await store.failed_runs(limit=2, before=(first[-1]["failed_at"], first[-1]["run_id"]))
    assert [r["run_id"] for r in rest] == ["run_0"]
    assert (await store.get_run_failure("run_0"))["error"] == "boom 0"


async def _park_the_old_way(store, clock):
    """What an older release left: a failed run, dead = TRUE, and work queued after it."""
    await store.put(fired("e1", clock), SPEC)
    (lease,) = await store.claim("w", Lane.BACKGROUND, 1, 60)
    run = Run("run_old", SPEC.key, lease.version, [], RunStatus.RUNNING, "openai_agents", None,
              started_at=clock())
    assert await store.create_run(lease, run)
    run.status, run.error, run.finished_at = RunStatus.FAILED, "old failure", clock()
    assert await store.finish_run(lease, run)
    assert await store.fail(lease, "old failure", 10)  # releases the lease
    await store._thread_query("UPDATE threads SET dead = TRUE, attempts = 3 WHERE key = ? "
                              "RETURNING key", (SPEC.key,))
    await store.put(fired("e2", clock), SPEC)
    assert await store.claim("w", Lane.BACKGROUND, 1, 60) == []  # parked


async def test_sweep_opens_old_dead_threads_without_running_their_work(domain_store, clock):
    store = domain_store
    await _park_the_old_way(store, clock)
    queued = [e.id for e in await store.pending_events(SPEC.key)]
    clock.advance(60)
    (row,) = await store.sweep_dead_threads()
    assert row["thread_key"] == SPEC.key and row["run_id"] == "run_old"
    assert row["event_ids"] == queued and row["attempts"] == 3
    assert row["error"] == "old failure" and row["consumed_by"] == FAILED_BEFORE_UPGRADE
    thread = await store.get_thread(SPEC.key)
    assert not thread.dead and thread.pending_since is None and thread.attempts == 0
    assert await store.pending_events(SPEC.key) == []
    assert await store.claim("w", Lane.BACKGROUND, 1, 60) == []  # stale work stays put
    assert await store.sweep_dead_threads() == []  # idempotent
    (listed,) = await store.failed_runs()
    assert listed["run_id"] == "run_old" and listed["event_ids"] == queued
    # New work runs normally; the swept work runs only when replayed.
    await store.put(fired("e3", clock), SPEC)
    (lease,) = await store.claim("w", Lane.BACKGROUND, 1, 60)
    assert [e.idempotency_key for e in await store.pending_events(SPEC.key)] == ["e3"]
    assert await store.complete(lease, "run_new", [e.id for e in
                                                   await store.pending_events(SPEC.key)])
    result = await store.replay_failed_run("run_old")
    assert result["replayed"] and result["requeued"] == 2
    assert [e.id for e in await store.pending_events(SPEC.key)] == queued
    assert len(await store.claim("w", Lane.BACKGROUND, 1, 60)) == 1


async def test_redeliver_one_or_all_dead_rows(domain_store):
    store = domain_store
    for key in ("k1", "k2", "k3"):
        await store.enqueue_delivery("a", key, {"n": key})
    for key in ("k1", "k2"):
        await store.delivery_failed("a", key, "HTTP 500", None)
    await store.enqueue_delivery("b", "k9", {})
    await store.delivery_failed("b", "k9", "x", None)
    assert await store.redeliver("a", "k1") == 1
    row = await store.get_delivery("a", "k1")
    assert row.status == PENDING and row.attempts == 0
    assert await store.redeliver("a", "k3") == 0  # pending, not dead
    assert await store.redeliver("a") == 1
    assert {d.key for d in await store.claim_deliveries(["a"])} == {"k1", "k2", "k3"}
    assert (await store.get_delivery("b", "k9")).status == DEAD
