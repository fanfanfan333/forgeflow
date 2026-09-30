"""INC29 T04 —— 薄 OpenHands Agent Server（§4）：端点契约、鉴权、线路纪律。

本文件只测**服务端本体**（用 ``starlette.testclient.TestClient`` 在进程内驱动，
不启 uvicorn、不碰真模型）。覆盖：

* 端点：C1/C2/C4/C7/C8/count/C15/C18 的**真实行为**（不是桩），未实现端点
  一律 ``501``（诚实缺省），缺 key ⇒ ``401``（鉴权先于路由）；
* A2 单真相源 —— 服务端源码**不含**任何 ``label`` / ``phase`` / ``kind`` 业务词表；
* A6 不丢不重 —— ``seq`` 单调、按 ``after_seq`` 续订无缺口、无重复（REST + WS）；
* A8 判定权 —— 服务端响应**无** pass/fail 字段，也不引用 ForgeFlow 的 verdict 模块；
* A9 词表封闭 —— 错误事件映射后的 ``status`` ∈ ``protocol.py::STEP_STATUSES``，
  派生的 ``degraded`` ∈ ``protocol.py::DEGRADED_VALUES``；
* D3 守卫复用 —— 服务端传输（``conversation.py::_build``）真的坐实了与子进程档**同一个**
  ``run_code_task._PolicyGuardedExecutor`` 守卫到解析后的 ``file_editor`` 工具上，且守卫
  未坐实时会产出可见 note frame（``AgentErrorEvent`` 形状）而非静默。

引文一律 ``file.py::symbol``，不用行号。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import threading
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from forgeflow.codeplane.runner import run_code_task
from forgeflow.codeplane.runner.agent_server import conversation
from forgeflow.codeplane.runner.agent_server.app import create_app
from forgeflow.codeplane.runner.agent_server.models import BashOutput

_SERVER_DIR = (
    Path(__file__).resolve().parents[2]
    / "forgeflow"
    / "codeplane"
    / "runner"
    / "agent_server"
)
_TOKEN = "test-session-key"
_HEADERS = {"X-Session-API-Key": _TOKEN}


def _client(**kwargs) -> TestClient:
    return TestClient(create_app(token=_TOKEN, **kwargs))


def _fake_event(type_name: str, event_id: str):
    """A bare object whose class name is ``type_name`` and whose ``id`` is stable."""
    cls = type(type_name, (), {})
    obj = cls()
    obj.id = event_id
    return obj


def _create_conversation(client: TestClient, workspace: str = "") -> str:
    body = {
        "agent": {"llm": {"model": "ollama_chat/qwen3:8b"}, "tools": ["terminal"]},
        "workspace": {"working_dir": workspace} if workspace else {},
        "initial_message": {"role": "user", "content": [{"type": "text", "text": "hi"}], "run": False},
        "max_iterations": 3,
    }
    resp = client.post("/api/conversations", json=body, headers=_HEADERS)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# --------------------------------------------------------------------------- #
# 端点：真实行为                                                               #
# --------------------------------------------------------------------------- #
def test_c1_c2_create_and_get_conversation():
    with _client() as client:
        cid = _create_conversation(client)
        resp = client.get(f"/api/conversations/{cid}", headers=_HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == cid
        assert body["execution_status"] == "idle"


def test_count_is_a_liveness_probe():
    with _client() as client:
        assert client.get("/api/conversations/count", headers=_HEADERS).json() == 0
        _create_conversation(client)
        assert client.get("/api/conversations/count", headers=_HEADERS).json() == 1


def test_c7_delete_removes_the_conversation():
    with _client() as client:
        cid = _create_conversation(client)
        assert client.delete(f"/api/conversations/{cid}", headers=_HEADERS).status_code == 200
        assert client.get(f"/api/conversations/{cid}", headers=_HEADERS).status_code == 404


def test_c8_unknown_conversation_is_404():
    with _client() as client:
        resp = client.get(
            "/api/conversations/00000000-0000-0000-0000-000000000000/events/search",
            headers=_HEADERS,
        )
        assert resp.status_code == 404


def test_c4_run_unknown_conversation_is_404():
    with _client() as client:
        resp = client.post(
            "/api/conversations/00000000-0000-0000-0000-000000000000/run", headers=_HEADERS
        )
        assert resp.status_code == 404


def test_c18_returns_raw_exit_code_and_stdout():
    with _client() as client:
        resp = client.post(
            "/api/bash/execute_bash_command",
            json={"command": "echo raw-output-probe", "timeout": 20},
            headers=_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["exit_code"] == 0
        assert "raw-output-probe" in body["stdout"]
        assert body["timeout"] is False


def test_c18_nonzero_exit_is_reported_verbatim():
    with _client() as client:
        resp = client.post(
            "/api/bash/execute_bash_command",
            json={"command": "exit 3", "timeout": 20},
            headers=_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["exit_code"] == 3


def test_c18_cwd_outside_the_workspace_is_422():
    import tempfile

    with _client(workspace_root=str(Path(tempfile.gettempdir()))) as client:
        resp = client.post(
            "/api/bash/execute_bash_command",
            json={"command": "echo x", "cwd": "C:/Windows", "timeout": 20},
            headers=_HEADERS,
        )
        assert resp.status_code == 422


@pytest.mark.skipif(shutil.which("git") is None, reason="git 不可用")
def test_c15_git_diff_returns_original_and_modified(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    target = repo / "a.txt"
    target.write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init"],
        cwd=repo,
        check=True,
    )
    target.write_text("new\n", encoding="utf-8")

    with _client() as client:
        resp = client.get("/api/git/diff", headers=_HEADERS, params={"path": str(target)})
        assert resp.status_code == 200
        body = resp.json()
        assert body["original"] == "old\n"
        assert body["modified"] == "new\n"


def test_c15_missing_path_is_400():
    with _client() as client:
        assert client.get("/api/git/diff", headers=_HEADERS).status_code == 400


# --------------------------------------------------------------------------- #
# 未实现端点 ⇒ 501；缺 key ⇒ 401（鉴权先于路由）                                 #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/conversations/search"),
        ("POST", "/api/conversations/abc/pause"),
        ("POST", "/api/conversations/abc/interrupt"),
        ("GET", "/api/conversations/abc/events/count"),
        ("GET", "/api/git/changes"),
        ("GET", "/api/git/commits"),
        ("POST", "/api/bash/start_bash_command"),
        ("POST", "/api/auth/workspace-session"),
        ("GET", "/api/totally/unknown/path"),
    ],
)
def test_unimplemented_endpoints_are_501(method, path):
    with _client() as client:
        resp = client.request(method, path, headers=_HEADERS)
        assert resp.status_code == 501, f"{method} {path} -> {resp.status_code}"
        assert resp.json() == {"detail": "Not Implemented"}


def test_missing_key_is_401_even_for_an_unimplemented_endpoint():
    with _client() as client:
        resp = client.post("/api/conversations/abc/pause")
        assert resp.status_code == 401
        assert resp.json() == {"detail": "Unauthorized"}


def test_wrong_key_is_401():
    with _client() as client:
        resp = client.get("/api/conversations/count", headers={"X-Session-API-Key": "nope"})
        assert resp.status_code == 401


def test_ws_auth_failure_closes_the_socket():
    with _client() as client:
        cid = _create_conversation(client)
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"/sockets/events/{cid}"):
                pass


def test_ws_unknown_conversation_closes_4004():
    from starlette.websockets import WebSocketDisconnect

    with _client() as client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(
                "/sockets/events/00000000-0000-0000-0000-000000000000"
                f"?session_api_key={_TOKEN}"
            ):
                pass
        assert exc.value.code == 4004


def test_validation_error_is_422_without_echoing_input():
    with _client() as client:
        resp = client.post("/api/conversations", json={"agent": "not-a-dict"}, headers=_HEADERS)
        assert resp.status_code == 422
        assert isinstance(resp.json()["detail"], list)


# --------------------------------------------------------------------------- #
# A6 不丢不重 —— seq 单调、可续订、无重复（REST）                                #
# --------------------------------------------------------------------------- #
def test_a6_frames_are_monotonic_and_deduplicated_rest():
    with _client() as client:
        cid = _create_conversation(client)
        runtime = client.app.state.runtimes[cid]
        for index in range(5):
            runtime._append_event(_fake_event("ActionEvent", f"e{index}"))
        # A duplicate SDK event (same fingerprint) must be dropped.
        duplicate = _fake_event("ActionEvent", "dup")
        runtime._append_event(duplicate)
        runtime._append_event(duplicate)

        items = client.get(
            f"/api/conversations/{cid}/events/search", headers=_HEADERS, params={"after_seq": -1}
        ).json()["items"]
        seqs = [frame["seq"] for frame in items]
        assert seqs == list(range(len(seqs))), f"seq 非严格递增：{seqs}"
        assert len(seqs) == len(set(seqs)), "存在重复 seq"
        assert len(items) == 6, f"去重失败：{len(items)} 帧"

        # Resume from seq=2 ⇒ only 3..5 (no gap, no duplicate).
        resumed = client.get(
            f"/api/conversations/{cid}/events/search", headers=_HEADERS, params={"after_seq": 2}
        ).json()["items"]
        assert [frame["seq"] for frame in resumed] == [3, 4, 5]


def test_a6_ws_resume_has_no_loss_or_duplicate():
    with _client() as client:
        cid = _create_conversation(client)
        runtime = client.app.state.runtimes[cid]
        for index in range(5):
            runtime._append_event(_fake_event("ActionEvent", f"w{index}"))

        with client.websocket_connect(
            f"/sockets/events/{cid}?session_api_key={_TOKEN}&after_seq=-1"
        ) as ws:
            first = [ws.receive_json() for _ in range(5)]
        assert [frame["seq"] for frame in first] == [0, 1, 2, 3, 4]

        for index in range(5, 10):
            runtime._append_event(_fake_event("ActionEvent", f"w{index}"))
        with client.websocket_connect(
            f"/sockets/events/{cid}?session_api_key={_TOKEN}&after_seq=4"
        ) as ws:
            second = [ws.receive_json() for _ in range(5)]
        seqs = [frame["seq"] for frame in first] + [frame["seq"] for frame in second]
        assert seqs == list(range(10)), f"续订后 seq 不连续：{seqs}"
        assert len(seqs) == len(set(seqs)), "续订后出现重复帧"


def test_ws_frames_carry_only_the_raw_wire_shape():
    with _client() as client:
        cid = _create_conversation(client)
        runtime = client.app.state.runtimes[cid]
        runtime._append_event(_fake_event("ObservationEvent", "o1"))
        with client.websocket_connect(
            f"/sockets/events/{cid}?session_api_key={_TOKEN}"
        ) as ws:
            frame = ws.receive_json()
        assert frame["type"] == "ObservationEvent"
        assert set(frame) <= {"type", "seq", "ts", "source", "tool", "detail", "stdout", "stderr"}
        assert "label" not in frame and "phase" not in frame and "kind" not in frame


# --------------------------------------------------------------------------- #
# A2 单真相源 —— 服务端源码不含业务词表                                          #
# --------------------------------------------------------------------------- #
def test_a2_server_source_has_no_business_vocabulary():
    pattern = re.compile(r"\b(label|phase|kind)\s*[=:]")
    offenders: dict[str, list[str]] = {}
    for path in sorted(_SERVER_DIR.glob("*.py")):
        hits = pattern.findall(path.read_text(encoding="utf-8"))
        if hits:
            offenders[path.name] = hits
    assert offenders == {}, f"服务端出现了业务词表赋值：{offenders}"


# --------------------------------------------------------------------------- #
# A8 判定权 —— 服务端无 pass/fail，也不引用 ForgeFlow 的 verdict 模块            #
# --------------------------------------------------------------------------- #
def _code_only(source: str) -> str:
    """Strip docstrings + ``#`` comments so only executable text remains."""
    without_docstrings = re.sub(r'"""[\s\S]*?"""', "", source)
    without_docstrings = re.sub(r"'''[\s\S]*?'''", "", without_docstrings)
    return re.sub(r"#[^\n]*", "", without_docstrings)


