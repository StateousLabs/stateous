"""The migration runner: the baseline revision and the ones after it, kept in
`stateous_version`."""

import shutil
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import OperationalError

from stateous.control.store import ForeignDatabase, Store, migrations, tables
from stateous.control.store.migrations import (
    BASELINE,
    VERSION_TABLE,
    head,
    migrate,
    upgrade_from_empty,
)
from stateous.control.store.store import open_engine


def sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def version_of(url: str) -> str | None:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"version_table": VERSION_TABLE})
            return context.get_current_revision()
    finally:
        engine.dispose()


def table_names(url: str) -> list[str]:
    engine = create_engine(url)
    try:
        return sorted(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def token_columns(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return {column["name"] for column in inspect(engine).get_columns("tokens")}
    finally:
        engine.dispose()


def control_plane_ids(url: str) -> list[int]:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            return list(connection.execute(text("SELECT id FROM control_plane")).scalars())
    finally:
        engine.dispose()


SECOND_REVISION = '''"""A test revision: tokens get a note."""

import sqlalchemy as sa
from alembic import op

revision = "9990"
down_revision = "{down}"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tokens") as batch_op:
        batch_op.add_column(sa.Column("note", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("tokens") as batch_op:
        batch_op.drop_column("note")
'''


FAILING_SECOND_REVISION = '''"""A test revision that fails after it changed the schema."""

import sqlalchemy as sa
from alembic import op

revision = "9990"
down_revision = "{down}"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tokens", recreate="always") as batch_op:
        batch_op.add_column(sa.Column("note", sa.String(length=64), nullable=True))
    op.create_table("extra", sa.Column("id", sa.Integer(), primary_key=True))
    op.execute("SELECT * FROM no_such_table")


def downgrade() -> None:
    pass
'''


def with_a_second_revision(
    tmp_path: Path, source: str = SECOND_REVISION, name: str = "migrations"
) -> Path:
    """A copy of the package's migrations, with revision 9990 on top of its newest."""
    copy = tmp_path / name
    shutil.copytree(
        migrations.HERE, copy, ignore=shutil.ignore_patterns("__pycache__", "__init__.py")
    )
    (copy / "versions" / "9990_note.py").write_text(
        source.replace("{down}", head()), encoding="utf-8"
    )
    return copy


def insert_a_token(url: str) -> None:
    engine = open_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO tokens (token, state, status, visit, context, entered_at) "
                "VALUES ('T-1', 'coding', 'active', 1, '{}', '2026-10-06 09:00:00')"
            )
        )
    engine.dispose()


def test_the_baseline_is_the_first_revision_kept_in_its_own_table() -> None:
    assert (BASELINE, VERSION_TABLE) == ("0001", "stateous_version")
    assert head() == "0002"


def test_a_new_database_is_made_at_the_newest_revision(tmp_path: Path) -> None:
    url = sqlite_url(tmp_path / "s.db")

    Store(url).close()

    assert version_of(url) == head()
    assert table_names(url) == [
        "claims", "control_plane", "ledger", "memory", "stateous_version", "tasks", "timers",
        "tokens",
    ]  # fmt: skip


# SQLite cannot reflect an expression index, so the comparison skips `tokens_folded`
# and says so. The next test checks that index. On Postgres the comparison sees it.
@pytest.mark.filterwarnings("ignore:Skipped unsupported reflection of expression-based index")
@pytest.mark.filterwarnings("ignore:autogenerate skipping metadata-specified expression-based")
def test_the_migrations_build_exactly_the_tables_the_code_declares(tmp_path: Path) -> None:
    engine = create_engine(sqlite_url(tmp_path / "m.db"))

    upgrade_from_empty(engine)
    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"version_table": VERSION_TABLE, "compare_server_default": True}
        )
        differences = compare_metadata(context, tables.Base.metadata)
    engine.dispose()

    assert differences == []


def test_the_migrations_build_the_index_that_compares_token_ids_without_case(
    tmp_path: Path,
) -> None:
    engine = create_engine(sqlite_url(tmp_path / "m.db"))

    upgrade_from_empty(engine)
    with engine.connect() as connection:
        sql = connection.scalar(
            text("SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'tokens_folded'")
        )
    engine.dispose()

    assert sql is not None
    assert "lower(token)" in sql.replace('"', "")


def test_both_ways_to_make_a_database_give_it_one_control_plane_row(tmp_path: Path) -> None:
    made = sqlite_url(tmp_path / "made.db")
    Store(made).close()
    Store(made).close()
    migrated = sqlite_url(tmp_path / "migrated.db")
    engine = create_engine(migrated)
    upgrade_from_empty(engine)
    engine.dispose()

    assert control_plane_ids(made) == [1]
    assert control_plane_ids(migrated) == [1]


