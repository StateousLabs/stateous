"""The public names that `stateous.__all__` exports."""

import stateous


def test_the_public_names_are_the_contracts() -> None:
    assert stateous.__all__ == [
        "END",
        "START",
        "Client",
        "Context",
        "Occurrence",
        "OutcomeError",
        "StateRef",
        "Workflow",
        "on_state",
    ]
    for name in stateous.__all__:
        assert getattr(stateous, name) is not None
