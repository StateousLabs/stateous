"""The connection to the test server: all that a test sends gets to the server.

On Windows, `localhost` is `::1` first. Docker Desktop has a different relay
(`wslrelay`) on `::1` than on `127.0.0.1`, and on `::1` the bytes of a pipelined
`executemany` sometimes do not get to Postgres: the server waits for the rest of the
message (ClientRead) and the client waits for the answer, with no time limit. These
tests send the same statements as the test suite many times, and they fail in a
fixed time instead of waiting for ever."""

import threading

from sqlalchemy import create_engine, text

from stateous.control.store.store import database_url

from ..conftest import fresh_postgres_database, needs_postgres

pytestmark = needs_postgres

ROUNDS = 300
"""Over the `::1` relay, the first stop came within 25 rounds in each try."""

LIMIT = 60.0
"""Seconds for all the rounds. Over `127.0.0.1` they take less than 5."""


def test_many_pipelined_writes_and_long_queries_get_to_the_server() -> None:
    engine = create_engine(database_url(fresh_postgres_database()))
    finished = threading.Event()
    states = ["coding"] * 1000
    query = text(
        "SELECT id FROM tasks WHERE state IN ("
        + ", ".join(f":s{i}" for i in range(len(states)))
        + ") LIMIT 20"
    )
    names = {f"s{i}": name for i, name in enumerate(states)}
    errors: list[BaseException] = []

    def rounds() -> None:
        try:
            send_rounds()
        except BaseException as error:  # the test reports it below
            errors.append(error)

    def send_rounds() -> None:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE tasks (id int PRIMARY KEY, state text, n int)"))
            connection.execute(
                text("INSERT INTO tasks SELECT g, 'coding', 0 FROM generate_series(1, 3) g")
            )
        for _ in range(ROUNDS):
            with engine.begin() as connection:
                connection.execute(query, names).all()  # one statement, 1000 parameters
                connection.execute(  # executemany: psycopg sends it as a pipeline
                    text("UPDATE tasks SET n = n + 1 WHERE id = :id"),
                    [{"id": 1}, {"id": 2}, {"id": 3}],
                )
        finished.set()

    # A daemon thread: if it waits for ever, the test fails and the run goes on. The
    # session's teardown drops the database, and that ends the waiting backend.
    worker = threading.Thread(target=rounds, daemon=True)
    worker.start()
    worker.join(timeout=LIMIT)

    assert not errors, errors
    assert finished.is_set(), (
        f"{ROUNDS} rounds did not end in {LIMIT:.0f} s: the connection to the test "
        "server stopped (see the module docstring)"
    )
    engine.dispose()
