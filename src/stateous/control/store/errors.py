"""Why the store cannot open a database."""


class ForeignDatabase(Exception):
    """This version of Stateous cannot use the database: another version made it, or
    another program uses its table names."""


class DatabaseHeld(Exception):
    """Another control plane serves this database (`Store.hold_serving`)."""


class SchemaNotReady(Exception):
    """The database has no Stateous tables, or tables of another version, and the store
    may not migrate them (`Store(..., migrate=False)`). The message names the command."""
