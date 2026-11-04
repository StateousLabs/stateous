"""Try out a Stateous workflow in one process, with the real routines.

Run it with the project's Python, the one that has Stateous:

    python try_workflow.py GRAPH SUPERVISOR_FOLDER --context JSON
        [--fire STATE=EVENT[:PAYLOAD_JSON]]... [--timeout 600] [--quiet 120] [--json]

`--help` tells each option. Exit code 0: the token completed. 1: it stopped for
another reason. 2: wrong use.
"""

import sys

SKILL_VERSION = "{version}"
"""The version of Stateous that this skill is for."""

if __name__ == "__main__":
    try:
        from stateous.tryout.main import main
    except ImportError:
        print(
            "error: this Python has no Stateous; run the script with the project's Python",
            file=sys.stderr,
        )
        sys.exit(2)
    sys.exit(main(sys.argv[1:], skill_version=SKILL_VERSION))
