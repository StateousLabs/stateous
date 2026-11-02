"""The public names that `stateous.__all__` exports."""

import stateous


def test_the_public_names_are_the_contracts() -> None:
    assert stateous.__all__ == [
        "END",
        "START",
        "AttemptOver",
        "Branch",
        "Branches",
        "ChildToken",
        "Client",
        "Context",
        "ControlPlane",
        "ControlPlaneError",
        "DatabaseHeld",
        "EventNotAllowed",
        "Fired",
        "ForeignDatabase",
        "GraphConflict",
        "HistoryEntry",
        "HoldLost",
        "InvalidArgument",
        "InvalidContext",
        "InvalidPayload",
        "InvalidTokenId",
        "Notice",
        "NoticeKind",
        "NoticesOff",
        "Occurrence",
        "OutcomeError",
        "SchemaNotReady",
        "StateRef",
        "TaskNotFound",
        "TaskNotHeld",
        "Token",
        "TokenNotActive",
        "TokenNotFound",
        "Workflow",
        "on_state",
        "supervise",
    ]
    for name in stateous.__all__:
        assert getattr(stateous, name) is not None
