"""The package and its parts, as the contract lays them out."""

import importlib

import pytest


@pytest.mark.parametrize(
    "name",
    [
        "stateous",
        "stateous.model",
        "stateous.compiler",
        "stateous.control",
        "stateous.supervisor",
        "stateous.client",
        "stateous.cli",
    ],
)
def test_each_part_of_the_package_imports(name: str) -> None:
    assert importlib.import_module(name).__doc__
