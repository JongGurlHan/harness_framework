#!/usr/bin/env python3
"""
Harness Step Executor — phase 내 step을 순차 실행하고, 완료 여부는 하네스가 직접 검증한다.

Usage:
    python scripts/execute.py <phase-dir> [--push] [--model MODEL] [--reset-failed]

흐름:
    step마다 claude -p 세션을 띄우고, 세션은 step{N}-result.json에 결과를 보고한다.
    하네스는 그 보고와 step의 AC 커맨드 실행 결과를 보고 상태를 확정한다.
    세션 ID는 하네스가 미리 발급해 index.json에 저장하므로, 실패·타임아웃·하네스 중단 뒤에도
    같은 세션을 --resume 으로 이어서 실패 출력을 전달한다.
"""

import argparse
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import types
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TIMEOUT_SEC = 3600
FEEDBACK_TAIL = 3000


@contextlib.contextmanager
def progress_indicator(label: str):
    """터미널 진행 표시기. with 문으로 사용하며 .elapsed 로 경과 시간을 읽는다."""
    frames = "◐◓◑◒"
    stop = threading.Event()
    t0 = time.monotonic()

    def _animate():
        idx = 0
        while not stop.wait(0.12):
            sec = int(time.monotonic() - t0)
            sys.stderr.write(f"\r{frames[idx % len(frames)]} {label} [{sec}s]")
            sys.stderr.flush()
            idx += 1
        sys.stderr.write("\r" + " " * (len(label) + 20) + "\r")
        sys.stderr.flush()

    th = threading.Thread(target=_animate, daemon=True)
    th.start()
    info = types.SimpleNamespace(elapsed=0.0)
    try:
        yield info
    finally:
        stop.set()
        th.join()
        info.elapsed = time.monotonic() - t0


def resolve_claude_bin() -> str:
    """claude 실행 파일 경로. Windows npm 설치의 claude.cmd shim 대신 실제 exe를 우선한다."""
    override = os.environ.get("HARNESS_CLAUDE_BIN")
    if override:
        return override
    found = shutil.which("claude")
    if not found:
        return "claude"
    if found.lower().endswith(".cmd"):
        real = Path(found).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        if real.exists():
            return str(real)
    return found


def dirty_paths_outside_phases(porcelain: str) -> list:
    """`git status --porcelain` 출력에서 phases/ 밖의 변경 경로만 추린다."""
    paths = []
    for line in porcelain.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].split(" -> ")[-1].strip().strip('"')
        if not path.startswith("phases/"):
            paths.append(path)
    return paths


def _tail(text: str, n: int = FEEDBACK_TAIL) -> str:
    return text if len(text) <= n else "…(앞부분 생략)\n" + text[-n:]


def _kill_tree(proc: subprocess.Popen):
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)


