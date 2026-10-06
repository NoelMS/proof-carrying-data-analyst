"""Docker provider (skipped when Docker or the sandbox image is unavailable).

Build the image first:  docker build -f Dockerfile.sandbox -t pcda-sandbox:latest .
"""
import shutil
import subprocess

import pytest

from app.config import Config
from app.sandbox import Sandbox
from app.workflow import Analyst
from tests.conftest import SYNTHETIC


def _image_ready() -> bool:
    if not shutil.which("docker"):
        return False
    r = subprocess.run(["docker", "image", "inspect", "pcda-sandbox:latest"], capture_output=True)
    return r.returncode == 0


pytestmark = pytest.mark.skipif(not _image_ready(), reason="docker sandbox image not available")
CFG = Config(llm_provider="none", sandbox="docker", timeout_s=30)


def test_workflow_verifies_in_docker(catalog):
    st = Analyst(catalog, CFG).run("What is the revenue in USD by region?")
    assert st.verified, st.final.get("reason")
    assert st.final["numeric_value"]["Europe"] == "163760.97"


@pytest.mark.parametrize("code", [
    "import socket\nsocket.create_connection(('1.1.1.1', 53), timeout=3)",
    "print(open('/etc/hostname').read())",
    "open('data/orders.csv', 'w').write('x')",
])
def test_docker_blocks_escape(code):
    r = Sandbox(CFG).run(code, SYNTHETIC, ["orders"], trusted=True)
    assert r.status in ("security_violation", "runtime_error") and not r.ok
