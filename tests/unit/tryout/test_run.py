"""`try_workflow`: run a workflow in one process, and stop with a reason."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from stateous import InvalidContext
from stateous.supervisor.supervisor import SupervisorError
from stateous.tryout.report import TryReport
from stateous.tryout.run import Fire, parse_fire, try_workflow

from .support import job, parallel

CONTEXT = {"title": "first"}


def tried(
    graph: Path,
    folder: Path,
    context: dict[str, Any] | None = None,
    *,
    fires: list[Fire] | None = None,
    timeout: float = 30,
    quiet: float = 20,
) -> TryReport:
    return asyncio.run(
        try_workflow(
            graph,
            folder,
            context if context is not None else CONTEXT,
            fires=fires or [],
            timeout=timeout,
            quiet=quiet,
            database=graph.parent / "try.db",
        )
    )


def test_the_workflow_runs_to_its_end(tmp_path: Path) -> None:
    report = tried(*job(tmp_path, "return events.done()"))
    assert report.stop.reason == "completed"
    assert report.stop.page is None
    assert report.steps[0].state == "work"
    assert report.steps[0].outcome == "event done"
    top = report.tokens[0].token
    kinds = [entry.kind for entry in report.histories[top]]
    assert kinds[0] == "started"
    assert "task_done" in kinds
    assert report.database.is_file()


def test_a_state_with_no_routine_stops_and_is_named(tmp_path: Path) -> None:
    report = tried(*job(tmp_path, "return events.other()", broken=False))
    assert report.stop.reason == "no_routine"
    assert report.stop.state == "broken"
    assert report.stop.page == "how-to/run-supervisors.md"
    assert "broken" in report.stop.sentence


def test_a_waiting_routine_stops_and_names_the_events(tmp_path: Path) -> None:
    report = tried(*job(tmp_path, "return None"))
    assert report.stop.reason == "waits_outside"
    assert report.stop.state == "work"
    assert "done" in report.stop.sentence
    assert "other" in report.stop.sentence
    assert "--fire" in report.stop.sentence


def test_a_fire_answers_a_waiting_state(tmp_path: Path) -> None:
    report = tried(*job(tmp_path, "return None"), fires=[parse_fire("work=done")])
    assert report.stop.reason == "completed"
    assert report.fires_used == ["work=done"]
    assert report.fires_unused == []


def test_an_unused_fire_is_reported(tmp_path: Path) -> None:
    report = tried(*job(tmp_path, "return events.done()"), fires=[parse_fire("nowhere=done")])
    assert report.stop.reason == "completed"
    assert report.fires_unused == ["nowhere=done"]


def test_a_timer_wait_stops_and_names_the_timer(tmp_path: Path) -> None:
    report = tried(*job(tmp_path, 'ctx.schedule(events.done, after="1h")\nreturn None'))
    assert report.stop.reason == "waits_timer"
    assert "done" in report.stop.sentence
    assert report.stop.page == "concepts/timers.md"


def test_a_failing_routine_follows_error(tmp_path: Path) -> None:
    report = tried(*job(tmp_path, 'raise RuntimeError("no disk")', retries=1))
    failed = [step for step in report.steps if step.state == "work"]
    assert [step.attempt for step in failed] == [1, 2]
    assert all(step.outcome is not None and "no disk" in step.outcome for step in failed)
    assert report.steps[-1].state == "broken"
    assert report.stop.reason == "completed"


def test_a_routine_that_never_ends_hits_the_time_limit(tmp_path: Path) -> None:
    report = tried(*job(tmp_path, "await asyncio.sleep(3600)"), timeout=3, quiet=60)
    assert report.stop.reason == "time_limit"
    assert "work" in report.stop.sentence
    assert report.steps[0].ended is None


def test_a_routine_with_no_sign_of_life_is_quiet(tmp_path: Path) -> None:
    report = tried(*job(tmp_path, "await asyncio.sleep(3600)"), timeout=60, quiet=2)
    assert report.stop.reason == "quiet"
    assert "work" in report.stop.sentence


def test_notes_keep_a_long_routine_from_quiet(tmp_path: Path) -> None:
    body = 'for n in range(10):\n    ctx.note(f"step {n}")\n    await asyncio.sleep(0.3)'
    report = tried(*job(tmp_path, body + "\nreturn events.done()"), quiet=1.5)
    assert report.stop.reason == "completed"
    assert len(report.steps[0].notes) == 10


def test_a_bad_context_is_refused_before_the_run(tmp_path: Path) -> None:
    with pytest.raises(InvalidContext, match="title"):
        tried(*job(tmp_path, "return events.done()"), {"name": "x"})


def test_parallel_children_are_in_the_report(tmp_path: Path) -> None:
    report = tried(*parallel(tmp_path), {"repo": "r"})
    assert report.stop.reason == "no_routine"
    assert report.stop.state == "security/triage" or report.stop.state == "design/triage"
    children = [token.token for token in report.tokens if token.parent is not None]
    assert len(children) == 2
    assert all(child in report.histories for child in children)


def test_parse_fire_forms() -> None:
    assert parse_fire("review=approved") == Fire("review", "approved", None)
    assert parse_fire('review=approved:{"by": "ann"}').payload == {"by": "ann"}
    assert parse_fire("security/triage=lgtm").state == "security/triage"
    assert parse_fire("review=approved").label == "review=approved"
    for bad in ("review", "=x", "review=", "review=a:[1]", "review=a:{"):
        with pytest.raises(ValueError, match="STATE=EVENT"):
            parse_fire(bad)


def test_a_fire_for_another_state_is_not_used_at_a_wait(tmp_path: Path) -> None:
    report = tried(*job(tmp_path, "return None"), fires=[parse_fire("nowhere=done")])
    assert report.stop.reason == "waits_outside"
    assert report.fires_unused == ["nowhere=done"]


def test_a_token_that_fails_ends_the_run(tmp_path: Path) -> None:
    graph, folder = job(tmp_path, "return events.other()", broken_body='raise RuntimeError("x")')
    report = tried(graph, folder)
    assert report.stop.reason == "failed"
    assert "failed at broken" in report.stop.sentence
    assert report.stop.page == "concepts/tasks-and-routines.md"


def test_a_supervisor_that_ends_early_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def at_once(*args: object, **kwargs: object) -> int:
        return 0

    monkeypatch.setattr("stateous.tryout.run.supervise", at_once)
    with pytest.raises(SupervisorError, match="stopped before the token ended"):
        tried(*job(tmp_path, "return events.done()"))


def test_a_new_temporary_database_without_one(tmp_path: Path) -> None:
    graph, folder = job(tmp_path, "return events.done()")
    report = asyncio.run(try_workflow(graph, folder, CONTEXT, timeout=30))
    assert report.database.is_file()
    assert report.database.parent != graph.parent


def test_a_time_limit_with_nothing_running_names_the_state(tmp_path: Path) -> None:
    report = tried(*job(tmp_path, "return events.done()"), timeout=0)
    assert report.stop.reason == "time_limit"
    assert "is at" in report.stop.sentence
