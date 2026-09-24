#!/usr/bin/env python3
"""
PreToolUse(Bash) 훅 — 되돌리기 어려운 명령을 차단한다.

Claude Code 규약: 도구 입력은 stdin JSON으로 들어오고, exit 2 + stderr 가 차단 신호다.
(exit 1 은 차단되지 않는 일반 오류로 처리된다.)

정규식 기반의 과속방지턱일 뿐 샌드박스가 아니다. settings.json 의 permissions.deny 와 함께 쓴다.
"""

import json
import re
import sys

PATTERNS = [
    (r"\brm\s+(-\S+\s+)*-[a-zA-Z]*[rR][a-zA-Z]*f", "rm -rf"),
    (r"\brm\s+(-\S+\s+)*-[a-zA-Z]*f[a-zA-Z]*[rR]", "rm -rf"),
    (r"\brm\s+.*(-r|-R|--recursive)\b.*\s(-f|--force)\b", "rm -r -f"),
    (r"\bgit\s+push\b.*\s(--force\S*|-f)\b", "git push --force"),
    (r"\bgit\s+reset\s+.*--hard\b|\bgit\s+reset\s+--hard\b", "git reset --hard"),
    (r"\bgit\s+clean\s+(-\S+\s+)*-[a-zA-Z]*f", "git clean -f"),
    (r"(?i)\bdrop\s+(table|database|schema)\b", "DROP TABLE/DATABASE"),
]


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except ValueError:
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    command = (payload.get("tool_input") or {}).get("command", "")
    for pattern, label in PATTERNS:
        if re.search(pattern, command):
            msg = f"BLOCKED: 위험한 명령({label})이 감지되었습니다. 다른 방법을 사용하세요.\n"
            sys.stderr.buffer.write(msg.encode("utf-8"))  # Windows 기본 코덱(cp949)이면 메시지가 깨진다
            sys.stderr.flush()
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