def test_a8_server_has_no_pass_fail_and_owns_no_verdict():
    fields = set(BashOutput.model_fields)
    assert not (fields & {"passed", "failed", "verdict", "ok", "success", "result"}), fields

    code = "\n".join(_code_only(p.read_text(encoding="utf-8")) for p in _SERVER_DIR.glob("*.py"))
    assert "evaluate_test_output" not in code
    assert "tests_verdict" not in code


# --------------------------------------------------------------------------- #
# A9 词表封闭                                                                  #
# --------------------------------------------------------------------------- #
def test_a9_error_events_map_inside_the_shared_status_vocabulary():
    from forgeflow.codeplane.events import adapt_openhands_event
    from forgeflow.codeplane.protocol import STEP_STATUSES

    for type_name in ("ConversationErrorEvent", "AgentErrorEvent"):
        event = adapt_openhands_event({"type": type_name, "detail": "boom"})
        assert event.status in STEP_STATUSES
        assert event.status == "error"
        assert event.kind == "engine"


def test_a9_server_never_emits_a_degraded_field():
    """``degraded`` is derived client-side only — the server has no such concept."""
    joined = "\n".join(p.read_text(encoding="utf-8") for p in _SERVER_DIR.glob("*.py"))
    assert "degraded" not in joined


# --------------------------------------------------------------------------- #
# D3 守卫复用 —— 服务端传输真的坐实了与子进程档**同一个** file_editor 动作策略守卫 #
# --------------------------------------------------------------------------- #
#: ``conversation.py::_build`` 声称复用 ``run_code_task._install_file_editor_guard``，
#: 但此前**没有任何测试**证明服务端传输真的坐实了同一个守卫（INC29 T04 review D3）。
#: 这一段用注入的伪 SDK 走通 ``ConversationRuntime._build`` 的真实代码路径，断言：
#:   1) 守卫被安装到**解析后**的 ``file_editor`` 工具上（``set_executor`` 真被调用）；
#:   2) 坐实的是 ``run_code_task`` 里那**同一个** ``_PolicyGuardedExecutor`` 子类；
#:   3) 守卫**未能**坐实时会产出**可见**的 note frame（``AgentErrorEvent`` 形状）而非静默。


