"""Why the store cannot open a database. Each is a `ControlPlaneError`, with a code."""

from typing import ClassVar

from stateous.control.errors import ControlPlaneError


class ForeignDatabase(ControlPlaneError):
    """This version of Stateous cannot use the database: another version made it, or
    another program uses its table names."""

    code: ClassVar[str] = "foreign_database"


class DatabaseHeld(ControlPlaneError):
    """Another control plane serves this database (`Store.hold_serving`)."""

    code: ClassVar[str] = "database_held"


class SchemaNotReady(ControlPlaneError):
    """The database has no Stateous tables, or tables of another version, and the store
    may not migrate them (`Store(..., migrate=False)`). The message names the command."""

    code: ClassVar[str] = "schema_not_ready"
