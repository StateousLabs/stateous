"""`stateous token ...` against a real control plane."""

import socket

import pytest
from typer.testing import CliRunner, Result

from stateous.cli.main import app

runner = CliRunner()


def run(*args: str) -> Result:
    return runner.invoke(app, list(args))


def ok(*args: str) -> str:
    result = run(*args)
    assert result.exit_code == 0, result.output
    return result.stdout


def nowhere() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{probe.getsockname()[1]}"


def test_token_is_a_command_group() -> None:
    output = run("--help").output

    assert "token" in output
    assert "serve" in output


def test_start_fire_and_show_a_token(served: str) -> None:
    assert ok("token", "start", "T-1", '{"repo": "acme/api"}', "--url", served) == (
        "T-1: coding, active\n"
    )
    assert ok("token", "fire", "T-1", "done", "--url", served) == "T-1: testing, active\n"

    shown = ok("token", "show", "T-1", "--url", served).splitlines()

    assert shown[0] == "T-1: testing, active"
    assert any(line.endswith("entered coding") for line in shown)
    assert any(line.endswith("done in coding") for line in shown)


def test_the_url_comes_from_stateous_url(served: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STATEOUS_URL", served)

    assert ok("token", "start", "T-1", '{"repo": "acme/api"}') == "T-1: coding, active\n"


def test_fire_with_a_payload_and_a_key(served: str) -> None:
    ok("token", "start", "T-1", '{"repo": "acme/api"}', "--url", served)
    ok("token", "fire", "T-1", "done", "--url", served)
    fire = ["token", "fire", "T-1", "passed", '{"passed": 3, "failed": 0}', "--key", "ci-9"]

    first = ok(*fire, "--url", served)
    second = ok(*fire, "--url", served)

    assert first.endswith(", completed\n")
    assert second.startswith("duplicate: T-1: ")


def test_show_without_an_id_lists_the_tokens(served: str) -> None:
    assert ok("token", "show", "--url", served) == "no tokens\n"
    ok("token", "start", "T-1", '{"repo": "a"}', "--url", served)
    ok("token", "start", "T-2", '{"repo": "b"}', "--url", served)

    listed = sorted(ok("token", "show", "--url", served).splitlines())

    assert listed == ["T-1: coding, active", "T-2: coding, active"]


def test_show_graph_draws_the_workflow(served: str) -> None:
    drawn = ok("token", "show", "--graph", "--url", served)

    assert drawn.startswith("stateDiagram-v2\n")
    assert "    coding --> testing: done\n" in drawn


def test_pause_resume_and_cancel(served: str) -> None:
    ok("token", "start", "T-1", '{"repo": "acme/api"}', "--url", served)

    assert ok("token", "pause", "T-1", "--url", served) == "T-1: coding, paused\n"
    assert ok("token", "resume", "T-1", "--url", served) == "T-1: coding, active\n"
    assert ok("token", "cancel", "T-1", "--url", served).endswith(", cancelled\n")


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["token", "start", "T-1", "{bad"], "CONTEXT_JSON is not valid JSON"),
        (["token", "start", "T-1", "[1]"], "CONTEXT_JSON must be a JSON object"),
        (["token", "fire", "T-1", "done", "nope"], "PAYLOAD_JSON is not valid JSON"),
        (["token", "show", "T-1", "--graph"], "not both"),
        (["token", "show", "NOPE"], "(404)"),
    ],
)
def test_a_mistake_is_one_sentence_and_exit_1(served: str, args: list[str], message: str) -> None:
    result = run(*args, "--url", served)

    assert result.exit_code == 1
    assert message in result.output
    assert "Traceback" not in result.output
    assert len(result.output.strip().splitlines()) == 1


def test_a_control_plane_that_does_not_answer_is_one_sentence() -> None:
    result = run("token", "show", "--url", nowhere())

    assert result.exit_code == 1
    assert "cannot reach the control plane" in result.output
    assert "Traceback" not in result.output
    assert len(result.output.strip().splitlines()) == 1


def test_a_url_that_is_not_a_url_is_one_sentence() -> None:
    result = run("token", "show", "--url", "http://[::1")

    assert result.exit_code == 1
    assert "http://[::1 is not a URL" in result.output
    assert "Traceback" not in result.output
    assert len(result.output.strip().splitlines()) == 1


def test_a_url_with_a_host_that_is_not_valid_is_one_sentence() -> None:
    result = run("token", "show", "--url", "http://my host:8000")

    assert result.exit_code == 1
    assert "has a host that is not valid" in result.output
    assert "Traceback" not in result.output
    assert len(result.output.strip().splitlines()) == 1
