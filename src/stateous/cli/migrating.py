"""`stateous migrate`: make or update the tables of a workflow's database, when a
deploy says so. A control plane that a developer embeds never migrates by itself."""

from __future__ import annotations

import typer

from stateous.cli.errors import exit_with
from stateous.cli.serving import one_line, shown_db
from stateous.control.store import Store
from stateous.control.store.migrations import head


def migrate(db: str, namespace: str | None, notices: bool | None) -> None:
    """The body of `stateous migrate`. `notices` None keeps the setting."""
    try:
        store = Store(db, namespace=namespace)
    except Exception as error:  # a bad namespace, a driver, a connection: one sentence
        exit_with(one_line(error))
    try:
        if notices is not None:
            store.migrate(notices=notices)
        where = "" if store.namespace is None else f" in namespace '{store.namespace}'"
        state = "on" if store.notices_on else "off"
    except Exception as error:
        exit_with(one_line(error))
    finally:
        store.close()
    typer.echo(f"migrated {shown_db(db)}{where} to {head()}; notices {state}")
