# sdlc-supervisor/coding.py: an agent, run by PHD (see the guide, "Agents, with PHD")
import phd

from stateous import on_state

from contract import TicketContext, events, states


@on_state(states.coding)
async def code(ctx: TicketContext) -> events.done | events.stuck:
    spec = phd.Spec(
        harness="claude",
        model="sonnet",
        stack=phd.Stack.load("stacks/dev"),  # the agent: a PHD stack folder
        cwd=ctx["repo"],
        credentials=phd.Credentials.from_host("claude"),  # the login of this computer
        output_schema=states.coding.outcome_schema(),
        idle_timeout_s=900,  # no event for 15 minutes: stop (PHD request 7)
    )
    inputs = phd.Inputs(msg=f"Implement {ctx['repo']}")
    # Leaving the block stops an unfinished run:
    # an error, or the token left the state (PHD request 1).
    async with phd.Dispatcher(spec).run(inputs, session_id=ctx.memory.get("dev")) as handle:
        async for ev in handle.events():
            if handle.session_id and "dev" not in ctx.memory:
                # known after run.started; kept across a crash (PHD request 2)
                ctx.memory["dev"] = handle.session_id
            ctx.note(ev.summary)  # progress in the token's history (PHD request 6)
        result = await handle.wait()
    if result.status != "succeeded":
        if result.error and result.error.kind == "session" and "dev" in ctx.memory:
            del ctx.memory["dev"]  # the session cannot be resumed: the retry starts fresh
        raise RuntimeError(result.error)  # a failed attempt
    return states.coding.outcome(result.structured_output)