class _FakeResolvedTool:
    """A stand-in for an SDK-resolved ``ToolDefinition`` (records ``set_executor``)."""

    def __init__(self, name: str, description: str = "") -> None:
        self.name = name
        self.description = description
        self.executor: object | None = object()  # a live tool always carries one
        self.seated: list[object] = []

    def set_executor(self, executor: object) -> "_FakeResolvedTool":
        self.seated.append(executor)
        self.executor = executor
        return self

    def model_copy(self, update: dict | None = None) -> "_FakeResolvedTool":
        clone = _FakeResolvedTool(self.name, self.description)
        clone.executor = self.executor
        clone.seated = self.seated  # shared, so the seated executor stays observable
        if update:
            clone.description = update.get("description", clone.description)
        return clone


def _resolved_tools_from(specs: list) -> dict[str, _FakeResolvedTool]:
    """Emulate the SDK resolving tool *specs* into concrete ``tools_map`` entries."""
    out: dict[str, _FakeResolvedTool] = {}
    for spec in specs:
        name = str(getattr(spec, "name", "") or "")
        if not name:
            continue
        # The file_editor description carries the SDK's exact bad-path sentence, so
        # ``_host_path_line_fix`` replaces it in place (no "description changed" note).
        description = run_code_task._SDK_BAD_PATH_LINE if name == "file_editor" else ""
        out[name] = _FakeResolvedTool(name, description)
    return out


