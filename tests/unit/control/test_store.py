"""The store: the ledger and the rows it explains, one transaction at a time."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from stateous.control.store import Store, tables
from stateous.control.store.store import database_url, utc

NOW = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)


def add_token(store: Store, token: str, status: str = "active") -> None:
    with store.transaction(NOW) as tx:
        tx.add_token(
            tables.Token(
                token=token, state="coding", status=status, visit=1, context={},
                entered_at=utc(NOW),
            )
        )  # fmt: skip


def test_ledger_entries_are_numbered_per_token() -> None:
    store = Store("sqlite://")
    # One transaction for each token: a transaction waits for one token lock only.
    with store.transaction(NOW) as tx:
        first = tx.append("T-1", "started", context={})
        second = tx.append("T-1", "entered", state="coding", visit=1)
    with store.transaction(NOW) as tx:
        other = tx.append("T-2", "started", context={})

    assert (first, second, other) == (1, 2, 1)
    with store.transaction(NOW) as tx:
        found = [(e.seq, e.kind, e.data, e.at) for e in tx.entries("T-1")]
    assert found == [
        (1, "started", {"context": {}}, utc(NOW)),
        (2, "entered", {"state": "coding", "visit": 1}, utc(NOW)),
    ]


def test_a_transaction_is_all_or_nothing() -> None:
    store = Store("sqlite://")

    with pytest.raises(RuntimeError), store.transaction(NOW) as tx:
        tx.append("T-1", "started", context={})
        raise RuntimeError("crash halfway")

    with store.transaction(NOW) as tx:
        assert tx.entries("T-1") == []


def test_an_idempotency_key_finds_its_entry_on_its_own_token_only() -> None:
    store = Store("sqlite://")
    with store.transaction(NOW) as tx:
        tx.append("T-1", "event_accepted", key="delivery-7", event="done", payload=None)

    with store.transaction(NOW) as tx:
        found = tx.find_key("T-1", "delivery-7")
        assert found is not None and found.data["event"] == "done"
        assert tx.find_key("T-2", "delivery-7") is None


def test_counts_visits_and_the_last_accepted_event_come_from_the_ledger() -> None:
    store = Store("sqlite://")
    with store.transaction(NOW) as tx:
        tx.append("T-1", "entered", state="coding", visit=1)
        tx.append("T-1", "event_accepted", event="done", payload=None, state="coding")
        tx.append("T-1", "entered", state="testing", visit=1)
        tx.append("T-1", "event_accepted", event="failed", payload={"failed": 1}, state="testing")
        tx.append("T-1", "entered", state="coding", visit=2)

    with store.transaction(NOW) as tx:
        assert tx.counts("T-1") == {"done": 1, "failed": 1}
        assert tx.visits("T-1", "coding") == 2
        last = tx.last_accepted("T-1")
        assert last is not None and last.data["payload"] == {"failed": 1}


def test_a_claim_takes_the_oldest_queued_task_of_an_active_token() -> None:
    store = Store("sqlite://")
    for token, status in (("T-1", "paused"), ("T-2", "active"), ("T-3", "active")):
        add_token(store, token, status)
        with store.transaction(NOW) as tx:
            tx.queue_task(token, "coding", 1)

    with store.transaction(NOW) as tx:
        task = tx.claim_task("box-1", ["coding"])
        assert task is not None
        assert (task.token, task.status, task.claimed_by, task.attempt) == (
            "T-2", "claimed", "box-1", 1,
        )  # fmt: skip
    with store.transaction(NOW) as tx:
        assert tx.claim_task("box-1", ["testing"]) is None


def test_renewing_leases_names_only_the_tasks_the_supervisor_still_holds() -> None:
    store = Store("sqlite://")
    held: list[int] = []
    for token, supervisor in (("T-1", "box-1"), ("T-2", "box-1"), ("T-3", "box-2")):
        add_token(store, token)
        with store.transaction(NOW) as tx:
            tx.queue_task(token, "coding", 1)
        with store.transaction(NOW) as tx:
            task = tx.claim_task(supervisor, ["coding"])
            assert task is not None
            held.append(task.id)
    until = utc(NOW + timedelta(minutes=2))

    with store.transaction(NOW) as tx:
        renewed = tx.renew_leases("box-1", held, until)

    assert renewed == {held[0], held[1]}
    with store.transaction(NOW) as tx:
        task = tx.task(held[0])
        assert task is not None and task.lease_until == until


def test_renewing_leases_does_not_revive_one_whose_lease_already_ended() -> None:
    store = Store("sqlite://")
    add_token(store, "T-1")
    with store.transaction(NOW) as tx:
        tx.queue_task("T-1", "coding", 1)
    with store.transaction(NOW) as tx:
        task = tx.claim_task("box-1", ["coding"])
        assert task is not None
        task.lease_until = utc(NOW + timedelta(minutes=2))
        task_id = task.id

    past_lease = NOW + timedelta(minutes=10)  # well after the lease ended
    with store.transaction(past_lease) as tx:
        renewed = tx.renew_leases("box-1", [task_id], utc(past_lease + timedelta(minutes=2)))

    assert renewed == set()
    with store.transaction(NOW) as tx:
        task = tx.task(task_id)
        assert task is not None and task.lease_until == utc(NOW + timedelta(minutes=2))


def test_memory_is_kept_per_token_and_state_and_deleted_with_the_token() -> None:
    store = Store("sqlite://")
    with store.transaction(NOW) as tx:
        tx.set_memory("T-1", "coding", "dev", "s-1")
        tx.set_memory("T-1", "testing", "dev", "s-2")
        tx.set_memory("T-2", "coding", "dev", {"id": "s-3"})
        tx.set_memory("T-1", "coding", "dev", "s-4")

    with store.transaction(NOW) as tx:
        assert tx.memory("T-1", "coding") == {"dev": "s-4"}
        assert tx.memory("T-1", "testing") == {"dev": "s-2"}
    with store.transaction(NOW) as tx:
        assert tx.forget("T-1", "coding", "dev")
        assert not tx.forget("T-1", "coding", "dev")
        tx.delete_memory("T-1")
    with store.transaction(NOW) as tx:
        assert tx.memory("T-1", "testing") == {}
        assert tx.memory("T-2", "coding") == {"dev": {"id": "s-3"}}


def test_due_timers_are_those_of_active_tokens_whose_time_came() -> None:
    store = Store("sqlite://")
    add_token(store, "T-1")
    add_token(store, "T-2", "paused")
    with store.transaction(NOW) as tx:
        due = tx.add_timer("T-1", "coding", 1, "stuck", None, utc(NOW), kind="timer")
        later = utc(NOW + timedelta(minutes=1))
        tx.add_timer("T-1", "coding", 1, "stuck", None, later, kind="timer")
        tx.add_timer("T-2", "coding", 1, "stuck", None, utc(NOW), kind="timer")
        due_id = due.id

    with store.transaction(NOW) as tx:
        assert tx.due_timers() == [due_id]


def test_a_new_database_has_one_empty_control_plane_row() -> None:
    store = Store("sqlite://")

    with store.transaction(NOW) as tx:
        row = tx.control_plane()
        assert (row.id, row.structure, row.checksum, row.serving, row.since) == (
            1, None, None, None, None,
        )  # fmt: skip


@pytest.mark.parametrize(
    ("given", "used"),
    [
        ("postgres://u:p@db/flows", "postgresql+psycopg://u:p@db/flows"),
        ("postgresql://u:p@db/flows", "postgresql+psycopg://u:p@db/flows"),
        ("postgresql+psycopg://u@db/flows", "postgresql+psycopg://u@db/flows"),
        ("sqlite:///stateous.db", "sqlite:///stateous.db"),
    ],
)
def test_postgres_urls_as_people_write_them_are_understood(given: str, used: str) -> None:
    assert database_url(given) == used


def test_a_closed_store_lets_go_of_its_connections() -> None:
    store = Store("sqlite://")
    with store.transaction(NOW) as tx:
        tx.append("T-1", "started", context={})
    pool = store.engine.pool

    store.close()

    assert store.engine.pool is not pool


def test_a_database_this_version_made_opens_again(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'new.db').as_posix()}"
    Store(url).close()

    Store(url).close()


def test_a_lease_ends_only_if_it_is_still_over_when_it_is_ended() -> None:
    store = Store("sqlite://")
    add_token(store, "T-1")
    with store.transaction(NOW) as tx:
        lapsed, renewed = tx.queue_task("T-1", "coding", 1), tx.queue_task("T-1", "coding", 1)
        for task, until in ((lapsed, NOW - timedelta(seconds=1)), (renewed, NOW)):
            task.status, task.claimed_by, task.lease_until = "claimed", "box-1", utc(until)
        ids = lapsed.id, renewed.id

    with store.transaction(NOW) as tx:
        assert (tx.end_lease(ids[0]), tx.end_lease(ids[1])) == (True, False)
    with store.transaction(NOW) as tx:
        leases = [task.lease_until for task in map(tx.task, ids) if task is not None]
    assert leases == [None, utc(NOW)]