def test_a_database_at_the_baseline_gets_the_claims_table_and_keeps_its_rows(
    tmp_path: Path,
) -> None:
    url = sqlite_url(tmp_path / "baseline.db")
    engine = create_engine(url)
    upgrade_from_empty(engine, revision=BASELINE)
    engine.dispose()
    insert_a_token(url)
    assert "claims" not in table_names(url)

    Store(url).close()

    assert version_of(url) == head()
    assert "claims" in table_names(url)
    assert control_plane_ids(url) == [1]
    engine = create_engine(url)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT token FROM tokens")).scalars().all() == ["T-1"]
    engine.dispose()


def test_a_database_behind_the_newest_revision_is_upgraded_keeping_its_rows(
    tmp_path: Path,
) -> None:
    url = sqlite_url(tmp_path / "behind.db")
    Store(url).close()
    insert_a_token(url)
    engine = open_engine(url)

    migrate(engine, script_location=with_a_second_revision(tmp_path))
    engine.dispose()

    assert version_of(url) == "9990"
    assert "note" in token_columns(url)
    engine = create_engine(url)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT token FROM tokens")).scalars().all() == ["T-1"]
    engine.dispose()


@pytest.mark.filterwarnings("ignore:Skipped unsupported reflection of expression-based index")
def test_a_failed_upgrade_leaves_the_database_as_it_was_and_can_be_tried_again(
    tmp_path: Path,
) -> None:
    url = sqlite_url(tmp_path / "failing.db")
    Store(url).close()
    insert_a_token(url)
    before = table_names(url)
    engine = open_engine(url)

    with pytest.raises(OperationalError, match="no_such_table"):
        migrate(engine, script_location=with_a_second_revision(tmp_path, FAILING_SECOND_REVISION))

    assert version_of(url) == head()
    assert table_names(url) == before
    assert "note" not in token_columns(url)
    migrate(engine, script_location=with_a_second_revision(tmp_path, name="fixed"))
    engine.dispose()
    assert version_of(url) == "9990"


def test_a_database_at_a_revision_this_version_does_not_know_is_refused_untouched(
    tmp_path: Path,
) -> None:
    url = sqlite_url(tmp_path / "ahead.db")
    Store(url).close()
    engine = open_engine(url)
    migrate(engine, script_location=with_a_second_revision(tmp_path))
    engine.dispose()

    with pytest.raises(ForeignDatabase, match="another version of Stateous"):
        Store(url)

    assert version_of(url) == "9990"


def test_a_database_with_our_tables_but_no_version_is_refused_untouched(tmp_path: Path) -> None:
    url = sqlite_url(tmp_path / "foreign.db")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE tokens (token VARCHAR(256) PRIMARY KEY)"))
    engine.dispose()

    with pytest.raises(ForeignDatabase, match="no version"):
        Store(url)

    assert table_names(url) == ["tokens"]


def test_a_database_that_lost_a_table_is_refused(tmp_path: Path) -> None:
    url = sqlite_url(tmp_path / "broken.db")
    Store(url).close()
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE memory"))
    engine.dispose()

    with pytest.raises(ForeignDatabase, match="memory"):
        Store(url)


def test_a_database_at_the_newest_revision_that_lost_the_claims_table_is_refused(
    tmp_path: Path,
) -> None:
    url = sqlite_url(tmp_path / "no-claims.db")
    Store(url).close()
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE claims"))
    engine.dispose()

    with pytest.raises(ForeignDatabase, match="claims"):
        Store(url)


def test_a_database_at_the_baseline_with_a_claims_table_of_its_own_is_refused_untouched(
    tmp_path: Path,
) -> None:
    url = sqlite_url(tmp_path / "foreign-claims.db")
    engine = create_engine(url)
    upgrade_from_empty(engine, revision=BASELINE)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE claims (id INTEGER PRIMARY KEY)"))
    engine.dispose()

    with pytest.raises(ForeignDatabase, match="a table named claims that Stateous did not make"):
        Store(url)

    assert version_of(url) == BASELINE


def test_a_new_database_that_fails_to_be_stamped_is_left_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = sqlite_url(tmp_path / "unstamped.db")

    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("the disk is full")

    monkeypatch.setattr(MigrationContext, "stamp", fail)

    with pytest.raises(RuntimeError, match="the disk is full"):
        Store(url)

    assert table_names(url) == []


def test_another_apps_alembic_version_is_not_read_or_written(tmp_path: Path) -> None:
    url = sqlite_url(tmp_path / "shared.db")
    other = create_engine(url)
    with other.begin() as connection:
        connection.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
        )
        connection.execute(text("INSERT INTO alembic_version VALUES ('a1b2c3d4e5f6')"))
    other.dispose()

    Store(url).close()
    Store(url).close()

    assert version_of(url) == head()
    engine = create_engine(url)
    with engine.connect() as connection:
        rows = connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
    engine.dispose()
    assert rows == ["a1b2c3d4e5f6"]


def test_opening_a_database_twice_changes_nothing(tmp_path: Path) -> None:
    url = sqlite_url(tmp_path / "twice.db")
    Store(url).close()
    before = table_names(url)

    Store(url).close()

    assert table_names(url) == before
    assert version_of(url) == head()
