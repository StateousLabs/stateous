"""The supervisor's folder: stateous.yaml, the contract's checksum, and the stubs."""

import json
import socket
from pathlib import Path

import pytest

from stateous.supervisor.folder import (
    FolderConfig,
    FolderError,
    implemented_states,
    load_folder,
    stub_name,
    write_folder,
)

CHECKSUM = "sha256:" + "a" * 64
CONTRACT_TEXT = (
    "# GENERATED from the `sdlc` workflow — do not edit.\n"
    f"# {CHECKSUM}\n"
    '__workflow__ = "sdlc"\n'
    f'__checksum__ = "{CHECKSUM}"\n'
)


def folder_with(tmp_path: Path, config: str, contract: str = CONTRACT_TEXT) -> Path:
    folder = tmp_path / "sdlc-worker"
    folder.mkdir()
    (folder / "contract.py").write_text(contract, encoding="utf-8")
    (folder / "stateous.yaml").write_text(config, encoding="utf-8")
    return folder


def stub(state: str) -> str:
    name = state.replace("-", "_")
    return (
        "from contract import states\n\nfrom stateous import on_state\n\n\n"
        f"@on_state(states.{name})\ndef {name}(ctx: object) -> None:\n"
        "    raise NotImplementedError\n"
    )


def test_a_folder_is_loaded_with_its_settings_and_checksum(tmp_path: Path) -> None:
    folder = folder_with(tmp_path, "url: http://cp.internal:8080/\ntasks: 2\nname: box-1\n")

    loaded = load_folder(folder)

    assert loaded.path == folder.resolve()
    assert loaded.checksum == CHECKSUM
    assert (loaded.config.url, loaded.config.tasks) == ("http://cp.internal:8080", 2)
    assert loaded.config.supervisor_name() == "box-1"


def test_tasks_default_to_four_and_the_name_to_the_host_name(tmp_path: Path) -> None:
    loaded = load_folder(folder_with(tmp_path, "url: http://127.0.0.1:8080\n"))

    assert loaded.config.tasks == 4
    assert loaded.config.supervisor_name() == socket.gethostname()


def test_a_path_that_is_not_a_folder_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FolderError, match="is not a folder"):
        load_folder(tmp_path / "nothing")


@pytest.mark.parametrize("missing", ["contract.py", "stateous.yaml"])
def test_a_folder_without_its_files_is_refused(tmp_path: Path, missing: str) -> None:
    folder = folder_with(tmp_path, "url: http://127.0.0.1:8080\n")
    (folder / missing).unlink()

    with pytest.raises(FolderError, match=rf"has no {missing}: run `stateous init URL "):
        load_folder(folder)


@pytest.mark.parametrize(
    ("config", "said"),
    [
        ("url: http://x.test\ntasks: 0\n", "tasks: input should be greater than or equal to 1"),
        ("url: http://x.test\ntasks: 1001\n", "tasks: input should be less than or equal to 1000"),
        ("url: http://x.test\nport: 9\n", "port: there is no such setting"),
        ("url: cp.internal:8080\n", "url: 'cp.internal:8080' is not an http:// or https://"),
        ("tasks: 2\n", "url: missing"),
        ("", "url: missing"),
        ("- url\n", "it is not a mapping of settings"),
        ("url: [\n", "cannot read"),
    ],
)
def test_bad_settings_are_named_in_one_plain_sentence(
    tmp_path: Path, config: str, said: str
) -> None:
    folder = folder_with(tmp_path, config)

    with pytest.raises(FolderError) as raised:
        load_folder(folder)

    assert said in str(raised.value)
    assert "\n" not in str(raised.value)
    assert "pydantic" not in str(raised.value)


def test_a_contract_without_a_checksum_is_refused(tmp_path: Path) -> None:
    folder = folder_with(tmp_path, "url: http://127.0.0.1:8080\n", contract="x = 1\n")

    with pytest.raises(FolderError, match=r"has no __checksum__: run `stateous init URL .*` again"):
        load_folder(folder)


def test_implemented_states_are_found_without_importing_the_files(tmp_path: Path) -> None:
    (tmp_path / "contract.py").write_text("raise SystemExit('never imported')\n", encoding="utf-8")
    (tmp_path / "a.py").write_text(stub("coding"), encoding="utf-8")
    (tmp_path / "b.py").write_text(
        "import stateous\nimport contract\n\n\n"
        "@stateous.on_state(contract.states.in_progress)\n"
        "async def work(ctx: object) -> None:\n    return None\n",
        encoding="utf-8",
    )
    (tmp_path / "c.py").write_text("def plain() -> None:\n    return None\n", encoding="utf-8")
    (tmp_path / "broken.py").write_text("def (:\n", encoding="utf-8")

    assert implemented_states(tmp_path) == {"coding", "in_progress"}


