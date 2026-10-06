"""The public names: contract section 3, as far as phase 1 makes them."""

import stateous


def test_the_public_names_are_the_contracts() -> None:
    assert stateous.__all__ == [
        "END",
        "START",
        "Context",
        "Occurrence",
        "OutcomeError",
        "StateRef",
        "Workflow",
        "on_state",
    ]
    for name in stateous.__all__:
        assert getattr(stateous, name) is not None
