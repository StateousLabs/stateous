# Build a workflow with an agent

This guide tells you how to give your coding agent the Stateous skill. With it, the agent builds a workflow, runs it and finds its errors.

The skill works with each agent that reads skills in the open Agent Skills format, for example Claude Code, Codex, Cursor and OpenCode.

## 1. Install the skill

Find the folder where your agent reads skills. Your agent's documentation names it. For example, Claude Code reads the skills of a project in `.claude/skills`.

```bash
stateous install skill .claude/skills
```

The command writes `.claude/skills/stateous/`. Stateous does not look for the folders of your agents: you choose the folder.

The skill has three parts:

- `SKILL.md`: the steps of the work, and the pages to read before each step.
- `references/`: the pages of this site, for the version of Stateous that you installed. The Python API page holds the signatures and the docstrings.
- `scripts/try_workflow.py`: the try-out. It runs a workflow in one process, with the real routines.

## 2. Ask for a workflow

Tell the agent what the workflow must do, in your own words. The agent asks you when a choice changes the graph, for example a state, an event, or who does the work of a state.

The agent then does these steps:

1. It writes a starter workflow with `stateous init workflow`, and changes `flow.py` and `schemas.py`.
2. It compiles the workflow with `stateous compile`, and corrects each error.
3. It writes the supervisor's folder with `stateous init supervisor`, and writes the routines.
4. It tries out the workflow with the try-out script.
5. It reads the report of the try-out, corrects the cause of a stop, and tries again.

## 3. The try-out

The agent runs the script with the Python of the supervisor's folder. It gives the context in a JSON file:

```text
uv run --project SUPERVISOR_FOLDER python .claude/skills/stateous/scripts/try_workflow.py GRAPH SUPERVISOR_FOLDER --context @context.json
```

The script makes a new SQLite database in a temporary folder. It starts a control plane, the supervisor of the folder and one token. It runs the real routines, also the agents with PHD.

The script stops by itself. It stops when the token ends, when nothing changes for some time, or when its time limit ends. It also stops at a state that waits for the world. That is a state with no routine, a state that waits for an event from outside, or a timer.

The report tells each task, how long it took, its event or its error, and its notes. It also holds the history of each token, and the page of this site that tells about the stop.

At a state that waits for the world, the agent asks you what the answer is. It does not choose the answer itself. Then it runs the script again with `--fire STATE=EVENT`, or `--fire STATE=EVENT:@FILE` for an event with data in a JSON file.

## 4. After an upgrade

The skill is for one version of Stateous. After you upgrade Stateous, write the skill again:

```bash
stateous install skill .claude/skills --force
```

When the versions are not the same, the try-out script stops, and tells you this command.

## See also

- [The commands](../reference/cli.md): `stateous install skill`.
- [Write a workflow](write-a-workflow.md) and [Write a routine](write-a-routine.md): the steps that the agent follows.
- [Agents, with PHD](../concepts/agents.md): routines that run an agent.