def test_a_stub_is_named_after_its_state() -> None:
    assert stub_name("coding") == "coding.py"
    assert stub_name("in-progress") == "in_progress.py"


def test_a_new_folder_gets_the_contract_a_stub_per_state_and_stateous_yaml(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "new" / "sdlc-worker"

    lines = write_folder(
        folder,
        url="http://127.0.0.1:8080",
        contract=CONTRACT_TEXT,
        states=["coding", "in-progress"],
        stub=stub,
    )

    assert (folder / "contract.py").read_text(encoding="utf-8") == CONTRACT_TEXT
    assert (folder / "coding.py").read_text(encoding="utf-8") == stub("coding")
    assert (folder / "in_progress.py").read_text(encoding="utf-8") == stub("in-progress")
    loaded = load_folder(folder)
    assert (loaded.config.url, loaded.config.tasks) == ("http://127.0.0.1:8080", 4)
    assert [line.split()[0] for line in lines] == ["wrote", "wrote", "wrote", "wrote"]


def test_init_again_replaces_the_contract_and_keeps_everything_else(tmp_path: Path) -> None:
    folder = tmp_path / "sdlc-worker"
    write_folder(folder, url="http://a.test", contract="old\n", states=["coding"], stub=stub)
    (folder / "coding.py").write_text(stub("coding") + "# mine\n", encoding="utf-8")
    (folder / "stateous.yaml").write_text("url: http://b.test\ntasks: 9\n", encoding="utf-8")

    lines = write_folder(
        folder, url="http://a.test", contract=CONTRACT_TEXT, states=["coding"], stub=stub
    )

    assert (folder / "contract.py").read_text(encoding="utf-8") == CONTRACT_TEXT
    assert (folder / "coding.py").read_text(encoding="utf-8").endswith("# mine\n")
    settings = (folder / "stateous.yaml").read_text(encoding="utf-8")
    assert settings == "url: http://b.test\ntasks: 9\n"
    assert [line.split()[0] for line in lines] == ["wrote", "kept", "kept"]


def test_a_state_implemented_in_another_file_gets_no_stub(tmp_path: Path) -> None:
    folder = tmp_path / "sdlc-worker"
    folder.mkdir()
    (folder / "all_routines.py").write_text(stub("coding"), encoding="utf-8")

    write_folder(folder, url="http://a.test", contract=CONTRACT_TEXT, states=["coding"], stub=stub)

    assert not (folder / "coding.py").exists()


def test_a_file_with_the_stubs_name_is_never_written_over(tmp_path: Path) -> None:
    folder = tmp_path / "sdlc-worker"
    folder.mkdir()
    (folder / "coding.py").write_text("HELPERS = 1\n", encoding="utf-8")

    lines = write_folder(
        folder, url="http://a.test", contract=CONTRACT_TEXT, states=["coding"], stub=stub
    )

    assert (folder / "coding.py").read_text(encoding="utf-8") == "HELPERS = 1\n"
    assert any(line.startswith("skipped") and "'coding'" in line for line in lines)


@pytest.mark.parametrize(
    ("url", "said"),
    [
        ("http://[::1", "url: 'http://[::1' is not a valid address: invalid port: ':1'"),
        ("http://", "url: 'http://' has no host"),
        ("http://:8080", "url: 'http://:8080' has no host"),
        ("http://127.0.0.1:99999", "url: 'http://127.0.0.1:99999' has the port 99999"),
        ("http://127.0.0.1:0", "url: 'http://127.0.0.1:0' has the port 0"),
    ],
)
def test_a_url_that_cannot_be_reached_is_refused_in_one_sentence(
    tmp_path: Path, url: str, said: str
) -> None:
    folder = folder_with(tmp_path, f"url: {json.dumps(url)}\n")

    with pytest.raises(FolderError) as raised:
        load_folder(folder)

    assert said in str(raised.value)
    assert "\n" not in str(raised.value)


def test_an_ipv6_url_with_a_port_is_taken() -> None:
    assert FolderConfig(url="https://[::1]:8443/").url == "https://[::1]:8443"


@pytest.mark.parametrize("state", ["contract", "stateous", "json", "asyncio", "typing"])
def test_a_stub_never_takes_the_name_of_a_python_module(state: str) -> None:
    assert stub_name(state) == f"{state}_routine.py"


def test_init_says_why_a_stub_has_another_name(tmp_path: Path) -> None:
    folder = tmp_path / "sdlc-worker"

    lines = write_folder(
        folder, url="http://a.test", contract=CONTRACT_TEXT, states=["json", "coding"], stub=stub
    )

    assert (folder / "json_routine.py").read_text(encoding="utf-8") == stub("json")
    assert not (folder / "json.py").exists()
    assert f"wrote    {folder / 'json_routine.py'} (json.py would hide a Python module)" in lines
    assert f"wrote    {folder / 'coding.py'}" in lines
