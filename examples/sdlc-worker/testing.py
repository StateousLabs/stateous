# sdlc-worker/testing.py
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.etree import ElementTree

from stateous import on_state

from contract import Tests, TicketContext, events, states  # generated


def pytest_in(checkout: Path, report: Path) -> None:
    """Run pytest in `checkout`, in a process group of its own. Then Ctrl-C to the
    supervisor does not stop the tests, and the supervisor waits for them."""
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    command.append(f"--junitxml={report}")
    if sys.platform == "win32":
        subprocess.run(
            command,
            cwd=checkout,
            capture_output=True,
            check=False,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    else:
        subprocess.run(
            command, cwd=checkout, capture_output=True, check=False, start_new_session=True
        )


def run_tests(repo: str) -> Tests:
    """Run the tests of the checkout `repo`. Count the tests that passed and failed."""
    checkout = Path(repo)
    if not checkout.is_dir():
        raise FileNotFoundError(f"no checkout at {checkout.resolve()}")
    with tempfile.TemporaryDirectory() as folder:
        report = Path(folder) / "report.xml"
        pytest_in(checkout, report)
        if not report.is_file():
            raise RuntimeError(f"pytest wrote no report for {checkout}")
        root = ElementTree.parse(report).getroot()
    suite = root if root.tag == "testsuite" else root[0]
    failed = int(suite.get("failures", "0")) + int(suite.get("errors", "0"))
    passed = int(suite.get("tests", "0")) - failed - int(suite.get("skipped", "0"))
    return Tests(passed=passed, failed=failed)


@on_state(states.testing)
def test(ctx: TicketContext) -> events.passed | events.failed:
    result = run_tests(ctx["repo"])
    return events.passed(result) if result.failed == 0 else events.failed(result)
