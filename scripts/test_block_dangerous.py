"""
.claude/hooks/block_dangerous.py 테스트.
Claude Code PreToolUse 규약대로 stdin JSON을 넣고 종료 코드를 확인한다 (2 = 차단).
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "block_dangerous.py"


def run_hook(command: str, tool_name: str = "Bash") -> subprocess.CompletedProcess:
    payload = {"hook_event_name": "PreToolUse", "tool_name": tool_name, "tool_input": {"command": command}}
    return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                          capture_output=True, text=True, encoding="utf-8")


@pytest.mark.parametrize("cmd", [
    "rm -rf /",
    "rm -fr build",
    "rm -Rf node_modules",
    "rm -r -f dist",
    "rm --recursive --force dist",
    "cd src && rm -rf ..",
    "git push --force origin main",
    "git push -f",
    "git push origin main --force-with-lease",
    "git reset --hard HEAD~1",
    "git clean -fdx",
    "psql -c 'DROP TABLE users'",
    "psql -c 'drop database prod'",
])
def test_blocks_dangerous(cmd):
    r = run_hook(cmd)
    assert r.returncode == 2, cmd
    assert "BLOCKED" in r.stderr


@pytest.mark.parametrize("cmd", [
    "rm file.txt",
    "rm -r empty_dir",
    "git push origin feat-mvp",
    "git reset --soft HEAD~1",
    "git clean -n",
    "npm run build && npm test",
    "echo 'format the drive'",
])
def test_allows_safe(cmd):
    assert run_hook(cmd).returncode == 0, cmd


def test_ignores_non_bash_tool():
    assert run_hook("rm -rf /", tool_name="Write").returncode == 0


def test_invalid_json_does_not_crash():
    r = subprocess.run([sys.executable, str(HOOK)], input="not json", capture_output=True, text=True)
    assert r.returncode == 0