class _FakeGuardSDK:
    """A minimal fake ``openhands`` SDK exposing only the seam the D3 guard introspects."""

    def __init__(self, *, empty_tools: bool = False) -> None:
        self.empty_tools = empty_tools
        self.agent: object | None = None
        self.conversation: object | None = None
        self.file_editor_tool: _FakeResolvedTool | None = None

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        created = self
        sdk = types.ModuleType("openhands.sdk")

        class _LLM:
            def __init__(self, **kwargs) -> None:  # noqa: ANN003
                self.kwargs = kwargs

        class _Tool:
            def __init__(self, name: str = "", **kwargs) -> None:  # noqa: ANN003
                self.name = name

        class _Agent:
            def __init__(self, llm=None, tools=None, system_prompt=None, **kwargs) -> None:  # noqa: ANN001, ANN003
                self.llm = llm
                self.tools = list(tools or [])
                self.system_prompt = system_prompt if system_prompt is not None else ""
                self._tools_lock = threading.Lock()
                self._tools = {} if created.empty_tools else _resolved_tools_from(self.tools)
                created.agent = self
                created.file_editor_tool = self._tools.get("file_editor")

            @property
            def tools_map(self) -> dict[str, _FakeResolvedTool]:
                return self._tools

        class _Conversation:
            def __init__(self, **kwargs) -> None:  # noqa: ANN003
                self.kwargs = kwargs
                self.agent = kwargs.get("agent")
                self.state = types.SimpleNamespace(events=[])
                created.conversation = self

            def run(self) -> None:  # pragma: no cover — never driven here
                pass

            def send_message(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003  # pragma: no cover
                pass

        sdk.LLM = _LLM
        sdk.Agent = _Agent
        sdk.Conversation = _Conversation
        sdk.Tool = _Tool

        terminal = types.ModuleType("openhands.tools.terminal")

        class _TerminalTool:
            name = "terminal"

        terminal.TerminalTool = _TerminalTool

        file_editor = types.ModuleType("openhands.tools.file_editor")

        class _FileEditorTool:
            name = "file_editor"

        file_editor.FileEditorTool = _FileEditorTool

        tool_tool = types.ModuleType("openhands.sdk.tool.tool")

        class _ToolExecutor:
            def __call__(self, action, conversation=None):  # noqa: ANN001, ANN002  # pragma: no cover
                raise NotImplementedError

        tool_tool.ToolExecutor = _ToolExecutor

        fe_definition = types.ModuleType("openhands.tools.file_editor.definition")

        class _FileEditorObservation:
            @staticmethod
            def from_text(*args, **kwargs):  # noqa: ANN002, ANN003  # pragma: no cover
                return None

        fe_definition.FileEditorObservation = _FileEditorObservation

        monkeypatch.setitem(sys.modules, "openhands", types.ModuleType("openhands"))
        monkeypatch.setitem(sys.modules, "openhands.sdk", sdk)
        monkeypatch.setitem(
            sys.modules, "openhands.sdk.tool", types.ModuleType("openhands.sdk.tool")
        )
        monkeypatch.setitem(sys.modules, "openhands.sdk.tool.tool", tool_tool)
        monkeypatch.setitem(sys.modules, "openhands.tools", types.ModuleType("openhands.tools"))
        monkeypatch.setitem(sys.modules, "openhands.tools.terminal", terminal)
        monkeypatch.setitem(sys.modules, "openhands.tools.file_editor", file_editor)
        monkeypatch.setitem(
            sys.modules, "openhands.tools.file_editor.definition", fe_definition
        )


def _guard_request(workspace: str, agent_spec: dict) -> SimpleNamespace:
    return SimpleNamespace(
        agent=agent_spec,
        workspace={"working_dir": workspace},
        max_iterations=0,
        initial_message=None,
    )


def test_d3_guard_is_seated_on_the_resolved_file_editor_tool(monkeypatch, tmp_path):
    """``_build`` 必须把 D3 守卫坐实到解析后的 ``file_editor`` 工具上（非静默）。"""
    monkeypatch.setattr(run_code_task, "_GUARDED_EXECUTOR_CLS", None)
    fake = _FakeGuardSDK()
    fake.install(monkeypatch)

    spec = {"llm": {"model": "ollama_chat/qwen3:8b"}, "tools": ["terminal", "file_editor"]}
    runtime = conversation.ConversationRuntime("cid-d3", _guard_request(str(tmp_path), spec))
    runtime._build()

    assert fake.conversation is not None, "_build 未构造会话"
    live_tools = fake.conversation.agent._tools
    tool = live_tools["file_editor"]
    assert tool.seated, "守卫未被安装到解析后的 file_editor 工具上（静默丢失）"

    seated = tool.seated[-1]
    guarded_cls = run_code_task._guarded_executor_class()
    assert isinstance(seated, guarded_cls), f"坐实的不是 D3 守卫类：{type(seated)!r}"
    assert isinstance(seated, run_code_task._PolicyGuardedExecutor)
    assert seated._workspace_root == str(tmp_path)
    assert "workspace_boundary" in seated.rule_names, seated.rule_names

    # 成功坐实 ⇒ 不得误报「策略未启用」的 note frame。
    notes = [
        f for f in runtime._frames if f.get("type") == "AgentErrorEvent"
    ]
    assert not any(
        "代码动作策略未启用" in str(f.get("detail", "")) for f in notes
    ), notes


def test_d3_unseated_guard_emits_a_visible_note_frame(monkeypatch, tmp_path):
    """守卫**未能**坐实时必须产出可见的 note frame（``AgentErrorEvent``），绝不静默。"""
    monkeypatch.setattr(run_code_task, "_GUARDED_EXECUTOR_CLS", None)
    fake = _FakeGuardSDK(empty_tools=True)  # tools_map 为空 ⇒ 守卫无处锚定
    fake.install(monkeypatch)

    spec = {"llm": {"model": "ollama_chat/qwen3:8b"}, "tools": ["terminal"]}
    runtime = conversation.ConversationRuntime("cid-d3b", _guard_request(str(tmp_path), spec))
    runtime._build()

    assert fake.file_editor_tool is None, "空工具集里不该出现 file_editor"
    notes = [f for f in runtime._frames if f.get("type") == "AgentErrorEvent"]
    assert notes, "守卫未能坐实却没有任何可见 note frame（静默失败）"
    assert any(
        "代码动作策略未启用" in str(f.get("detail", "")) for f in notes
    ), notes
    # note 只带 raw 形状，绝无 degraded（那是客户端派生、非服务端概念）。
    assert all("degraded" not in f for f in notes)

