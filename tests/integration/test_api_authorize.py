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
