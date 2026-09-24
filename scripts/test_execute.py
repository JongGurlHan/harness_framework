"""
execute.py 테스트.
git·claude CLI·AC 커맨드는 모두 mock 처리하고, 상태 전이와 프롬프트 구성을 검증한다.
"""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import execute as ex


def write_json(p: Path, data):
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def read_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


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
    d = tmp_project / "phases" / "t"
    d.mkdir(exist_ok=True)
    write_json(d / "index.json", {"project": "T", "phase": "t", "steps": steps})
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


class TestInit:
    def test_reads_model_and_timeout_from_index(self, tmp_project):
        d = tmp_project / "phases" / "cfg"
        d.mkdir()
        write_json(d / "index.json", {"project": "P", "phase": "cfg", "model": "claude-opus-5-5",
                                      "timeout_sec": 7200, "steps": []})
        inst = ex.StepExecutor("cfg", root=tmp_project)
        assert inst._model == "claude-opus-5-5"
        assert inst._timeout == 7200

    def test_cli_model_overrides_index(self, tmp_project):
        d = tmp_project / "phases" / "cfg"
        d.mkdir()
        write_json(d / "index.json", {"project": "P", "phase": "cfg", "model": "a", "steps": []})
        assert ex.StepExecutor("cfg", root=tmp_project, model="b")._model == "b"

    def test_defaults(self, executor):
        assert executor._model is None
        assert executor._timeout == ex.DEFAULT_TIMEOUT_SEC


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
        code = ("import subprocess, sys, time; "
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
                "time.sleep(30)")
        import time
        t0 = time.monotonic()
        rc, _, _, timed_out = ex.run_killing_tree([sys.executable, "-c", code], input="",
                                                   cwd=str(tmp_path), timeout=1)
        assert timed_out and time.monotonic() - t0 < 15


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


# ---------------------------------------------------------------------------
# 검증
# ---------------------------------------------------------------------------

class TestRunAc:
    def test_all_pass(self, executor):
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


class TestValidateAc:
    def test_missing_ac_exits(self, tmp_project):
        inst = make_executor(tmp_project, [{"step": 0, "name": "a", "status": "pending"}])
        with pytest.raises(SystemExit) as e:
            inst._validate_ac()
        assert e.value.code == 1

    def test_empty_ac_exits(self, tmp_project):
        inst = make_executor(tmp_project, [{"step": 0, "name": "a", "status": "pending", "ac": []}])
        with pytest.raises(SystemExit):
            inst._validate_ac()

    def test_blank_skip_reason_exits(self, tmp_project):
        inst = make_executor(tmp_project, [{"step": 0, "name": "a", "status": "pending", "skip_ac": " "}])
        with pytest.raises(SystemExit):
            inst._validate_ac()

    def test_explicit_skip_ok(self, tmp_project):
        make_executor(tmp_project, [
            {"step": 0, "name": "a", "status": "pending", "skip_ac": "문서만 수정"},
        ])._validate_ac()

    def test_completed_steps_ignored(self, tmp_project):
        make_executor(tmp_project, [
            {"step": 0, "name": "a", "status": "completed"},
            {"step": 1, "name": "b", "status": "pending", "ac": ["npm test"]},
        ])._validate_ac()


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

    def test_model_error_is_retry(self, executor):
        self._write_result(executor, {"status": "error", "reason": "원인 불명"})
        status, fb, _ = executor._verify(self._step(executor), self._inv())
        assert status == "retry" and "원인 불명" in fb

    def test_invalid_json_is_retry(self, executor):
        executor._result_file(2).write_text("{not json", encoding="utf-8")
        status, _, _ = executor._verify(self._step(executor), self._inv())
        assert status == "retry"

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
            return ex.InvokeResult(exit_code=run.get("exit", 0), timed_out=run.get("timeout", False), stderr="")

        executor._invoke_claude = fake
        return calls

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
        executor._git_commit = lambda msg: msgs.append(msg)
        executor._run_git = MagicMock(return_value=MagicMock(returncode=1, stdout="", stderr=""))
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
    def _record(self, executor, diff_rcs):
        calls = []
        rcs = iter(diff_rcs)

        def fake_git(*args):
            calls.append(args)
            if args[:2] == ("diff", "--cached"):
                return MagicMock(returncode=next(rcs, 0))
            return MagicMock(returncode=0, stdout="", stderr="")

        executor._run_git = fake_git
        return calls

    def test_two_phase_commit(self, executor):
        calls = self._record(executor, [1, 1])
        executor._commit_step(2, "ui")
        msgs = [c[2] for c in calls if c[0] == "commit"]
        assert msgs[0].startswith("feat(mvp):") and msgs[1].startswith("chore(mvp):")

    def test_no_code_changes_skips_feat_commit(self, executor):
        calls = self._record(executor, [0, 1])
        executor._commit_step(2, "ui")
        msgs = [c[2] for c in calls if c[0] == "commit"]
        assert len(msgs) == 1 and msgs[0].startswith("chore")

    def test_wip_prefix(self, executor):
        calls = self._record(executor, [1, 1])
        executor._commit_step(2, "ui", kind="wip")
        msgs = [c[2] for c in calls if c[0] == "commit"]
        assert msgs[0].startswith("wip(mvp):")

    def test_commit_failure_exits(self, executor):
        def fake_git(*args):
            if args[:2] == ("diff", "--cached"):
                return MagicMock(returncode=1)
            if args[0] == "commit":
                return MagicMock(returncode=1, stdout="", stderr="hook failed")
            return MagicMock(returncode=0, stdout="", stderr="")
        executor._run_git = fake_git
        with pytest.raises(SystemExit) as e:
            executor._commit_step(2, "ui")
        assert e.value.code == 1


# ---------------------------------------------------------------------------
# top-level index
# ---------------------------------------------------------------------------

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

    def test_missing_index_exits(self, tmp_project):
        (tmp_project / "phases" / "empty").mkdir()
        with patch("sys.argv", ["execute.py", "empty"]), patch.object(ex, "ROOT", tmp_project):
            with pytest.raises(SystemExit) as e:
                ex.main()
            assert e.value.code == 1
