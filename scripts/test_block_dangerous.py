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
    # 옵션 순서·표기 변형 (회귀: -f 가 -r 보다 앞이거나 긴 옵션만 쓰면 통과했다)
    "rm -f -r dist",
    "rm --force --recursive dist",
    "rm -r --force dist",
    "sudo rm -rf /tmp/x",
    "find . -name cache -exec rm -rf {} +",
    "bash -c 'rm -rf dist'",
    "git push --force origin main",
    "git push -f",
    "git push origin main --force-with-lease",
    "git push -uf origin x",
    "git push -fu origin x",
    "git push origin +main",
    "git reset --hard HEAD~1",
    "git clean -fdx",
    "git clean -xdf",
    # git 전역 옵션 (회귀: git 바로 뒤에 하위 명령이 와야만 잡았다)
    "git -C . reset --hard HEAD",
    "git -c core.pager=cat push --force",
    "git --no-pager clean -fdx",
    # 옵션 바로 뒤의 구분자·따옴표로 묶인 인자 (회귀: 옵션 뒤에 공백이나 끝이 와야만 잡았다)
    "git push -f; echo done",
    "git push -f|cat",
    "(git push -f)",
    "rm -r dist -f; echo done",
    "rm -r dist -f&& echo done",
    'git -C "C:/My Project" reset --hard HEAD',
    "git -C 'my dir' clean -fd",
    # Windows (Bash 도구에서 cmd 를 거쳐 실행)
    "cmd //c rd /s /q build",
    "cmd /c rmdir /S /Q build",
    "psql -c 'DROP TABLE users'",
    "psql -c 'drop database prod'",
])
def test_blocks_dangerous(cmd):
    r = run_hook(cmd)
    assert r.returncode == 2, cmd
    assert "BLOCKED" in r.stderr


@pytest.mark.parametrize("cmd", [
    "rm file.txt",
    "rm -f file.txt",
    "rm -r empty_dir",
    "rm -r build; ls -f",  # -f 는 다른 명령의 옵션이다
    "git rm -r --cached foo",
    "git push origin feat-mvp",
    "git push --follow-tags origin feat-mvp",
    "git push origin feat+x",
    "git -C sub status",
    'git -C "C:/My Project" status',
    "git reset --soft HEAD~1",
    "git reset HEAD~1 && echo --hard",
    "git clean -n",
    "git clean -e cache -n",
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