def run_killing_tree(cmd, *, cwd: str, timeout: int, input: Optional[str] = None,
                     shell: bool = False) -> tuple:
    """(exit_code, stdout, stderr, timed_out). 타임아웃이면 자식·손자 프로세스까지 모두 종료한다.
    직계 프로세스만 죽이면 파이프를 쥔 손자(npm, 테스트 러너 등)가 끝날 때까지 대기하고, 그 사이 작업도 계속된다."""
    extra = {} if os.name == "nt" else {"start_new_session": True}
    proc = subprocess.Popen(cmd, cwd=cwd, shell=shell, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", **extra)
    try:
        out, err = proc.communicate(input=input, timeout=timeout)
        return proc.returncode, out or "", err or "", False
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = "", ""
        return -1, out or "", err or "", True


@dataclass
class InvokeResult:
    exit_code: int
    timed_out: bool
    stderr: str


class StepExecutor:
    """Phase 디렉토리 안의 step들을 순차 실행하는 하네스."""

    MAX_ATTEMPTS = 3
    TZ = timezone(timedelta(hours=9))

    def __init__(self, phase_dir_name: str, *, root: Optional[Path] = None,
                 auto_push: bool = False, model: Optional[str] = None):
        self._rootp = Path(root or ROOT)
        self._root = str(self._rootp)
        self._phases_dir = self._rootp / "phases"
        self._phase_dir = self._phases_dir / phase_dir_name
        self._phase_dir_name = phase_dir_name
        self._top_index_file = self._phases_dir / "index.json"
        self._auto_push = auto_push

        if not self._phase_dir.is_dir():
            print(f"ERROR: {self._phase_dir} not found")
            sys.exit(1)

        self._index_file = self._phase_dir / "index.json"
        if not self._index_file.exists():
            print(f"ERROR: {self._index_file} not found")
            sys.exit(1)

        idx = self._read_json(self._index_file)
        self._project = idx.get("project", "project")
        self._phase_name = idx.get("phase", phase_dir_name)
        self._total = len(idx["steps"])
        self._model = model or idx.get("model")
        self._timeout = idx.get("timeout_sec", DEFAULT_TIMEOUT_SEC)
        self._branch = f"feat-{self._phase_name}"

    def run(self, *, reset_failed: bool = False):
        self._print_header()
        if reset_failed:
            self._reset_failed()
        self._check_blockers()
        self._validate_ac()
        self._ensure_clean_tree()
        self._checkout_branch()
        self._commit_plan_files()
        self._ensure_created_at()
        self._execute_all_steps()
        self._finalize()

    # --- timestamps / JSON I/O ---

    def _stamp(self) -> str:
        return datetime.now(self.TZ).strftime("%Y-%m-%dT%H:%M:%S%z")

    @staticmethod
    def _read_json(p: Path) -> dict:
        return json.loads(p.read_text(encoding="utf-8"))

    @staticmethod
    def _write_json(p: Path, data: dict):
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def _update_step(self, step_num: int, **fields):
        """index.json의 한 step을 갱신한다. 값이 None인 필드는 삭제한다."""
        index = self._read_json(self._index_file)
        for s in index["steps"]:
            if s["step"] == step_num:
                for k, v in fields.items():
                    if v is None:
                        s.pop(k, None)
                    else:
                        s[k] = v
        self._write_json(self._index_file, index)

    # --- git ---

    def _run_git(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=self._root, capture_output=True,
                              text=True, encoding="utf-8", errors="replace")

    def _git_commit(self, msg: str):
        r = self._run_git("commit", "-m", msg)
        if r.returncode != 0:
            print(f"  ERROR: 커밋 실패 — {msg}")
            print(f"  {(r.stderr or r.stdout).strip()}")
            sys.exit(1)
        print(f"  Commit: {msg}")

    def _ensure_clean_tree(self):
        """phases/ 밖에 커밋되지 않은 변경이 있으면 중단한다. 자동 커밋에 섞이는 것을 막기 위함."""
        r = self._run_git("status", "--porcelain")
        if r.returncode != 0:
            return  # git 미사용은 _checkout_branch 에서 처리
        dirty = dirty_paths_outside_phases(r.stdout)
        if dirty:
            print("  ERROR: phases/ 밖에 커밋되지 않은 변경이 있습니다. commit 또는 stash 후 다시 실행하세요.")
            for p in dirty[:20]:
                print(f"    {p}")
            sys.exit(1)

    def _checkout_branch(self):
        r = self._run_git("rev-parse", "--abbrev-ref", "HEAD")
        if r.returncode != 0:
            print("  ERROR: git을 사용할 수 없거나 git repo가 아닙니다.")
            print(f"  {r.stderr.strip()}")
            sys.exit(1)

        if r.stdout.strip() == self._branch:
            return

        r = self._run_git("rev-parse", "--verify", self._branch)
        r = self._run_git("checkout", self._branch) if r.returncode == 0 else self._run_git("checkout", "-b", self._branch)

        if r.returncode != 0:
            print(f"  ERROR: 브랜치 '{self._branch}' checkout 실패.")
            print(f"  {r.stderr.strip()}")
            sys.exit(1)

        print(f"  Branch: {self._branch}")

    def _commit_plan_files(self):
        """아직 커밋되지 않은 phase 계획 파일을 먼저 커밋해 step 커밋과 분리한다."""
        self._run_git("add", "--", "phases")
        if self._run_git("diff", "--cached", "--quiet").returncode != 0:
            self._git_commit(f"chore({self._phase_name}): phase plan")

    def _commit_step(self, step_num: int, step_name: str, kind: str = "feat"):
        """코드 변경(feat/wip)과 메타데이터(chore)를 분리 커밋한다."""
        index_rel = f"phases/{self._phase_dir_name}/index.json"
        self._run_git("add", "-A")
        self._run_git("reset", "-q", "HEAD", "--", index_rel)
        if self._run_git("diff", "--cached", "--quiet").returncode != 0:
            self._git_commit(f"{kind}({self._phase_name}): step {step_num} — {step_name}")

        self._run_git("add", "-A")
        if self._run_git("diff", "--cached", "--quiet").returncode != 0:
            self._git_commit(f"chore({self._phase_name}): step {step_num} output")

    # --- top-level index ---

    def _update_top_index(self, status: str):
        if not self._top_index_file.exists():
            return
        top = self._read_json(self._top_index_file)
        ts = self._stamp()
        for phase in top.get("phases", []):
            if phase.get("dir") == self._phase_dir_name:
                phase["status"] = status
                ts_key = {"completed": "completed_at", "error": "failed_at", "blocked": "blocked_at"}.get(status)
                if ts_key:
                    phase[ts_key] = ts
                break
        self._write_json(self._top_index_file, top)

    # --- 프롬프트 ---

    def _build_doc_index(self) -> str:
        """docs/*.md 경로와 제목만 나열한다. 내용은 세션이 필요할 때 직접 읽는다."""
        docs_dir = self._rootp / "docs"
        if not docs_dir.is_dir():
            return ""
        lines = []
        for doc in sorted(docs_dir.glob("*.md")):
            title = next((l[2:].strip() for l in doc.read_text(encoding="utf-8").splitlines()
                          if l.startswith("# ")), doc.stem)
            lines.append(f"- `docs/{doc.name}` — {title}")
        if not lines:
            return ""
        return ("## 프로젝트 문서\n\n"
                "CLAUDE.md는 자동으로 로드된다. 아래 문서는 이 step에 관련된 것을 골라 직접 읽어라.\n\n"
                + "\n".join(lines) + "\n\n")

    @staticmethod
    def _build_step_context(index: dict) -> str:
        lines = []
        for s in index["steps"]:
            if s["status"] != "completed" or not s.get("summary"):
                continue
            lines.append(f"- Step {s['step']} ({s['name']}): {s['summary']}")
            if s.get("handoff"):
                lines += [f"    {l}" for l in s["handoff"].splitlines()]
        if not lines:
            return ""
        return ("## 이전 Step 산출물\n\n" + "\n".join(lines)
                + "\n\n세부 변경은 `git log`와 코드에서 직접 확인하라.\n\n")

    def _result_file(self, step_num: int) -> Path:
        return self._phase_dir / f"step{step_num}-result.json"

    def _build_prompt(self, step: dict, index: dict) -> str:
        step_num, step_name = step["step"], step["name"]
        step_file = self._phase_dir / f"step{step_num}.md"
        if not step_file.exists():
            print(f"  ERROR: {step_file} not found")
            sys.exit(1)

        result_rel = f"phases/{self._phase_dir_name}/step{step_num}-result.json"
        ac = step.get("ac") or []
        ac_section = (
            "## Acceptance Criteria\n\n"
            "완료를 보고하기 전에 직접 실행해 통과를 확인하라. 하네스가 종료 후 같은 커맨드로 다시 검증한다.\n\n"
            "```bash\n" + "\n".join(ac) + "\n```\n\n"
        ) if ac else ""

        return (
            f"{self._project} 프로젝트, phase `{self._phase_name}`의 Step {step_num} ({step_name})을 수행한다.\n"
            f"무인 실행 세션이라 사용자에게 질문할 수 없다. 판단이 필요하면 설계 문서의 의도에 맞게 결정하고 "
            f"그 결정을 handoff에 남겨라.\n\n"
            f"{self._build_doc_index()}"
            f"{self._build_step_context(index)}"
            f"## 작업 방식\n\n"
            f"- 목표는 아래 step 문서가 설명하는 결과를 AC가 통과하는 상태로 만드는 것이다. "
            f"목표에 필요하면 여러 모듈을 함께 수정하고, 테스트·fixture·헬퍼 같은 새 파일을 만들어도 된다.\n"
            f"- step 목표와 무관한 기능 추가나 리팩터링은 하지 마라. 이유: 다음 step의 범위와 겹쳐 충돌한다.\n"
            f"- step 문서의 시그니처는 설계 의도다. 실제로 맞지 않으면 조정하고, 이유를 handoff에 적어라.\n"
            f"- 기존 테스트를 깨뜨리지 마라.\n"
            f"- git commit/push는 하지 마라(커밋하지 마라). 커밋은 하네스가 step 단위로 한다.\n"
            f"- `phases/{self._phase_dir_name}/index.json`은 수정하지 마라. 상태는 하네스가 관리한다.\n\n"
            f"{ac_section}"
            f"## 종료 보고\n\n"
            f"작업을 마치면 `{result_rel}`에 JSON으로 기록하라:\n\n"
            f"- 완료: `{{\"status\": \"completed\", \"summary\": \"산출물 한 줄 요약\", "
            f"\"handoff\": \"다음 step이 알아야 할 결정·바뀐 인터페이스·남은 이슈 (10줄 이내, 선택)\"}}`\n"
            f"- 사용자 개입 없이는 진행 불가(API 키, 외부 인증, 수동 설정 등): "
            f"`{{\"status\": \"blocked\", \"reason\": \"구체적 사유\"}}`\n"
            f"- 해결 방법을 더 찾지 못함: `{{\"status\": \"error\", \"reason\": \"시도한 것과 막힌 지점\"}}`\n\n"
            f"---\n\n"
            f"{step_file.read_text(encoding='utf-8')}"
        )

    def _build_interrupted_prompt(self, step_num: int) -> str:
        return (
            f"하네스가 중단됐다가 다시 시작됐다. Step {step_num} 작업이 도중에 끊겼을 수 있다.\n\n"
            f"작업 트리와 테스트 상태를 확인해 남은 작업을 마무리하고, AC를 확인한 뒤 "
            f"`phases/{self._phase_dir_name}/step{step_num}-result.json`을 새로 기록하라."
        )

    def _build_retry_prompt(self, step_num: int, feedback: str) -> str:
        return (
            f"하네스 검증 결과 Step {step_num}이 아직 완료되지 않았다.\n\n"
            f"{feedback}\n\n"
            f"원인을 찾아 고친 뒤 AC를 다시 확인하고 "
            f"`phases/{self._phase_dir_name}/step{step_num}-result.json`을 새로 기록하라."
        )

    # --- Claude 호출 ---

    def _invoke_claude(self, step_num: int, prompt: str, *, attempt: int,
                       session_id: str, resume: bool = False) -> InvokeResult:
        """session_id 는 하네스가 미리 발급한 값. resume=False 면 그 ID로 새 세션을 만든다."""
        cmd = [resolve_claude_bin(), "-p", "--dangerously-skip-permissions", "--output-format", "json"]
        if self._model:
            cmd += ["--model", self._model]
        cmd += ["--resume", session_id] if resume else ["--session-id", session_id]

        exit_code, stdout, stderr, timed_out = run_killing_tree(
            cmd, input=prompt, cwd=self._root, timeout=self._timeout)

        if exit_code != 0:
            print(f"\n  WARN: Claude 비정상 종료 (code {exit_code}{', timeout' if timed_out else ''})")

        out_path = self._phase_dir / f"step{step_num}-attempt{attempt}-output.json"
        self._write_json(out_path, {
            "step": step_num, "attempt": attempt, "model": self._model,
            "sessionId": session_id, "resumed": resume, "exitCode": exit_code, "timedOut": timed_out,
            "stdout": stdout, "stderr": stderr,
        })
        return InvokeResult(exit_code, timed_out, stderr)

    # --- 검증 ---

    def _run_ac(self, commands: list) -> tuple:
        """AC 커맨드를 순서대로 실행한다. 첫 실패에서 멈추고 출력 끝부분을 피드백으로 돌려준다."""
        for cmd in commands:
            rc, stdout, stderr, timed_out = run_killing_tree(
                cmd, cwd=self._root, timeout=self._timeout, shell=True)
            if timed_out:
                return False, f"AC 실패: `{cmd}` 타임아웃({self._timeout}s)"
            if rc != 0:
                return False, f"AC 실패: `{cmd}` (exit {rc})\n\n```\n{_tail(stdout + stderr)}\n```"
        return True, ""

    def _verify(self, step: dict, inv: InvokeResult) -> tuple:
        """(status, feedback, result) 반환. status: completed | blocked | retry"""
        result_file = self._result_file(step["step"])
        try:
            result = self._read_json(result_file)
        except (FileNotFoundError, ValueError):
            if inv.timed_out:
                return "retry", f"세션이 타임아웃({self._timeout}s)으로 종료됐다. 남은 작업을 이어서 마무리하라.", None
            return "retry", (f"`{result_file.name}`이 없거나 올바른 JSON이 아니다 (claude exit {inv.exit_code}).\n"
                             f"{_tail(inv.stderr, 1000)}").strip(), None

        status = result.get("status")
        if status == "blocked":
            return "blocked", result.get("reason", ""), result
        if status != "completed":
            return "retry", f"세션이 error를 보고했다: {result.get('reason', '')}", result

        ac = step.get("ac") or []
        if not ac:
            # skip_ac step: 검증할 커맨드가 없으니 최소한 세션이 정상 종료했는지는 확인한다
            if inv.timed_out or inv.exit_code != 0:
                return "retry", (f"completed를 보고했지만 세션이 정상 종료하지 않았다 "
                                 f"(exit {inv.exit_code}{', 타임아웃' if inv.timed_out else ''}). "
                                 f"작업이 실제로 끝났는지 확인하라."), result
            return "completed", "", result
        ok, feedback = self._run_ac(ac)
        return ("completed", "", result) if ok else ("retry", feedback, result)

    # --- 헤더 & 상태 점검 ---

    def _print_header(self):
        print(f"\n{'='*60}")
        print("  Harness Step Executor")
        print(f"  Phase: {self._phase_name} | Steps: {self._total} | Model: {self._model or 'default'}")
        if self._auto_push:
            print("  Auto-push: enabled")
        print(f"{'='*60}")

    def _check_blockers(self):
        for s in self._read_json(self._index_file)["steps"]:
            if s["status"] == "error":
                print(f"\n  ✗ Step {s['step']} ({s['name']}) failed.")
                print(f"  Error: {s.get('error_message', 'unknown')}")
                print("  원인을 해결한 뒤 --reset-failed 로 다시 실행하세요.")
                sys.exit(1)
            if s["status"] == "blocked":
                print(f"\n  ⏸ Step {s['step']} ({s['name']}) blocked.")
                print(f"  Reason: {s.get('blocked_reason', 'unknown')}")
                print("  사유를 해결한 뒤 --reset-failed 로 다시 실행하세요.")
                sys.exit(2)

    def _validate_ac(self):
        """미완료 step마다 ac 가 있거나, 검증 생략 사유(skip_ac)가 명시돼 있어야 한다."""
        missing = [s for s in self._read_json(self._index_file)["steps"]
                   if s["status"] != "completed"
                   and not s.get("ac") and not str(s.get("skip_ac") or "").strip()]
        if missing:
            print("\n  ERROR: 완료 검증 커맨드(ac)가 없는 step이 있습니다.")
            for s in missing:
                print(f"    Step {s['step']} ({s['name']})")
            print('  index.json에 "ac": ["npm test", ...]를 추가하세요. '
                  '검증을 생략하려면 "skip_ac": "사유"를 명시하세요.')
            sys.exit(1)

    def _reset_failed(self):
        index = self._read_json(self._index_file)
        for s in index["steps"]:
            if s["status"] in ("error", "blocked"):
                s["status"] = "pending"
                # session_id도 지운다: 사람이 고친 뒤이므로 이전 세션의 맥락을 이어받지 않는다
                for k in ("error_message", "failed_at", "blocked_reason", "blocked_at", "session_id"):
                    s.pop(k, None)
                print(f"  ↺ Step {s['step']} ({s['name']}) → pending")
        self._write_json(self._index_file, index)

    def _ensure_created_at(self):
        index = self._read_json(self._index_file)
        if "created_at" not in index:
            index["created_at"] = self._stamp()
            self._write_json(self._index_file, index)

    # --- 실행 루프 ---

    def _stop_step(self, step: dict, status: str, message: str, attempts: int):
        step_num, step_name = step["step"], step["name"]
        if status == "blocked":
            self._update_step(step_num, status="blocked", blocked_reason=message,
                              blocked_at=self._stamp(), attempts=attempts)
        else:
            self._update_step(step_num, status="error", error_message=message,
                              failed_at=self._stamp(), attempts=attempts)
        self._result_file(step_num).unlink(missing_ok=True)
        # 중간 결과를 커밋해 두면 재실행 시 작업 트리가 깨끗하고, 다음 세션이 이어받을 수 있다.
        self._commit_step(step_num, step_name, kind="wip")
        self._update_top_index(status)

    def _execute_single_step(self, step: dict):
        step_num, step_name = step["step"], step["name"]
        index = self._read_json(self._index_file)
        done = sum(1 for s in index["steps"] if s["status"] == "completed")
        prompt = self._build_prompt(step, index)

        # pending 인데 session_id 가 남아 있으면 이전 실행이 도중에 중단된 것 → 그 세션을 이어받는다
        session_id = step.get("session_id")
        resumable = session_id is not None
        if not session_id:
            session_id = str(uuid.uuid4())
        self._update_step(step_num, model=self._model or "default", session_id=session_id)

        feedback = None
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            self._result_file(step_num).unlink(missing_ok=True)
            if attempt == 1 and not resumable:
                p = prompt
            elif attempt == 1:
                p = self._build_interrupted_prompt(step_num)
            elif resumable:
                p = self._build_retry_prompt(step_num, feedback)
            else:
                session_id = str(uuid.uuid4())
                self._update_step(step_num, session_id=session_id)
                p = prompt + f"\n\n---\n\n## 이전 시도 실패\n\n{feedback}\n"
            resume = resumable

            tag = f"Step {step_num}/{self._total - 1} ({done} done): {step_name}"
            if attempt > 1:
                tag += f" [retry {attempt}/{self.MAX_ATTEMPTS}{', resumed' if resume else ''}]"

            with progress_indicator(tag) as pi:
                inv = self._invoke_claude(step_num, p, attempt=attempt, session_id=session_id, resume=resume)
                # 재개가 즉시 실패(결과 없이 비정상 종료)하면 세션을 이어받을 수 없는 것으로 보고 새 세션으로 전환
                resumable = not (resume and inv.exit_code != 0 and not inv.timed_out
                                 and not self._result_file(step_num).exists())
                status, feedback, result = self._verify(step, inv)
            elapsed = int(pi.elapsed)

            if status == "completed":
                self._update_step(step_num, status="completed", summary=result.get("summary", ""),
                                  handoff=result.get("handoff") or None,
                                  completed_at=self._stamp(), attempts=attempt)
                self._result_file(step_num).unlink(missing_ok=True)
                self._commit_step(step_num, step_name)
                print(f"  ✓ Step {step_num}: {step_name} [{elapsed}s]")
                return

            if status == "blocked":
                self._stop_step(step, "blocked", feedback, attempt)
                print(f"  ⏸ Step {step_num}: {step_name} blocked [{elapsed}s]")
                print(f"    Reason: {feedback}")
                sys.exit(2)

            first_line = feedback.splitlines()[0] if feedback else ""
            print(f"  ↻ Step {step_num}: attempt {attempt}/{self.MAX_ATTEMPTS} 실패 — {first_line}")

        self._stop_step(step, "error", f"[{self.MAX_ATTEMPTS}회 시도 후 실패] {feedback}", self.MAX_ATTEMPTS)
        print(f"  ✗ Step {step_num}: {step_name} failed after {self.MAX_ATTEMPTS} attempts")
        sys.exit(1)

    def _execute_all_steps(self):
        while True:
            index = self._read_json(self._index_file)
            pending = next((s for s in index["steps"] if s["status"] == "pending"), None)
            if pending is None:
                return
            if "started_at" not in pending:
                self._update_step(pending["step"], started_at=self._stamp())
            self._execute_single_step(pending)

    def _finalize(self):
        index = self._read_json(self._index_file)
        unfinished = [s for s in index["steps"] if s["status"] != "completed"]
        if unfinished:
            s = unfinished[0]
            print(f"\n  ERROR: Step {s['step']} ({s['name']})이 '{s['status']}' 상태라 phase를 완료 처리할 수 없습니다.")
            sys.exit(1)

        print("\n  All steps completed!")
        index["completed_at"] = self._stamp()
        self._write_json(self._index_file, index)
        self._update_top_index("completed")

        self._run_git("add", "-A")
        if self._run_git("diff", "--cached", "--quiet").returncode != 0:
            self._git_commit(f"chore({self._phase_name}): mark phase completed")

        if self._auto_push:
            r = self._run_git("push", "-u", "origin", self._branch)
            if r.returncode != 0:
                print(f"\n  ERROR: git push 실패: {r.stderr.strip()}")
                sys.exit(1)
            print(f"  ✓ Pushed to origin/{self._branch}")

        print(f"\n{'='*60}")
        print(f"  Phase '{self._phase_name}' completed!")
        print(f"{'='*60}")


def configure_stdio():
    """출력을 파이프/파일로 받을 때 Windows 기본 코덱(cp949)으로 기호를 쓰다 죽지 않게 UTF-8로 고정한다."""
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main():
    configure_stdio()
    parser = argparse.ArgumentParser(description="Harness Step Executor")
    parser.add_argument("phase_dir", help="Phase directory name (e.g. 0-mvp)")
    parser.add_argument("--push", action="store_true", help="Push branch after completion")
    parser.add_argument("--model", help="claude --model 값 (phase index.json의 model보다 우선)")
    parser.add_argument("--reset-failed", action="store_true",
                        help="error/blocked step을 pending으로 되돌린 뒤 실행")
    args = parser.parse_args()

    StepExecutor(args.phase_dir, root=ROOT, auto_push=args.push, model=args.model).run(
        reset_failed=args.reset_failed)


if __name__ == "__main__":
    main()
