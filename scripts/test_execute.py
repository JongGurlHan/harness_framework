"""
execute.py 테스트.
git·claude CLI·AC 커맨드는 모두 mock 처리하고, 상태 전이와 프롬프트 구성을 검증한다.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import execute as ex

REPO = Path(__file__).resolve().parent.parent
PY = sys.executable.replace("\\", "/")
AC_OK = f'"{PY}" -c "pass"'  # cmd.exe·bash·sh 어디서나 통과하는 AC
OK = {"status": "completed", "summary": "ok"}


def write_json(p: Path, data):
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def read_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def git(root: Path, *args) -> str:
    r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return r.stdout


def plan(root: Path, steps: list, phase: str = "p") -> Path:
    """phases/{phase}/ 에 index.json 과 step{N}.md 를 만든다."""
    d = root / "phases" / phase
    d.mkdir(parents=True, exist_ok=True)
    write_json(d / "index.json", {"project": "T", "phase": phase, "steps": steps})
    for s in steps:
        (d / f"step{s['step']}.md").write_text(f"# Step {s['step']}", encoding="utf-8")
    return d


def install_fake_claude(executor, runs: list) -> list:
    """_invoke_claude 를 가짜 세션으로 바꾼다. 호출마다 runs 의 다음 원소대로 파일을 쓰고 결과를 보고한다.
    runs 원소: {"files": {경로: 내용}, "do": 함수(root), "result": dict|None, "exit": int, "timeout": bool,
               "raise": 예외}"""
    calls = []

    def fake(step_num, prompt, *, attempt, session_id, resume=False):
        calls.append({"step": step_num, "prompt": prompt, "session_id": session_id, "resume": resume})
        run = runs[len(calls) - 1]
        for rel, text in run.get("files", {}).items():
            p = executor._rootp / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        if "do" in run:
            run["do"](executor._rootp)
        if "raise" in run:
            raise run["raise"]
        if run.get("result") is not None:
            write_json(executor._result_file(step_num), run["result"])
        return ex.InvokeResult(exit_code=run.get("exit", 0), timed_out=run.get("timeout", False), stderr="")

    executor._invoke_claude = fake
    return calls


def heartbeat_code(beat: Path) -> str:
    """손자 프로세스를 띄우고 잠드는 자식 코드. 손자는 살아 있는 동안 beat 파일에 계속 덧붙인다."""
    grandchild = ("import time\n"
                  "while True:\n"
                  f"    open({str(beat)!r}, 'a').write('.')\n"
                  "    time.sleep(0.05)\n")
    return f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {grandchild!r}]); time.sleep(60)"


def assert_heartbeat_stopped(beat: Path):
    time.sleep(0.5)
    size = beat.stat().st_size
    time.sleep(0.5)
    assert beat.stat().st_size == size, "손자 프로세스가 아직 살아 있다"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_project(tmp_path):
    """phases/, CLAUDE.md, docs/ 를 갖춘 임시 프로젝트 구조."""
    (tmp_path / "phases").mkdir()
    (tmp_path / "CLAUDE.md").write_text("# Rules\n- rule one", encoding="utf-8")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "arch.md").write_text("# 아키텍처\nSome content", encoding="utf-8")
    (docs / "guide.md").write_text("# UI 가이드\nAnother doc", encoding="utf-8")
    return tmp_path


@pytest.fixture
def phase_dir(tmp_project):
    """step 3개를 가진 phase 디렉토리."""
    d = tmp_project / "phases" / "0-mvp"
    d.mkdir()
    write_json(d / "index.json", {
        "project": "TestProject",
        "phase": "mvp",
        "steps": [
            {"step": 0, "name": "setup", "status": "completed", "summary": "프로젝트 초기화 완료"},
            {"step": 1, "name": "core", "status": "completed", "summary": "핵심 로직 구현",
             "handoff": "MarketBus 인터페이스를 publish/subscribe로 확정"},
            {"step": 2, "name": "ui", "status": "pending", "ac": ["npm run build", "npm test"]},
        ],
    })
    (d / "step2.md").write_text("# Step 2: UI\n\nUI를 구현하세요.", encoding="utf-8")
    return d


@pytest.fixture
def top_index(tmp_project):
    p = tmp_project / "phases" / "index.json"
    write_json(p, {"phases": [
        {"dir": "0-mvp", "status": "pending"},
        {"dir": "1-polish", "status": "pending"},
    ]})
    return p


@pytest.fixture
def executor(tmp_project, phase_dir):
    inst = ex.StepExecutor("0-mvp", root=tmp_project)
    inst._run_git = MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))
    return inst


def make_executor(tmp_project, steps, **kw):
    plan(tmp_project, steps, phase="t")
    return ex.StepExecutor("t", root=tmp_project, **kw)


# ---------------------------------------------------------------------------
# 기본 유틸
# ---------------------------------------------------------------------------

class TestStamp:
    def test_returns_kst_timestamp(self, executor):
        assert "+0900" in executor._stamp()


class TestJsonHelpers:
    def test_roundtrip_utf8(self, tmp_path):
        p = tmp_path / "t.json"
        ex.StepExecutor._write_json(p, {"k": "한글"})
        assert "한글" in p.read_text(encoding="utf-8")
        assert ex.StepExecutor._read_json(p) == {"k": "한글"}


    def test_write_survives_crash_mid_write(self, tmp_path, monkeypatch):
        # 회귀: 파일을 비운 직후 쓰기가 실패하면 index.json 이 0바이트로 남아 재시작조차 못 했다
        p = tmp_path / "index.json"
        ex.StepExecutor._write_json(p, {"v": 1})

        def crash(self, *a, **k):
            self.open("w").close()
            raise OSError("disk full")

        with monkeypatch.context() as m:
            m.setattr(Path, "write_text", crash)
            m.setattr(Path, "write_bytes", crash)
            with pytest.raises(OSError):
                ex.StepExecutor._write_json(p, {"v": 2})
        assert ex.StepExecutor._read_json(p) == {"v": 1}


class TestInit:
    def test_reads_model_and_timeout_from_index(self, tmp_project):
        d = tmp_project / "phases" / "cfg"
        d.mkdir()
        write_json(d / "index.json", {"project": "P", "phase": "cfg", "model": "claude-opus-5-5",
                                      "timeout_sec": 7200, "steps": []})
        inst = ex.StepExecutor("cfg", root=tmp_project)
        assert inst._model == "claude-opus-5-5"
        assert inst._timeout == 7200

    def test_reads_cost_caps(self, tmp_project):
        d = tmp_project / "phases" / "cfg"
        d.mkdir()
        write_json(d / "index.json", {"project": "P", "phase": "cfg", "max_cost_usd": 10,
                                      "step_max_cost_usd": 2.5, "steps": []})
        inst = ex.StepExecutor("cfg", root=tmp_project)
        assert inst._max_cost == 10 and inst._step_max_cost == 2.5

    def test_cli_model_overrides_index(self, tmp_project):
        d = tmp_project / "phases" / "cfg"
        d.mkdir()
        write_json(d / "index.json", {"project": "P", "phase": "cfg", "model": "a", "steps": []})
        assert ex.StepExecutor("cfg", root=tmp_project, model="b")._model == "b"

    def test_defaults(self, executor):
        assert executor._model is None
        assert executor._timeout == ex.DEFAULT_TIMEOUT_SEC
        assert executor._mcp is False

    def test_reads_mcp_from_index(self, tmp_project):
        d = tmp_project / "phases" / "cfg"
        d.mkdir()
        write_json(d / "index.json", {"project": "P", "phase": "cfg", "mcp": True, "steps": []})
        assert ex.StepExecutor("cfg", root=tmp_project)._mcp is True


# ---------------------------------------------------------------------------
# 프롬프트 구성
# ---------------------------------------------------------------------------

class TestDocIndex:
    def test_lists_docs_with_titles(self, executor):
        s = executor._build_doc_index()
        assert "docs/arch.md" in s and "아키텍처" in s
        assert "docs/guide.md" in s and "UI 가이드" in s

    def test_does_not_inline_contents(self, executor):
        assert "Some content" not in executor._build_doc_index()

    def test_does_not_inline_claude_md(self, executor):
        # CLAUDE.md는 claude CLI가 자동 로드하므로 중복 주입하지 않는다
        assert "rule one" not in executor._build_doc_index()

    def test_no_docs_dir(self, tmp_path):
        (tmp_path / "phases" / "p").mkdir(parents=True)
        write_json(tmp_path / "phases" / "p" / "index.json", {"steps": []})
        assert ex.StepExecutor("p", root=tmp_path)._build_doc_index() == ""


class TestBuildStepContext:
    def test_includes_summaries_and_handoff(self, phase_dir):
        ctx = ex.StepExecutor._build_step_context(read_json(phase_dir / "index.json"))
        assert "Step 0 (setup): 프로젝트 초기화 완료" in ctx
        assert "MarketBus 인터페이스" in ctx

    def test_excludes_pending(self, phase_dir):
        ctx = ex.StepExecutor._build_step_context(read_json(phase_dir / "index.json"))
        assert "Step 2" not in ctx

    def test_handoff_kept_without_summary(self):
        # 회귀: summary 가 없으면 handoff 까지 통째로 빠졌다
        ctx = ex.StepExecutor._build_step_context({"steps": [
            {"step": 0, "name": "a", "status": "completed", "handoff": "login(email)로 시그니처 변경"}]})
        assert "login(email)" in ctx

    def test_empty_when_no_completed(self):
        assert ex.StepExecutor._build_step_context({"steps": [{"step": 0, "name": "a", "status": "pending"}]}) == ""


class TestBuildPrompt:
    def _prompt(self, executor):
        index = read_json(executor._index_file)
        return executor._build_prompt(index["steps"][2], index)

    def test_includes_step_body(self, executor):
        assert "UI를 구현하세요" in self._prompt(executor)

    def test_includes_ac_commands(self, executor):
        p = self._prompt(executor)
        assert "npm run build" in p and "npm test" in p

    def test_includes_result_file_path(self, executor):
        assert "phases/0-mvp/step2-result.json" in self._prompt(executor)

    def test_tells_model_not_to_commit(self, executor):
        assert "커밋하지 마라" in self._prompt(executor)

    def test_allows_new_files(self, executor):
        p = self._prompt(executor)
        assert "추가 기능이나 파일을 만들지 마라" not in p
        assert "새 파일" in p

    def test_no_nested_retry_count(self, executor):
        assert "회 수정 시도" not in self._prompt(executor)

    def test_includes_doc_index_and_context(self, executor):
        p = self._prompt(executor)
        assert "docs/arch.md" in p
        assert "핵심 로직 구현" in p


# ---------------------------------------------------------------------------
# claude 실행
# ---------------------------------------------------------------------------

class TestResolveClaudeBin:
    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("HARNESS_CLAUDE_BIN", "/x/claude")
        assert ex.resolve_claude_bin() == "/x/claude"

    def test_prefers_real_exe_behind_npm_cmd_shim(self, tmp_path, monkeypatch):
        monkeypatch.delenv("HARNESS_CLAUDE_BIN", raising=False)
        shim = tmp_path / "claude.CMD"
        shim.write_text("")
        real = tmp_path / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        real.parent.mkdir(parents=True)
        real.write_text("")
        with patch("shutil.which", return_value=str(shim)):
            assert ex.resolve_claude_bin() == str(real)

    def test_uses_which_result(self, monkeypatch):
        monkeypatch.delenv("HARNESS_CLAUDE_BIN", raising=False)
        with patch("shutil.which", return_value="/usr/bin/claude"):
            assert ex.resolve_claude_bin() == "/usr/bin/claude"


class TestRunKillingTree:
    def test_normal_run(self, tmp_path):
        code = "import sys; d = sys.stdin.read(); print('got:' + d); sys.exit(3)"
        rc, out, err, timed_out = ex.run_killing_tree([sys.executable, "-c", code], input="한글",
                                                       cwd=str(tmp_path), timeout=30)
        assert rc == 3 and "got:한글" in out and not timed_out

    def test_timeout_kills_grandchildren(self, tmp_path):
        # 회귀: 직계 프로세스만 죽이면 파이프를 쥔 손자 프로세스가 끝날 때까지 기다리게 된다
        beat = tmp_path / "beat"
        t0 = time.monotonic()
        rc, _, _, timed_out = ex.run_killing_tree([sys.executable, "-c", heartbeat_code(beat)], input="",
                                                   cwd=str(tmp_path), timeout=2)
        assert timed_out and time.monotonic() - t0 < 15
        assert_heartbeat_stopped(beat)

    def test_interrupt_kills_tree_and_reraises(self, tmp_path, monkeypatch):
        # 회귀: Ctrl+C 등 타임아웃 외의 중단에서는 자식이 살아남아 무인 세션이 계속 파일을 고쳤다.
        # POSIX 에서는 자식이 별도 세션이라 터미널 Ctrl+C 가 자식에게 가지 않는다.
        beat = tmp_path / "beat"
        real = subprocess.Popen.communicate

        def interrupted(self, input=None, timeout=None):
            if timeout == 60:  # run_killing_tree 의 본 대기만 가로챈다
                time.sleep(1.5)  # 손자가 뛰기 시작할 때까지
                raise KeyboardInterrupt
            return real(self, input=input, timeout=timeout)

        monkeypatch.setattr(subprocess.Popen, "communicate", interrupted)
        with pytest.raises(KeyboardInterrupt):
            ex.run_killing_tree([sys.executable, "-c", heartbeat_code(beat)], input="",
                                cwd=str(tmp_path), timeout=60)
        assert_heartbeat_stopped(beat)


class TestSignalHandlers:
    def test_sigterm_becomes_system_exit(self):
        # SIGTERM 으로 죽을 때도 finally/except 경로를 타야 세션 프로세스 트리를 정리할 수 있다
        with pytest.raises(SystemExit):
            ex._exit_on_signal(15, None)


class TestInvokeClaude:
    def _run(self, executor, rc=0, **kw):
        kw.setdefault("session_id", "sid-1")
        with patch.object(ex, "run_killing_tree", return_value=(rc, '{"result": "ok"}', "", False)) as mock_run:
            res = executor._invoke_claude(2, "PROMPT", attempt=1, **kw)
        return res, mock_run

    def test_prompt_passed_via_stdin(self, executor):
        _, mock_run = self._run(executor)
        cmd = mock_run.call_args[0][0]
        assert "PROMPT" not in cmd
        assert mock_run.call_args[1]["input"] == "PROMPT"
        assert "-p" in cmd and "--output-format" in cmd

    def test_model_flag(self, executor):
        executor._model = "claude-opus-5-5"
        _, mock_run = self._run(executor)
        cmd = mock_run.call_args[0][0]
        assert cmd[cmd.index("--model") + 1] == "claude-opus-5-5"

    def test_new_session_uses_preassigned_id(self, executor):
        _, mock_run = self._run(executor)
        cmd = mock_run.call_args[0][0]
        assert cmd[cmd.index("--session-id") + 1] == "sid-1"
        assert "--resume" not in cmd

    def test_resume_flag(self, executor):
        _, mock_run = self._run(executor, resume=True)
        cmd = mock_run.call_args[0][0]
        assert cmd[cmd.index("--resume") + 1] == "sid-1"
        assert "--session-id" not in cmd

    def test_result_fields(self, executor):
        res, _ = self._run(executor, rc=3)
        assert res.exit_code == 3 and not res.timed_out

    def test_uses_configured_timeout(self, executor):
        executor._timeout = 42
        _, mock_run = self._run(executor)
        assert mock_run.call_args[1]["timeout"] == 42

    def test_timeout_is_caught(self, executor):
        with patch.object(ex, "run_killing_tree", return_value=(-1, "partial", "", True)):
            res = executor._invoke_claude(2, "P", attempt=1, session_id="s")
        assert res.timed_out

    def test_saves_attempt_output(self, executor):
        self._run(executor)
        data = read_json(executor._phase_dir / "step2-attempt1-output.json")
        assert data["step"] == 2 and data["exitCode"] == 0 and data["sessionId"] == "sid-1"

    def test_attempt_logs_not_overwritten_across_runs(self, executor):
        # 회귀: 재실행하면 attempt 번호가 1부터 다시 시작해 이전 실행의 실패 로그를 덮어썼다
        for out in ("RUN-1", "RUN-2"):
            with patch.object(ex, "run_killing_tree", return_value=(0, out, "", False)):
                executor._invoke_claude(2, "P", attempt=1, session_id="s")
        logs = sorted(executor._phase_dir.glob("step2-attempt*-output.json"))
        assert [read_json(p)["stdout"] for p in logs] == ["RUN-1", "RUN-2"]

    def test_parses_cost_and_turns(self, executor):
        stdout = '{"result": "ok", "total_cost_usd": 0.2008, "num_turns": 9}'
        with patch.object(ex, "run_killing_tree", return_value=(0, stdout, "", False)):
            res = executor._invoke_claude(2, "P", attempt=1, session_id="s")
        assert res.cost_usd == pytest.approx(0.2008) and res.num_turns == 9

    def test_missing_cost_is_zero(self, executor):
        # 타임아웃으로 죽은 세션은 결과 JSON이 없다
        with patch.object(ex, "run_killing_tree", return_value=(-1, "", "", True)):
            res = executor._invoke_claude(2, "P", attempt=1, session_id="s")
        assert res.cost_usd == 0.0 and res.num_turns == 0

    def test_mcp_isolated_by_default(self, executor):
        _, mock_run = self._run(executor)
        assert "--strict-mcp-config" in mock_run.call_args[0][0]

    def test_mcp_opt_in(self, executor):
        executor._mcp = True
        _, mock_run = self._run(executor)
        assert "--strict-mcp-config" not in mock_run.call_args[0][0]


# ---------------------------------------------------------------------------
# 검증
# ---------------------------------------------------------------------------

class TestRunAc:
    def test_all_pass(self, executor):
        executor._ac_shell = None
        with patch.object(ex, "run_killing_tree", return_value=(0, "ok", "", False)) as m:
            ok, fb = executor._run_ac(["a", "b"])
        assert ok and fb == "" and m.call_count == 2
        assert m.call_args[1]["shell"] is True

    def test_stops_at_first_failure_with_output_tail(self, executor):
        results = [(0, "", "", False), (1, "x" * 10000 + "REAL_ERROR", "", False)]
        with patch.object(ex, "run_killing_tree", side_effect=results) as m:
            ok, fb = executor._run_ac(["a", "b", "c"])
        assert not ok and m.call_count == 2
        assert "`b`" in fb and "REAL_ERROR" in fb
        assert len(fb) < 5000

    def test_timeout_is_failure(self, executor):
        with patch.object(ex, "run_killing_tree", return_value=(-1, "", "", True)):
            ok, fb = executor._run_ac(["a"])
        assert not ok and "타임아웃" in fb


class TestAcShell:
    def test_posix_uses_default_shell(self):
        assert ex.resolve_ac_shell(is_windows=False) is None

    def test_env_override(self, tmp_path, monkeypatch):
        bash = tmp_path / "bash.exe"
        bash.write_text("")
        monkeypatch.setenv("CLAUDE_CODE_GIT_BASH_PATH", str(bash))
        assert ex.resolve_ac_shell(is_windows=True) == str(bash)

    def test_env_override_to_missing_file_is_ignored(self, tmp_path, monkeypatch):
        # 없는 경로를 쓰면 AC 실행 때 트레이스백으로 죽는다
        monkeypatch.setenv("CLAUDE_CODE_GIT_BASH_PATH", str(tmp_path / "nope" / "bash.exe"))
        with patch("shutil.which", return_value=None):
            assert ex.resolve_ac_shell(is_windows=True) is None

    @pytest.mark.parametrize("git_rel", ["cmd/git.exe", "mingw64/bin/git.exe"])
    def test_finds_git_bash_next_to_git(self, tmp_path, monkeypatch, git_rel):
        monkeypatch.delenv("CLAUDE_CODE_GIT_BASH_PATH", raising=False)
        bash = tmp_path / "Git" / "bin" / "bash.exe"
        bash.parent.mkdir(parents=True)
        bash.write_text("")
        with patch("shutil.which", return_value=str(tmp_path / "Git" / git_rel)):
            assert ex.resolve_ac_shell(is_windows=True) == str(bash)

    def test_no_git_bash(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_GIT_BASH_PATH", raising=False)
        with patch("shutil.which", return_value=str(tmp_path / "git.exe")):
            assert ex.resolve_ac_shell(is_windows=True) is None

    def test_run_ac_uses_resolved_shell(self, executor):
        executor._ac_shell = "/x/bash"
        with patch.object(ex, "run_killing_tree", return_value=(0, "", "", False)) as m:
            executor._run_ac(["FOO=1 npm test"])
        assert m.call_args[0][0] == ["/x/bash", "-c", "FOO=1 npm test"]
        assert m.call_args[1]["shell"] is False

    def test_prompt_names_ac_shell(self, executor):
        executor._ac_shell = "/x/bash"
        index = read_json(executor._index_file)
        assert "`bash`" in executor._build_prompt(index["steps"][2], index)

    @pytest.mark.skipif(os.name != "nt", reason="Windows 전용")
    def test_bash_syntax_ac_passes_on_windows(self, executor):
        # 회귀: 세션(Git Bash)에서 통과한 AC 가 하네스(cmd.exe)에서는 실패해 시도만 소모했다
        executor._ac_shell = ex.resolve_ac_shell()
        ok, fb = executor._run_ac(['[ "$(echo hi)" = hi ]'])
        assert ok, fb


class TestValidateIndex:
    def _assert_rejected(self, tmp_project, steps, **index_fields):
        inst = make_executor(tmp_project, steps)
        if index_fields:
            idx = read_json(inst._index_file)
            idx.update(index_fields)
            write_json(inst._index_file, idx)
        with pytest.raises(SystemExit) as e:
            inst._validate_index()
        assert e.value.code == 1

    def test_missing_ac_exits(self, tmp_project):
        self._assert_rejected(tmp_project, [{"step": 0, "name": "a", "status": "pending"}])

    def test_empty_ac_exits(self, tmp_project):
        self._assert_rejected(tmp_project, [{"step": 0, "name": "a", "status": "pending", "ac": []}])

    def test_blank_skip_reason_exits(self, tmp_project):
        self._assert_rejected(tmp_project, [{"step": 0, "name": "a", "status": "pending", "skip_ac": " "}])

    @pytest.mark.parametrize("ac", [[""], ["  "], "npm test", [["npm", "test"]]])
    def test_malformed_ac_exits(self, tmp_project, ac):
        # 회귀: [""] 는 검증을 통과했고 실행하면 exit 0 이라 아무것도 확인하지 않고 completed 가 됐다
        self._assert_rejected(tmp_project, [{"step": 0, "name": "a", "status": "pending", "ac": ac}])

    def test_duplicate_step_numbers_exit(self, tmp_project):
        # 회귀: step 0 이 둘이면 한 번 실행으로 둘 다 completed 가 됐다
        self._assert_rejected(tmp_project, [{"step": 0, "name": "a", "status": "pending", "ac": ["x"]},
                                            {"step": 0, "name": "b", "status": "pending", "ac": ["x"]}])

    def test_gap_in_step_numbers_exits(self, tmp_project):
        self._assert_rejected(tmp_project, [{"step": 0, "name": "a", "status": "pending", "ac": ["x"]},
                                            {"step": 2, "name": "b", "status": "pending", "ac": ["x"]}])

    def test_unknown_status_exits(self, tmp_project):
        self._assert_rejected(tmp_project, [{"step": 0, "name": "a", "status": "in_progress", "ac": ["x"]}])

    def test_empty_steps_exits(self, tmp_project):
        self._assert_rejected(tmp_project, [])

    @pytest.mark.parametrize("timeout", ["3600", 0, -1])
    def test_invalid_timeout_exits(self, tmp_project, timeout):
        self._assert_rejected(tmp_project, [{"step": 0, "name": "a", "status": "pending", "ac": ["x"]}],
                              timeout_sec=timeout)

    @pytest.mark.parametrize("field, value", [
        ("max_cost_usd", "10"), ("max_cost_usd", 0), ("step_max_cost_usd", -1), ("step_max_cost_usd", True),
    ])
    def test_invalid_cost_cap_exits(self, tmp_project, field, value):
        self._assert_rejected(tmp_project, [{"step": 0, "name": "a", "status": "pending", "ac": ["x"]}],
                              **{field: value})

    def test_missing_step_file_exits(self, tmp_project):
        # 몇 시간 돈 뒤 그 step 차례에서야 멈추지 않도록 시작 전에 확인한다
        inst = make_executor(tmp_project, [{"step": 0, "name": "a", "status": "completed"},
                                           {"step": 1, "name": "b", "status": "pending", "ac": ["x"]}])
        (inst._phase_dir / "step1.md").unlink()
        with pytest.raises(SystemExit):
            inst._validate_index()

    def test_explicit_skip_ok(self, tmp_project):
        make_executor(tmp_project, [
            {"step": 0, "name": "a", "status": "pending", "skip_ac": "문서만 수정"},
        ])._validate_index()

    def test_completed_steps_ignored(self, tmp_project):
        inst = make_executor(tmp_project, [
            {"step": 0, "name": "a", "status": "completed"},
            {"step": 1, "name": "b", "status": "pending", "ac": ["npm test"]},
        ])
        (inst._phase_dir / "step0.md").unlink()  # 끝난 step 은 파일이 없어도 된다
        inst._validate_index()


class TestVerify:
    def _inv(self, **kw):
        base = dict(exit_code=0, timed_out=False, stderr="")
        base.update(kw)
        return ex.InvokeResult(**base)

    def _write_result(self, executor, data):
        write_json(executor._result_file(2), data)

    def _step(self, executor):
        return read_json(executor._index_file)["steps"][2]

    def _skip_step(self, executor):
        step = dict(self._step(executor))
        step.pop("ac")
        step["skip_ac"] = "문서 작업"
        return step

    def test_completed_and_ac_pass(self, executor):
        self._write_result(executor, {"status": "completed", "summary": "s"})
        with patch.object(executor, "_run_ac", return_value=(True, "")):
            status, fb, result = executor._verify(self._step(executor), self._inv())
        assert status == "completed" and result["summary"] == "s"

    def test_completed_but_ac_fails_is_retry(self, executor):
        self._write_result(executor, {"status": "completed", "summary": "s"})
        with patch.object(executor, "_run_ac", return_value=(False, "AC 실패: boom")):
            status, fb, _ = executor._verify(self._step(executor), self._inv())
        assert status == "retry" and "boom" in fb

    def test_missing_result_is_retry(self, executor):
        status, fb, _ = executor._verify(self._step(executor), self._inv(exit_code=1, stderr="crash"))
        assert status == "retry" and "result" in fb and "crash" in fb

    def test_timeout_is_retry(self, executor):
        status, fb, _ = executor._verify(self._step(executor), self._inv(timed_out=True))
        assert status == "retry" and "타임아웃" in fb

    def test_blocked(self, executor):
        self._write_result(executor, {"status": "blocked", "reason": "API 키 필요"})
        status, fb, _ = executor._verify(self._step(executor), self._inv())
        assert status == "blocked" and "API 키" in fb

    def test_model_error_is_gave_up(self, executor):
        self._write_result(executor, {"status": "error", "reason": "원인 불명"})
        status, fb, _ = executor._verify(self._step(executor), self._inv())
        assert status == "gave_up" and "원인 불명" in fb

    def test_invalid_json_is_retry(self, executor):
        executor._result_file(2).write_text("{not json", encoding="utf-8")
        status, _, _ = executor._verify(self._step(executor), self._inv())
        assert status == "retry"

    @pytest.mark.parametrize("raw", [
        [],                                               # 객체가 아님
        "completed",
        {"status": "done", "summary": "s"},               # 알 수 없는 status
        {"status": "completed", "summary": {"a": 1}},     # 문자열이 아닌 summary
        {"status": "completed", "summary": "s", "handoff": {"k": "v"}},
    ])
    def test_malformed_result_is_retry_with_feedback(self, executor, raw):
        # 회귀: 문법상 유효한 JSON 이라도 형태가 다르면 result.get() 에서 하네스 전체가 죽었다
        self._write_result(executor, raw)
        status, fb, _ = executor._verify(self._step(executor), self._inv())
        assert status == "retry" and "형식" in fb

    def test_handoff_list_is_joined(self, executor):
        self._write_result(executor, {"status": "completed", "summary": "s", "handoff": ["결정1", "결정2"]})
        with patch.object(executor, "_run_ac", return_value=(True, "")):
            status, _, result = executor._verify(self._step(executor), self._inv())
        assert status == "completed" and result["handoff"] == "결정1\n결정2"

    def test_skip_ac_with_clean_exit_completes(self, executor):
        self._write_result(executor, {"status": "completed", "summary": "s"})
        with patch.object(executor, "_run_ac") as m:
            status, _, _ = executor._verify(self._skip_step(executor), self._inv())
        assert status == "completed" and not m.called

    def test_skip_ac_with_nonzero_exit_is_retry(self, executor):
        self._write_result(executor, {"status": "completed", "summary": "s"})
        status, fb, _ = executor._verify(self._skip_step(executor), self._inv(exit_code=1))
        assert status == "retry" and "exit 1" in fb

    def test_skip_ac_with_timeout_is_retry(self, executor):
        self._write_result(executor, {"status": "completed", "summary": "s"})
        status, _, _ = executor._verify(self._skip_step(executor), self._inv(timed_out=True))
        assert status == "retry"


# ---------------------------------------------------------------------------
# step 실행 루프
# ---------------------------------------------------------------------------

class TestExecuteSingleStep:
    """_invoke_claude 는 result 파일을 쓰는 가짜로, _run_ac 는 mock 으로 대체.
    runs 원소: {"result": dict|None, "exit": int, "timeout": bool}"""

    OK = {"result": {"status": "completed", "summary": "ok"}}

    def _fake_invoke(self, executor, runs):
        calls = []

        def fake(step_num, prompt, *, attempt, session_id, resume=False):
            saved = next(s for s in read_json(executor._index_file)["steps"] if s["step"] == step_num)
            calls.append({"prompt": prompt, "session_id": session_id, "resume": resume,
                          "saved_session_id": saved.get("session_id")})
            run = runs[len(calls) - 1]
            if run.get("result") is not None:
                write_json(executor._result_file(step_num), run["result"])
            return ex.InvokeResult(exit_code=run.get("exit", 0), timed_out=run.get("timeout", False), stderr="",
                                   cost_usd=run.get("cost", 0.0), num_turns=run.get("turns", 0))

        executor._invoke_claude = fake
        return calls

    def test_accumulates_cost_and_turns_across_attempts(self, executor):
        self._fake_invoke(executor, [dict(self.OK, cost=0.25, turns=9), dict(self.OK, cost=0.125, turns=3)])
        with patch.object(executor, "_run_ac", side_effect=[(False, "AC 실패: x"), (True, "")]):
            executor._execute_single_step(self._step(executor))
        s = read_json(executor._index_file)["steps"][2]
        assert s["cost_usd"] == pytest.approx(0.375) and s["num_turns"] == 12

    def test_cost_recorded_even_when_step_fails(self, executor):
        self._fake_invoke(executor, [dict(self.OK, cost=0.5, turns=1)] * 3)
        with patch.object(executor, "_run_ac", return_value=(False, "AC 실패: x")):
            with pytest.raises(SystemExit):
                executor._execute_single_step(self._step(executor))
        assert read_json(executor._index_file)["steps"][2]["cost_usd"] == pytest.approx(1.5)

    def test_cost_accumulates_on_top_of_previous_runs(self, executor):
        executor._update_step(2, cost_usd=1.0, num_turns=5)
        self._fake_invoke(executor, [dict(self.OK, cost=0.5, turns=2)])
        with patch.object(executor, "_run_ac", return_value=(True, "")):
            executor._execute_single_step(self._step(executor))
        s = read_json(executor._index_file)["steps"][2]
        assert s["cost_usd"] == pytest.approx(1.5) and s["num_turns"] == 7

    def _step(self, executor):
        return read_json(executor._index_file)["steps"][2]

    def test_first_try_success(self, executor):
        calls = self._fake_invoke(executor, [{"result": {"status": "completed", "summary": "UI 완료", "handoff": "h"}}])
        with patch.object(executor, "_run_ac", return_value=(True, "")):
            executor._execute_single_step(self._step(executor))
        s = read_json(executor._index_file)["steps"][2]
        assert s["status"] == "completed" and s["summary"] == "UI 완료" and s["handoff"] == "h"
        assert "completed_at" in s and s["attempts"] == 1
        assert not executor._result_file(2).exists()
        assert len(calls) == 1 and calls[0]["resume"] is False

    def test_session_id_saved_before_invocation(self, executor):
        calls = self._fake_invoke(executor, [self.OK])
        with patch.object(executor, "_run_ac", return_value=(True, "")):
            executor._execute_single_step(self._step(executor))
        assert calls[0]["session_id"] and calls[0]["saved_session_id"] == calls[0]["session_id"]

    def test_retry_resumes_session_with_ac_feedback(self, executor):
        calls = self._fake_invoke(executor, [self.OK, self.OK])
        with patch.object(executor, "_run_ac", side_effect=[(False, "AC 실패: TypeError"), (True, "")]):
            executor._execute_single_step(self._step(executor))
        assert calls[1]["resume"] and calls[1]["session_id"] == calls[0]["session_id"]
        assert "TypeError" in calls[1]["prompt"]
        assert "UI를 구현하세요" not in calls[1]["prompt"]  # 재개 시 전체 프롬프트 재전송 안 함
        s = read_json(executor._index_file)["steps"][2]
        assert s["status"] == "completed" and s["attempts"] == 2

    def test_timeout_on_first_attempt_resumes_same_session(self, executor):
        calls = self._fake_invoke(executor, [{"result": None, "exit": -1, "timeout": True}, self.OK])
        with patch.object(executor, "_run_ac", return_value=(True, "")):
            executor._execute_single_step(self._step(executor))
        assert calls[1]["resume"] and calls[1]["session_id"] == calls[0]["session_id"]
        assert "타임아웃" in calls[1]["prompt"]

    def test_failed_resume_falls_back_to_new_session(self, executor):
        calls = self._fake_invoke(executor, [
            {"result": None, "exit": 0},       # 결과 보고 누락 → 재개 시도
            {"result": None, "exit": 1},       # 재개 자체가 실패 (세션 없음 등)
            self.OK,
        ])
        with patch.object(executor, "_run_ac", return_value=(True, "")):
            executor._execute_single_step(self._step(executor))
        assert calls[1]["resume"]
        assert calls[2]["resume"] is False
        assert calls[2]["session_id"] != calls[0]["session_id"]
        assert calls[2]["saved_session_id"] == calls[2]["session_id"]
        assert "UI를 구현하세요" in calls[2]["prompt"] and "이전 시도" in calls[2]["prompt"]

    def test_session_error_report_retries_in_new_session(self, executor):
        # 스스로 포기한 세션을 다시 깨우지 않고 새 세션에 맡긴다
        calls = self._fake_invoke(executor, [{"result": {"status": "error", "reason": "막힘: X"}}, self.OK])
        with patch.object(executor, "_run_ac", return_value=(True, "")):
            executor._execute_single_step(self._step(executor))
        assert calls[1]["resume"] is False and calls[1]["session_id"] != calls[0]["session_id"]
        assert "UI를 구현하세요" in calls[1]["prompt"] and "막힘: X" in calls[1]["prompt"]

    def test_step_cost_cap_stops_retries(self, executor):
        executor._step_max_cost = 0.5
        calls = self._fake_invoke(executor, [dict(self.OK, cost=0.3)] * 3)
        with patch.object(executor, "_run_ac", return_value=(False, "AC 실패: x")):
            with pytest.raises(SystemExit) as e:
                executor._execute_single_step(self._step(executor))
        assert e.value.code == 1 and len(calls) == 2
        s = read_json(executor._index_file)["steps"][2]
        assert s["status"] == "error" and "비용 상한" in s["error_message"]

    def test_phase_cost_cap_stops_before_starting(self, executor):
        executor._max_cost = 1.0
        executor._update_step(0, cost_usd=0.7)
        executor._update_step(1, cost_usd=0.5)  # 앞선 step 들이 이미 예산을 다 썼다
        calls = self._fake_invoke(executor, [self.OK])
        with pytest.raises(SystemExit):
            executor._execute_single_step(self._step(executor))
        assert calls == []
        assert "비용 상한" in read_json(executor._index_file)["steps"][2]["error_message"]

    def test_completed_step_not_failed_by_cap(self, executor):
        executor._step_max_cost = 0.1
        self._fake_invoke(executor, [dict(self.OK, cost=0.5)])
        with patch.object(executor, "_run_ac", return_value=(True, "")):
            executor._execute_single_step(self._step(executor))
        assert read_json(executor._index_file)["steps"][2]["status"] == "completed"

    def test_interrupted_run_resumes_saved_session(self, executor):
        executor._update_step(2, session_id="old-sess", started_at="t")
        calls = self._fake_invoke(executor, [self.OK])
        with patch.object(executor, "_run_ac", return_value=(True, "")):
            executor._execute_single_step(self._step(executor))
        assert calls[0]["resume"] and calls[0]["session_id"] == "old-sess"
        assert "중단" in calls[0]["prompt"]

    def test_stale_result_file_removed_before_attempt(self, executor):
        write_json(executor._result_file(2), {"status": "completed", "summary": "stale"})
        self._fake_invoke(executor, [{"result": None}] * 3)
        with pytest.raises(SystemExit):
            executor._execute_single_step(self._step(executor))
        assert read_json(executor._index_file)["steps"][2]["status"] == "error"

    def test_blocked_exits_2(self, executor, top_index):
        self._fake_invoke(executor, [{"result": {"status": "blocked", "reason": "API 키"}}])
        with pytest.raises(SystemExit) as e:
            executor._execute_single_step(self._step(executor))
        assert e.value.code == 2
        s = read_json(executor._index_file)["steps"][2]
        assert s["status"] == "blocked" and s["blocked_reason"] == "API 키" and "blocked_at" in s
        assert read_json(top_index)["phases"][0]["status"] == "blocked"

    def test_error_after_max_attempts(self, executor, top_index):
        calls = self._fake_invoke(executor, [self.OK] * 3)
        with patch.object(executor, "_run_ac", return_value=(False, "AC 실패: nope")):
            with pytest.raises(SystemExit) as e:
                executor._execute_single_step(self._step(executor))
        assert e.value.code == 1 and len(calls) == ex.StepExecutor.MAX_ATTEMPTS
        s = read_json(executor._index_file)["steps"][2]
        assert s["status"] == "error" and "nope" in s["error_message"] and "failed_at" in s
        assert read_json(top_index)["phases"][0]["status"] == "error"

    def test_records_model(self, executor):
        executor._model = "claude-opus-5-5"
        self._fake_invoke(executor, [self.OK])
        with patch.object(executor, "_run_ac", return_value=(True, "")):
            executor._execute_single_step(self._step(executor))
        assert read_json(executor._index_file)["steps"][2]["model"] == "claude-opus-5-5"

    def test_failed_step_changes_are_committed_as_wip(self, executor):
        msgs = []
        executor._git_commit = lambda msg, *paths: msgs.append(msg)
        executor._run_git = lambda *a: MagicMock(returncode=1 if a[:3] == ("diff", "--cached", "--quiet") else 0,
                                                 stdout="", stderr="")
        self._fake_invoke(executor, [{"result": {"status": "blocked", "reason": "r"}}])
        with pytest.raises(SystemExit):
            executor._execute_single_step(self._step(executor))
        assert any(m.startswith("wip(mvp)") for m in msgs)


# ---------------------------------------------------------------------------
# 상태 점검 / 복구
# ---------------------------------------------------------------------------

class TestCheckBlockers:
    def test_error_step_exits_1(self, tmp_project):
        inst = make_executor(tmp_project, [
            {"step": 0, "name": "ok", "status": "completed"},
            {"step": 1, "name": "bad", "status": "error", "error_message": "fail"},
        ])
        with pytest.raises(SystemExit) as e:
            inst._check_blockers()
        assert e.value.code == 1

    def test_blocked_step_exits_2(self, tmp_project):
        inst = make_executor(tmp_project, [
            {"step": 0, "name": "ok", "status": "completed"},
            {"step": 1, "name": "stuck", "status": "blocked", "blocked_reason": "API key"},
        ])
        with pytest.raises(SystemExit) as e:
            inst._check_blockers()
        assert e.value.code == 2

    def test_error_before_completed_is_detected(self, tmp_project):
        # 회귀: 뒤에서부터 보다가 completed에서 멈춰 앞선 error를 놓치던 버그
        inst = make_executor(tmp_project, [
            {"step": 0, "name": "bad", "status": "error"},
            {"step": 1, "name": "ok", "status": "completed"},
        ])
        with pytest.raises(SystemExit) as e:
            inst._check_blockers()
        assert e.value.code == 1

    def test_error_before_completed_and_pending(self, tmp_project):
        inst = make_executor(tmp_project, [
            {"step": 0, "name": "bad", "status": "error"},
            {"step": 1, "name": "ok", "status": "completed"},
            {"step": 2, "name": "next", "status": "pending"},
        ])
        with pytest.raises(SystemExit):
            inst._check_blockers()

    def test_all_fine(self, tmp_project):
        make_executor(tmp_project, [
            {"step": 0, "name": "ok", "status": "completed"},
            {"step": 1, "name": "next", "status": "pending"},
        ])._check_blockers()


class TestResetFailed:
    def test_resets_error_and_blocked(self, tmp_project):
        inst = make_executor(tmp_project, [
            {"step": 0, "name": "a", "status": "error", "error_message": "e", "failed_at": "t"},
            {"step": 1, "name": "b", "status": "blocked", "blocked_reason": "r", "blocked_at": "t"},
            {"step": 2, "name": "c", "status": "completed", "summary": "s"},
            {"step": 3, "name": "d", "status": "error", "session_id": "old"},
        ])
        inst._reset_failed()
        steps = read_json(inst._index_file)["steps"]
        assert steps[0] == {"step": 0, "name": "a", "status": "pending"}
        assert steps[1] == {"step": 1, "name": "b", "status": "pending"}
        assert steps[2]["status"] == "completed"
        assert "session_id" not in steps[3]  # 사람이 고친 뒤이므로 이전 세션을 이어받지 않는다


class TestFinalize:
    def test_refuses_when_not_all_completed(self, tmp_project):
        inst = make_executor(tmp_project, [
            {"step": 0, "name": "a", "status": "error"},
            {"step": 1, "name": "b", "status": "completed"},
        ])
        inst._run_git = MagicMock(return_value=MagicMock(returncode=0))
        with pytest.raises(SystemExit) as e:
            inst._finalize()
        assert e.value.code == 1
        assert "completed_at" not in read_json(inst._index_file)

    def test_records_phase_cost_total(self, tmp_project, capsys):
        inst = make_executor(tmp_project, [
            {"step": 0, "name": "a", "status": "completed", "cost_usd": 0.2, "num_turns": 9},
            {"step": 1, "name": "b", "status": "completed", "cost_usd": 0.3, "num_turns": 6},
            {"step": 2, "name": "c", "status": "completed"},
        ])
        inst._run_git = MagicMock(return_value=MagicMock(returncode=0))
        inst._finalize()
        idx = read_json(inst._index_file)
        assert idx["cost_usd"] == pytest.approx(0.5) and idx["num_turns"] == 15
        assert "$0.50" in capsys.readouterr().out

    def test_marks_completed(self, tmp_project, top_index):
        d = tmp_project / "phases" / "0-mvp"
        d.mkdir()
        write_json(d / "index.json", {"project": "T", "phase": "mvp",
                                      "steps": [{"step": 0, "name": "a", "status": "completed"}]})
        inst = ex.StepExecutor("0-mvp", root=tmp_project)
        inst._run_git = MagicMock(return_value=MagicMock(returncode=0))
        inst._finalize()
        assert "completed_at" in read_json(inst._index_file)
        assert read_json(top_index)["phases"][0]["status"] == "completed"


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------

class TestDirtyPaths:
    def test_ignores_phases_dir(self):
        porcelain = " M phases/0-mvp/index.json\n?? phases/0-mvp/step0.md\n"
        assert ex.dirty_paths_outside_phases(porcelain) == []

    def test_reports_other_paths(self):
        porcelain = " M src/app.ts\n?? notes.txt\n M phases/x/index.json\n"
        assert ex.dirty_paths_outside_phases(porcelain) == ["src/app.ts", "notes.txt"]

    def test_rename(self):
        assert ex.dirty_paths_outside_phases("R  old.ts -> new.ts\n") == ["new.ts"]

    def test_quoted_path(self):
        assert ex.dirty_paths_outside_phases('?? "phases/\\355\\225\\234.md"\n') == []


class TestEnsureCleanTree:
    def test_dirty_outside_phases_exits(self, executor):
        executor._run_git = MagicMock(return_value=MagicMock(returncode=0, stdout=" M src/a.ts\n"))
        with pytest.raises(SystemExit) as e:
            executor._ensure_clean_tree()
        assert e.value.code == 1

    def test_only_phases_dirty_ok(self, executor):
        executor._run_git = MagicMock(return_value=MagicMock(returncode=0, stdout="?? phases/0-mvp/x.md\n"))
        executor._ensure_clean_tree()


class TestCheckoutBranch:
    def _mock_git(self, executor, responses):
        it = iter(responses)
        executor._run_git = lambda *a: next(it, MagicMock(returncode=0, stdout="", stderr=""))

    def test_already_on_branch(self, executor):
        self._mock_git(executor, [MagicMock(returncode=0, stdout="feat-mvp\n", stderr="")])
        executor._checkout_branch()

    def test_branch_not_exists_create(self, executor):
        self._mock_git(executor, [
            MagicMock(returncode=0, stdout="main\n", stderr=""),
            MagicMock(returncode=1, stdout="", stderr="not found"),
            MagicMock(returncode=0, stdout="", stderr=""),
        ])
        executor._checkout_branch()

    def test_checkout_fails_exits(self, executor):
        self._mock_git(executor, [
            MagicMock(returncode=0, stdout="main\n", stderr=""),
            MagicMock(returncode=1, stdout="", stderr=""),
            MagicMock(returncode=1, stdout="", stderr="dirty tree"),
        ])
        with pytest.raises(SystemExit):
            executor._checkout_branch()

    def test_no_git_exits(self, executor):
        self._mock_git(executor, [MagicMock(returncode=128, stdout="", stderr="not a git repo")])
        with pytest.raises(SystemExit):
            executor._checkout_branch()


class TestCommitStep:
    """실제 git 으로 커밋 분리를 확인한다."""

    def _executor(self, repo):
        plan(repo, [{"step": 0, "name": "a", "status": "pending", "ac": [AC_OK]}])
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "plan")
        return ex.StepExecutor("p", root=repo)

    def test_code_commit_excludes_phase_index(self, repo):
        e = self._executor(repo)
        (repo / "src").mkdir()
        (repo / "src" / "a.ts").write_text("x", encoding="utf-8")
        e._update_step(0, session_id="s")
        e._commit_code(0, "a", "feat")
        assert commit_files(repo, "feat(p)") == ["src/a.ts"]
        e._commit_meta(0)
        assert commit_files(repo, "chore(p): step 0") == ["phases/p/index.json"]
        assert git(repo, "status", "--porcelain") == ""

    def test_no_code_changes_skips_code_commit(self, repo):
        e = self._executor(repo)
        e._update_step(0, session_id="s")
        e._commit_code(0, "a", "feat")
        assert git(repo, "log", "--format=%s", "--grep=^feat").strip() == ""
        assert git(repo, "status", "--porcelain").strip() == "M phases/p/index.json"

    def test_wip_prefix(self, repo):
        e = self._executor(repo)
        (repo / "a.txt").write_text("x", encoding="utf-8")
        e._commit_code(0, "a", "wip")
        assert git(repo, "log", "-1", "--format=%s").startswith("wip(p): step 0")

    def test_plan_commit_leaves_other_staged_changes(self, repo):
        # 중단된 세션의 작업이 스테이징돼 있어도 계획 커밋에는 phases/ 만 들어간다
        plan(repo, [{"step": 0, "name": "a", "status": "pending", "ac": [AC_OK]}])
        (repo / "half.ts").write_text("x", encoding="utf-8")
        git(repo, "add", "half.ts")
        ex.StepExecutor("p", root=repo)._commit_plan_files()
        assert all(f.startswith("phases/") for f in commit_files(repo, "chore(p): phase plan"))
        assert git(repo, "status", "--porcelain").strip() == "?? half.ts"  # 작업은 트리에 남는다

    def test_commit_failure_exits(self, executor):
        def fake_git(*args):
            if args[:3] == ("diff", "--cached", "--quiet"):
                return MagicMock(returncode=1)
            if args[0] == "commit":
                return MagicMock(returncode=1, stdout="", stderr="hook failed")
            return MagicMock(returncode=0, stdout="", stderr="")
        executor._run_git = fake_git
        with pytest.raises(SystemExit) as e:
            executor._commit_code(2, "ui", "feat")
        assert e.value.code == 1


# ---------------------------------------------------------------------------
# top-level index
# ---------------------------------------------------------------------------

    def test_diff_error_is_not_treated_as_no_changes(self, executor):
        # git diff --quiet 는 0=변경 없음, 1=변경 있음, 그 외=오류다
        executor._run_git = MagicMock(return_value=MagicMock(returncode=128, stdout="", stderr="fatal: bad"))
        with pytest.raises(SystemExit):
            executor._has_staged()

class TestUpdateTopIndex:
    def test_completed(self, executor, top_index):
        executor._update_top_index("completed")
        p = read_json(top_index)["phases"][0]
        assert p["status"] == "completed" and "completed_at" in p

    def test_other_phases_unchanged(self, executor, top_index):
        executor._update_top_index("error")
        assert read_json(top_index)["phases"][1]["status"] == "pending"

    def test_no_top_index_file(self, executor):
        executor._update_top_index("completed")  # should not raise


# ---------------------------------------------------------------------------
# 기타
# ---------------------------------------------------------------------------

class TestProgressIndicator:
    def test_elapsed(self):
        import time
        with ex.progress_indicator("test") as pi:
            time.sleep(0.15)
        assert pi.elapsed >= 0.1


class TestConfigureStdio:
    def test_symbols_printable_on_cp949_pipe(self):
        # 회귀: Windows에서 출력을 파이프로 받으면 cp949로 '↻' 등을 인코딩하다 크래시
        import io
        buf = io.BytesIO()
        stream = io.TextIOWrapper(buf, encoding="cp949")
        with patch("sys.stdout", stream), patch("sys.stderr", stream):
            ex.configure_stdio()
            print("↻ ✓ ✗ ⏸ 한글")
            stream.flush()
        assert "↻ ✓ ✗ ⏸ 한글" in buf.getvalue().decode("utf-8")


class TestMainCli:
    def test_no_args_exits(self):
        with patch("sys.argv", ["execute.py"]):
            with pytest.raises(SystemExit) as e:
                ex.main()
            assert e.value.code == 2

    def test_invalid_phase_dir_exits(self, tmp_path):
        with patch("sys.argv", ["execute.py", "nonexistent"]), patch.object(ex, "ROOT", tmp_path):
            with pytest.raises(SystemExit) as e:
                ex.main()
            assert e.value.code == 1

    def test_ctrl_c_exits_130_with_resume_hint(self, tmp_project, phase_dir, capsys):
        with patch("sys.argv", ["execute.py", "0-mvp"]), patch.object(ex, "ROOT", tmp_project), \
                patch.object(ex.StepExecutor, "run", side_effect=KeyboardInterrupt):
            with pytest.raises(SystemExit) as e:
                ex.main()
        assert e.value.code == 130 and "0-mvp --resume" in capsys.readouterr().out

    def test_resume_flag_passed_to_run(self, tmp_project, phase_dir):
        with patch("sys.argv", ["execute.py", "0-mvp", "--resume"]), patch.object(ex, "ROOT", tmp_project), \
                patch.object(ex.StepExecutor, "run") as run:
            ex.main()
        assert run.call_args.kwargs["resume"] is True

    def test_missing_index_exits(self, tmp_project):
        (tmp_project / "phases" / "empty").mkdir()
        with patch("sys.argv", ["execute.py", "empty"]), patch.object(ex, "ROOT", tmp_project):
            with pytest.raises(SystemExit) as e:
                ex.main()
            assert e.value.code == 1


# ---------------------------------------------------------------------------
# 통합: 실제 git 저장소에서 run() 전체 경로
# ---------------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path):
    """실제 git 저장소. 하네스 템플릿의 .gitignore 를 쓰고 main 에 초기 커밋이 있다."""
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    git(tmp_path, "config", "commit.gpgsign", "false")
    shutil.copy(REPO / ".gitignore", tmp_path / ".gitignore")
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def commit_files(root: Path, subject_prefix: str) -> list:
    """제목이 subject_prefix 로 시작하는 가장 최근 커밋의 파일 목록."""
    sha = git(root, "log", "-1", "--format=%H", f"--grep=^{subject_prefix}")
    assert sha.strip(), f"'{subject_prefix}' 커밋이 없다"
    return git(root, "show", "--name-only", "--format=", sha.strip()).split()


class TestRunInterrupted:
    """하네스가 세션 도중 죽은 뒤 다시 실행하는 경로 (harness.md '중단 복구')."""

    def _interrupted(self, repo, with_changes=True):
        d = plan(repo, [{"step": 0, "name": "a", "status": "pending", "ac": [AC_OK]}])
        git(repo, "checkout", "-q", "-b", "feat-p")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "chore(p): phase plan")
        idx = read_json(d / "index.json")
        idx["steps"][0].update(session_id="sess-1", started_at="t")
        write_json(d / "index.json", idx)
        if with_changes:
            (repo / "src").mkdir()
            (repo / "src" / "half.ts").write_text("// half", encoding="utf-8")  # 세션이 하다 만 작업

    def test_resumes_interrupted_step_with_its_changes(self, repo):
        self._interrupted(repo)
        e = ex.StepExecutor("p", root=repo)
        calls = install_fake_claude(e, [{"files": {"src/half.ts": "// done"}, "result": OK}])
        e.run(resume=True)
        assert calls[0]["resume"] and calls[0]["session_id"] == "sess-1"
        assert "src/half.ts" in commit_files(repo, "feat(p)")
        assert git(repo, "status", "--porcelain") == ""

    def test_interrupted_dirty_tree_requires_resume_flag(self, repo, capsys):
        # 회귀: 중단 뒤 사용자가 만든 파일까지 세션 작업으로 보고 feat 커밋에 넣었다.
        # 남은 변경을 보여 주고, --resume 으로 확인받은 뒤에만 이어받는다.
        self._interrupted(repo)
        (repo / "user-notes.txt").write_text("메모", encoding="utf-8")
        e = ex.StepExecutor("p", root=repo)
        calls = install_fake_claude(e, [{"result": OK}])
        with pytest.raises(SystemExit) as se:
            e.run()
        out = capsys.readouterr().out
        assert se.value.code == 1 and calls == []
        assert "src/half.ts" in out and "user-notes.txt" in out and "--resume" in out

    def test_interrupted_clean_tree_resumes_without_flag(self, repo):
        # 남은 변경이 없으면 섞일 것도 없으니 그냥 다시 실행하면 된다
        self._interrupted(repo, with_changes=False)
        e = ex.StepExecutor("p", root=repo)
        calls = install_fake_claude(e, [{"result": OK}])
        e.run()
        assert calls[0]["resume"] and calls[0]["session_id"] == "sess-1"

    def test_user_changes_without_interrupted_step_still_refused(self, repo):
        plan(repo, [{"step": 0, "name": "a", "status": "pending", "ac": [AC_OK]}])
        (repo / "notes.txt").write_text("사용자 작업", encoding="utf-8")
        e = ex.StepExecutor("p", root=repo)
        calls = install_fake_claude(e, [{"result": OK}])
        with pytest.raises(SystemExit) as se:
            e.run()
        assert se.value.code == 1 and calls == []

    def test_interrupted_step_on_other_branch_still_refused(self, repo):
        # 중단 흔적이 있어도 지금 브랜치가 phase 브랜치가 아니면 그 변경은 세션 작업이라고 볼 수 없다
        plan(repo, [{"step": 0, "name": "a", "status": "pending", "ac": [AC_OK],
                     "session_id": "sess-1", "started_at": "t"}])
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "plan on main")
        (repo / "notes.txt").write_text("사용자 작업", encoding="utf-8")
        assert git(repo, "branch", "--show-current").strip() == "main"
        e = ex.StepExecutor("p", root=repo)
        calls = install_fake_claude(e, [{"result": OK}])
        with pytest.raises(SystemExit):
            e.run()
        assert calls == []

    def test_crash_while_committing_success_can_resume(self, repo):
        # 성공한 step 을 커밋하는 도중 죽어도, 다시 실행하면 이어서 completed 가 된다
        plan(repo, [{"step": 0, "name": "a", "status": "pending", "ac": [AC_OK]}])
        e = ex.StepExecutor("p", root=repo)
        install_fake_claude(e, [{"files": {"src/a.ts": "x"}, "result": OK}])
        real_commit = e._git_commit

        def crash_on_feat(msg, *paths):
            if msg.startswith("feat("):
                raise KeyboardInterrupt
            real_commit(msg, *paths)

        e._git_commit = crash_on_feat
        with pytest.raises(KeyboardInterrupt):
            e.run()

        e2 = ex.StepExecutor("p", root=repo)
        install_fake_claude(e2, [{"result": OK}])
        e2.run(resume=True)
        assert read_json(e2._index_file)["steps"][0]["status"] == "completed"
        assert "src/a.ts" in commit_files(repo, "feat(p)")
        assert git(repo, "status", "--porcelain") == ""

    def test_blocked_leaves_clean_tree_including_top_index(self, repo):
        plan(repo, [{"step": 0, "name": "a", "status": "pending", "ac": [AC_OK]}])
        write_json(repo / "phases" / "index.json", {"phases": [{"dir": "p", "status": "pending"}]})
        e = ex.StepExecutor("p", root=repo)
        install_fake_claude(e, [{"files": {"src/a.ts": "x"},
                                 "result": {"status": "blocked", "reason": "API 키"}}])
        with pytest.raises(SystemExit) as se:
            e.run()
        assert se.value.code == 2
        assert read_json(repo / "phases" / "index.json")["phases"][0]["status"] == "blocked"
        assert git(repo, "status", "--porcelain") == ""


class TestRunSessionReports:
    def test_handoff_list_does_not_stall_next_step(self, repo):
        # 회귀: handoff 배열이 index.json 에 그대로 저장돼, 다음 step 프롬프트 생성에서 매번 죽었다
        plan(repo, [{"step": 0, "name": "a", "status": "pending", "ac": [AC_OK]},
                    {"step": 1, "name": "b", "status": "pending", "ac": [AC_OK]}])
        e = ex.StepExecutor("p", root=repo)
        calls = install_fake_claude(e, [
            {"result": {"status": "completed", "summary": "s", "handoff": ["결정1", "결정2"]}},
            {"result": OK},
        ])
        e.run()
        assert len(calls) == 2 and "결정2" in calls[1]["prompt"]
        assert read_json(e._index_file)["steps"][0]["handoff"] == "결정1\n결정2"


# ---------------------------------------------------------------------------
# 비밀값 파일
# ---------------------------------------------------------------------------

class TestGitignore:
    @pytest.mark.parametrize("path, ignored", [
        (".env", True), (".env.local", True), ("app/.env.production", True), (".env.example", False),
        ("phases/0-mvp/index.json.tmp", True),
    ])
    def test_env_files(self, path, ignored):
        r = subprocess.run(["git", "check-ignore", "-q", "--no-index", path], cwd=REPO)
        assert (r.returncode == 0) == ignored


class TestIsSecretPath:
    @pytest.mark.parametrize("path", [
        ".env", ".env.local", "config/.env.production", ".envrc", "certs/server.pem", "deploy.key", "id_rsa",
    ])
    def test_detected(self, path):
        assert ex.is_secret_path(path)

    @pytest.mark.parametrize("path", [".env.example", "src/env.ts", "environment.md", "keys.ts", "docs/.envrc.md"])
    def test_not_detected(self, path):
        assert not ex.is_secret_path(path)


class TestRunSecrets:
    def test_secret_files_never_committed(self, repo, capsys):
        # 대상 프로젝트의 .gitignore 에 .env 규칙이 없어도 하네스 자동 커밋에는 들어가지 않는다
        (repo / ".gitignore").write_text("phases/**/step*-output.json\nphases/**/step*-result.json\n",
                                         encoding="utf-8")
        git(repo, "commit", "-qam", "project gitignore")
        plan(repo, [{"step": 0, "name": "a", "status": "pending", "ac": [AC_OK]}])
        e = ex.StepExecutor("p", root=repo)
        install_fake_claude(e, [{"files": {"src/a.ts": "x", ".env.local": "API_KEY=secret"}, "result": OK}])
        e.run()
        committed = git(repo, "log", "--all", "--name-only", "--format=")
        assert "src/a.ts" in committed and ".env.local" not in committed
        assert ".env.local" in capsys.readouterr().out


    def test_secret_in_phases_not_in_plan_commit(self, repo):
        # 회귀: 계획 커밋은 비밀값 필터를 거치지 않았다
        plan(repo, [{"step": 0, "name": "a", "status": "pending", "ac": [AC_OK]}])
        (repo / "phases" / "p" / "deploy.pem").write_text("KEY", encoding="utf-8")
        e = ex.StepExecutor("p", root=repo)
        install_fake_claude(e, [{"result": OK}])
        e.run()
        assert "phases/p/deploy.pem" not in git(repo, "log", "--all", "--name-only", "--format=")

    def test_deleting_tracked_secret_is_committed(self, repo):
        # 회귀: 비밀값 파일을 지우는 변경까지 커밋에서 빼서 HEAD 에 남았다
        (repo / "deploy.pem").write_text("KEY", encoding="utf-8")
        git(repo, "add", "-f", "deploy.pem")
        git(repo, "commit", "-qm", "pem")
        plan(repo, [{"step": 0, "name": "a", "status": "pending", "ac": [AC_OK]}])
        e = ex.StepExecutor("p", root=repo)
        install_fake_claude(e, [{"do": lambda root: (root / "deploy.pem").unlink(), "result": OK}])
        e.run()
        assert "deploy.pem" not in git(repo, "ls-tree", "--name-only", "HEAD")
        assert git(repo, "status", "--porcelain") == ""

class TestRunBranchConfig:
    def test_validates_config_of_phase_branch(self, repo):
        # 회귀: 브랜치 전환 전의 index.json 으로 검증하고, 전환 뒤의 다른 index.json 으로 실행했다
        plan(repo, [{"step": 0, "name": "a", "status": "pending"}])  # ac 없음
        git(repo, "checkout", "-q", "-b", "feat-p")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "plan without ac")
        git(repo, "checkout", "-q", "main")
        plan(repo, [{"step": 0, "name": "a", "status": "completed"}])  # main 에서는 끝난 것처럼 보인다
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "completed on main")
        e = ex.StepExecutor("p", root=repo)
        calls = install_fake_claude(e, [{"result": OK}])
        with pytest.raises(SystemExit) as se:
            e.run()
        assert se.value.code == 1 and calls == []

    def test_reset_failed_applies_to_phase_branch(self, repo):
        # --reset-failed 는 실행할 브랜치의 index.json 을 고쳐야 한다
        plan(repo, [{"step": 0, "name": "a", "status": "error", "ac": [AC_OK], "error_message": "x"}])
        git(repo, "checkout", "-q", "-b", "feat-p")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "failed run")
        git(repo, "checkout", "-q", "main")
        plan(repo, [{"step": 0, "name": "a", "status": "pending", "ac": [AC_OK]}])
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "plan on main")
        e = ex.StepExecutor("p", root=repo)
        calls = install_fake_claude(e, [{"result": OK}])
        e.run(reset_failed=True)
        assert len(calls) == 1
        assert read_json(e._index_file)["steps"][0]["status"] == "completed"
        assert "error_message" not in read_json(e._index_file)["steps"][0]

class TestRunSessionGit:
    def test_session_commit_is_undone_and_recommitted(self, repo):
        # 세션은 커밋하지 않는다는 규칙을 하네스가 강제한다. 변경은 남기고 커밋만 되돌린다.
        plan(repo, [{"step": 0, "name": "a", "status": "pending", "ac": [AC_OK]}])
        e = ex.StepExecutor("p", root=repo)

        def session_commits(root):
            git(root, "add", "-A")
            git(root, "commit", "-qm", "session commit")

        install_fake_claude(e, [{"files": {"src/a.ts": "x"}, "do": session_commits, "result": OK}])
        e.run()
        assert "session commit" not in git(repo, "log", "--format=%s")
        assert "src/a.ts" in commit_files(repo, "feat(p)")
        assert git(repo, "status", "--porcelain") == ""

    def test_session_branch_switch_fails_step(self, repo):
        plan(repo, [{"step": 0, "name": "a", "status": "pending", "ac": [AC_OK]}])
        e = ex.StepExecutor("p", root=repo)
        install_fake_claude(e, [{"files": {"src/a.ts": "x"},
                                 "do": lambda root: git(root, "checkout", "-q", "-b", "other"), "result": OK}])
        with pytest.raises(SystemExit) as se:
            e.run()
        assert se.value.code == 1
        assert git(repo, "branch", "--show-current").strip() == "feat-p"
        s = read_json(e._index_file)["steps"][0]
        assert s["status"] == "error" and "other" in s["error_message"]
        assert "src/a.ts" in commit_files(repo, "wip(p)")


class TestRunFailures:
    def test_git_failure_stops_instead_of_completing(self, repo, capsys):
        # 회귀: index.lock 때문에 git add 가 실패해도 무시하고 step·phase 를 completed 로 기록했다.
        # 하네스가 타임아웃으로 세션을 죽일 때 세션의 git 이 같이 죽으면 이 lock 이 남는다.
        plan(repo, [{"step": 0, "name": "a", "status": "pending", "ac": [AC_OK]}])
        lock = repo / ".git" / "index.lock"
        e = ex.StepExecutor("p", root=repo)
        install_fake_claude(e, [{"files": {"app.py": "print(1)"}, "do": lambda root: lock.write_text(""),
                                 "result": OK}])
        with pytest.raises(SystemExit) as se:
            e.run()
        out = capsys.readouterr().out
        assert se.value.code == 1 and "completed!" not in out and "index.lock" in out
        assert read_json(e._index_file)["steps"][0]["status"] == "pending"

        lock.unlink()  # 원인을 치우고 이어서 실행하면 완료된다
        e2 = ex.StepExecutor("p", root=repo)
        install_fake_claude(e2, [{"result": OK}])
        e2.run(resume=True)
        assert "app.py" in commit_files(repo, "feat(p)")

    def test_invalid_cost_cap_reports_config_error(self, repo, capsys):
        # 회귀: 설정 검증 전에 헤더가 비용을 숫자로 출력하다 ValueError 로 죽었다
        d = plan(repo, [{"step": 0, "name": "a", "status": "pending", "ac": [AC_OK]}])
        idx = read_json(d / "index.json")
        idx["max_cost_usd"] = "10"
        write_json(d / "index.json", idx)
        e = ex.StepExecutor("p", root=repo)
        with pytest.raises(SystemExit) as se:
            e.run()
        assert se.value.code == 1 and "max_cost_usd" in capsys.readouterr().out


class TestRunSessionState:
    """index.json 은 하네스만 쓴다. 세션이 고친 내용은 되돌린다."""

    def test_session_cannot_mark_other_steps_completed(self, repo, capsys):
        # 회귀: 세션이 index.json 에서 뒤 step 을 completed 로 바꾸면 그 step 은 실행도 AC 검증도 없이 넘어갔다
        d = plan(repo, [{"step": 0, "name": "a", "status": "pending", "ac": [AC_OK]},
                        {"step": 1, "name": "b", "status": "pending", "ac": [AC_OK]}])

        def tamper(root):
            idx = read_json(d / "index.json")
            idx["steps"][1]["status"] = "completed"
            write_json(d / "index.json", idx)

        e = ex.StepExecutor("p", root=repo)
        calls = install_fake_claude(e, [{"do": tamper, "result": OK}, {"result": OK}])
        e.run()
        assert [c["step"] for c in calls] == [0, 1]
        assert "index.json" in capsys.readouterr().out

    def test_session_deleting_index_is_restored(self, repo):
        d = plan(repo, [{"step": 0, "name": "a", "status": "pending", "ac": [AC_OK]}])
        e = ex.StepExecutor("p", root=repo)
        install_fake_claude(e, [{"do": lambda root: (d / "index.json").unlink(), "result": OK}])
        e.run()
        assert read_json(d / "index.json")["steps"][0]["status"] == "completed"
