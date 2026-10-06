"""Why the store cannot open a database."""


class ForeignDatabase(Exception):
    """This version of Stateous cannot use the database: another version made it, or
    another program uses its table names."""


class DatabaseHeld(Exception):
    """Another control plane serves this database (`Store.hold_serving`)."""
