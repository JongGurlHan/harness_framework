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

# 옵션은 같은 단순 명령 안에서만 본다 (; & | 줄바꿈에서 끊는다). 순서·묶음(-rf, -f -r)·긴 이름을 모두 잡는다.
SEG = r"[^;&|\n]*"
END = r"(?=[\s;&|)]|$)"  # 옵션 바로 뒤에 구분자가 붙어도(-f; -f| -f)) 옵션의 끝으로 본다
ARG = r"""(?:"[^"]*"|'[^']*'|\S+)"""  # 따옴표로 묶인 인자는 공백을 포함한다
GIT = rf"\bgit(?:\s+(?:-[Cc]\s+{ARG}|-[^\s=]+(?:={ARG})?))*\s+"  # -C path, -c k=v, --no-pager 같은 전역 옵션을 건너뛴다
RECURSIVE = rf"\s(?:-[a-zA-Z]*[rR][a-zA-Z]*|--recursive){END}"
FORCE = rf"\s(?:-[a-zA-Z]*f[a-zA-Z]*|--force[^\s;&|)]*){END}"

PATTERNS = [
    (rf"\brm\b(?={SEG}{RECURSIVE})(?={SEG}{FORCE})", "rm -rf"),
    (rf"{GIT}push\b(?={SEG}(?:{FORCE}|\s\+\S))", "git push --force"),  # +refspec 도 강제 push 다
    (rf"{GIT}reset\b(?={SEG}\s--hard\b)", "git reset --hard"),
    (rf"{GIT}clean\b(?={SEG}{FORCE})", "git clean -f"),
    (rf"(?i)\b(?:rd|rmdir)\b(?={SEG}\s/s\b)", "rd /s"),
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
