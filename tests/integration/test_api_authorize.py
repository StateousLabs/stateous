"""The authorizer over HTTP and in process: each transport passes its caller."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path

import httpx2
from starlette.requests import Request

from stateous import ControlPlane
from stateous.control.access import Action
from stateous.control.api import create_app
from stateous.control.inprocess import InProcess
from stateous.transport import Refused

from ..unit.control.support import TICKET, sdlc, sqlite_url

U1 = {"user": "u1"}


def only_own(caller: object, action: Action, labels: Mapping[str, str]) -> bool:
    return labels.get("user") == caller


def user_of(request: Request) -> object:
    return request.headers.get("x-user")


def test_the_caller_callable_reaches_the_authorizer_and_forbidden_is_403(
    tmp_path: Path,
) -> None:
    async def main() -> tuple[int, str, int]:
        url = sqlite_url(tmp_path / "h.db")
        with ControlPlane(sdlc(), database=url, migrate=True, authorize=only_own) as plane:
            app = create_app(plane, caller=user_of)
            async with httpx2.AsyncClient(
                base_url="http://testserver", transport=httpx2.ASGITransport(app=app)
            ) as client:
                body = {"token": "T-1", "context": TICKET, "labels": U1}
                refused = await client.post("/tokens", json=body, headers={"X-User": "u2"})
                allowed = await client.post("/tokens", json=body, headers={"X-User": "u1"})
                return refused.status_code, refused.json()["code"], allowed.status_code

    assert asyncio.run(main()) == (403, "forbidden", 201)


def test_in_process_passes_its_caller(tmp_path: Path) -> None:
    async def main() -> str:
        url = sqlite_url(tmp_path / "i.db")
        with ControlPlane(sdlc(), database=url, migrate=True, authorize=only_own) as plane:
            plane_start = plane.start
            await asyncio.to_thread(lambda: plane_start("T-1", TICKET, labels=U1, caller="u1"))
            transport = InProcess(plane, caller="u2")
            try:
                await transport.claim("box", ["coding"], labels=U1)
            except Refused as refused:
                return refused.code
            return "allowed"

    assert asyncio.run(main()) == "forbidden"


def test_every_route_passes_its_caller(tmp_path: Path) -> None:
    async def main() -> list[int]:
        url = sqlite_url(tmp_path / "all.db")
        with ControlPlane(sdlc(), database=url, migrate=True, authorize=only_own) as plane:
            app = create_app(plane, caller=user_of)
            async with httpx2.AsyncClient(
                base_url="http://testserver",
                transport=httpx2.ASGITransport(app=app),
                headers={"X-User": "u1"},
            ) as client:
                statuses: list[int] = []
                for token in ("T-1", "T-2", "T-3"):
                    body = {"token": token, "context": TICKET, "labels": U1}
                    statuses.append((await client.post("/tokens", json=body)).status_code)
                claim = {"supervisor": "box", "states": ["coding"], "labels": U1}
                grant = (await client.post("/tasks/claim", json=claim)).json()
                task, attempt = grant["task"], grant["attempt"]
                calls = [
                    client.get("/health"),
                    client.get("/workflow"),
                    client.get("/tokens/T-1"),
                    client.get("/tokens", params={"label": "user=u1"}),
                    client.get("/tokens/T-1/history"),
                    client.post(f"/tasks/{task}/note", json={"text": "x", "attempt": attempt}),
                    client.put(
                        f"/tasks/{task}/memory", json={"key": "k", "value": 1, "attempt": attempt}
                    ),
                    client.post("/tasks/heartbeat", json={"supervisor": "box", "tasks": [task]}),
                    client.post(
                        f"/tasks/{task}/report",
                        json={"kind": "event", "event": "stuck", "supervisor": "box"},
                    ),
                    client.post("/tokens/T-2/pause"),
                    client.post("/tokens/T-2/resume"),
                    client.post("/tokens/T-2/cancel"),
                    client.post("/tokens/T-3/events/stuck", json={}),
                ]
                for call in calls:
                    statuses.append((await call).status_code)
                return statuses

    statuses = asyncio.run(main())

    assert all(status < 300 for status in statuses), statuses


def test_scope_of_the_caller_hides_other_scopes(tmp_path: Path) -> None:
    def scope_of(caller: object) -> Mapping[str, str] | None:
        return None if caller is None else {"user": str(caller)}

    async def main() -> tuple[int, int, int]:
        url = sqlite_url(tmp_path / "s.db")
        with ControlPlane(
            sdlc(), database=url, migrate=True, scope=["user"], scope_of=scope_of
        ) as plane:
            app = create_app(plane, caller=user_of)
            async with httpx2.AsyncClient(
                base_url="http://testserver", transport=httpx2.ASGITransport(app=app)
            ) as client:
                body = {"token": "T-1", "context": TICKET, "labels": U1}
                made = await client.post("/tokens", json=body, headers={"X-User": "u1"})
                mine = await client.get("/tokens/T-1", headers={"X-User": "u1"})
                theirs = await client.get("/tokens/T-1", headers={"X-User": "u2"})
                return made.status_code, mine.status_code, theirs.status_code

    assert asyncio.run(main()) == (201, 200, 404)


def test_an_admin_start_checks_only_the_scope_of_its_labels(tmp_path: Path) -> None:
    from stateous import ALL_SCOPES

    def scope_of(caller: object) -> object:
        return ALL_SCOPES if caller == "admin" else {"user": str(caller)}

    async def main() -> list[int]:
        url = sqlite_url(tmp_path / "adm.db")
        with ControlPlane(
            sdlc(),
            database=url,
            migrate=True,
            scope=["user"],
            scope_of=scope_of,  # type: ignore[arg-type]
        ) as plane:
            for user in ("u1", "u2"):
                plane.start("T-1", TICKET, labels={"user": user}, caller=user)
            app = create_app(plane, caller=user_of)
            async with httpx2.AsyncClient(
                base_url="http://testserver",
                transport=httpx2.ASGITransport(app=app),
                headers={"X-User": "admin"},
            ) as client:
                body = {"token": "T-1", "context": TICKET, "labels": {"user": "u3"}}
                made = await client.post("/tokens", json=body)
                again = await client.post("/tokens", json=body)
                return [made.status_code, again.status_code]

    assert asyncio.run(main()) == [201, 200]
