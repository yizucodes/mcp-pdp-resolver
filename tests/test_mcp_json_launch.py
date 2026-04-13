"""
Verify mcp.json launch layout: relative ``mcp_server.py`` with repo as cwd.

Run from the repository root with dependencies installed::

    python3 -m pip install -r requirements.txt
    python3 -m pytest tests/ -v
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MCP_JSON = REPO_ROOT / "mcp.json"


def _pdp_resolver_entry() -> dict:
    with open(MCP_JSON, encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg["mcpServers"]["pdp-resolver"]


class TestMcpJsonLaunch(unittest.TestCase):
    def test_mcp_json_uses_relative_script_path(self) -> None:
        entry = _pdp_resolver_entry()
        self.assertEqual(
            entry.get("args"),
            ["mcp_server.py"],
            "mcp.json should use a single relative script arg so clients can set cwd to the repo.",
        )
        cmd = entry.get("command")
        self.assertIsInstance(cmd, str)
        self.assertIn(cmd, ("python", "python3"))

    def test_server_stays_up_with_repo_cwd_and_relative_args(self) -> None:
        """Same layout as MCP clients: cwd = repo, argv = [python, mcp_server.py]."""
        entry = _pdp_resolver_entry()
        args = entry["args"]
        self.assertEqual(args, ["mcp_server.py"])

        env = os.environ.copy()
        env.setdefault("FIRECRAWL_API_KEY", "test-key-for-unit-test")

        with subprocess.Popen(
            [sys.executable, *args],
            cwd=str(REPO_ROOT),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        ) as proc:
            time.sleep(0.6)
            try:
                if proc.poll() is not None:
                    _, err = proc.communicate(timeout=5)
                    self.fail(
                        "Expected MCP server to keep running with cwd=repo and args=mcp_server.py; "
                        f"exited with code={proc.returncode}, stderr={err.decode(errors='replace')!r}"
                    )
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.communicate(timeout=5)

    def test_relative_script_not_found_if_cwd_is_wrong(self) -> None:
        """Guardrail: relative args require the client working directory to be the repo."""
        entry = _pdp_resolver_entry()
        args = entry["args"]
        env = os.environ.copy()
        env.setdefault("FIRECRAWL_API_KEY", "test-key-for-unit-test")

        proc = subprocess.run(
            [sys.executable, *args],
            cwd=tempfile.gettempdir(),
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(
            proc.returncode,
            0,
            "Running with cwd outside the repo should fail when args are relative.",
        )


if __name__ == "__main__":
    unittest.main()
