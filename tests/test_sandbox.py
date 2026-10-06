"""Sandbox isolation and execution-failure classification."""
import os

import pytest

from app.config import Config
from app.sandbox import Sandbox, parse_result
from app.security import check_code
from tests.conftest import SYNTHETIC

SB = Sandbox(Config(timeout_s=8))  # default 1 GB memory cap
READ = 'import pandas as pd\ndf = pd.read_csv("data/orders.csv", dtype=str)\n'


def run(code, trusted=False):
    return SB.run(code, SYNTHETIC, ["orders"], trusted=trusted)


def test_valid_code_runs():
    r = run(READ + 'print("RESULT: " + str(len(df)))')
    assert r.ok and r.result == 612 and r.exit_code == 0 and r.duration_s > 0


@pytest.mark.parametrize("code", [
    "import os\nprint(os.environ)",
    "import subprocess\nsubprocess.run(['whoami'])",
    "import socket",
    "print(open('data/orders.csv').read())",
    "__import__('os').system('dir')",
    "x = ().__class__.__bases__[0].__subclasses__()",
    "import pandas as pd\npd.read_pickle('x')",
    "import pandas as pd\npd.read_csv('data/orders.csv', engine='pyarrow')",
    "import numpy as np\nnp.fromfile('data/orders.csv')",
    "import pandas as pd\npd.DataFrame().to_csv('out.csv')",
])
def test_policy_rejects_dangerous_code(code):
    assert check_code(code)
    assert run(code).status == "policy_rejected"


# The checks below bypass the static policy (trusted=True) to prove the runtime boundary holds on its own.
def test_environment_holds_no_secrets(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-secret")
    r = run('import os\nprint("RESULT: " + __import__("json").dumps(sorted(os.environ)))', trusted=True)
    assert r.ok and "ANTHROPIC_API_KEY" not in r.result and "sk-test-secret" not in r.stdout


@pytest.mark.parametrize("code", [
    "import subprocess\nsubprocess.run(['whoami'])",
    "import os\nos.system('echo hi')",
    f"print(open({os.path.expanduser('~/.gitconfig')!r}).read())",
    "import pandas as pd\npd.read_csv('http://example.com/x.csv')",
    "import socket\nsocket.create_connection(('example.com', 80))",
    "open('data/orders.csv', 'w').write('tampered')",
    "import os\nos.remove('data/orders.csv')",
    "import shutil\nshutil.rmtree('data')",
    "import ctypes\nctypes.CDLL('kernel32' if __import__('os').name == 'nt' else 'libc.so.6')",
])
def test_runtime_guard_blocks_escape(code):
    r = run(code, trusted=True)
    assert r.status == "security_violation", (r.status, r.error, r.stderr[-300:])


def test_violation_flagged_even_if_caught():
    r = run("try:\n    open('../x.txt', 'w')\nexcept Exception:\n    pass\nprint('RESULT: 1')", trusted=True)
    assert r.status == "security_violation"


def test_source_data_unchanged_after_runs():
    before = (SYNTHETIC / "orders.csv").read_bytes()
    run("open('data/orders.csv', 'a').write('x')", trusted=True)
    assert (SYNTHETIC / "orders.csv").read_bytes() == before


def test_timeout():
    r = Sandbox(Config(timeout_s=2)).run("while True:\n    pass", SYNTHETIC, [])
    assert r.status == "timeout" and r.timed_out


def test_memory_limit():
    r = run("x = bytearray(2 * 1024 ** 3)\nprint('RESULT: 1')")
    assert r.status == "runtime_error"


@pytest.mark.parametrize("code,status", [
    ("print('RESULT: 1'", "policy_rejected"),  # syntax error
    ("x = 1 / 0", "runtime_error"),
    ("import pandas as pd\npd.read_csv('data/missing.csv')", "runtime_error"),
    ("import scipy", "policy_rejected"),
    ("print('the answer is 5')", "invalid_output"),
    ("print('RESULT: {bad json')", "invalid_output"),
    ("print('RESULT: 1')\nprint('RESULT: 2')", "invalid_output"),
])
def test_execution_failures_classified(code, status):
    assert run(code).status == status


def test_parse_result():
    assert parse_result('noise\nRESULT: {"a": "1.00"}\n') == ({"a": "1.00"}, None)
    assert parse_result("")[1]
