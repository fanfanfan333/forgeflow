"""file_index must not clear paths when plane paths are empty.

2026-10-31, f1d16f6 acceptance: the platform's own resource block
prompts "textfile.inspect [file_index=0]", but _effective_args only
indexed into plane_paths. A delegation child (or a template-path
task) whose files arrived via context paths had plane_paths = [],
so file_index=0 cleared paths to [] and the handler failed, while
a call without file_index succeeded. The same bug hit document
inspect on the A task.
"""

from types import SimpleNamespace

from forgeflow.runtime.orchestrator import TaskCreate
from forgeflow.runtime.react_executor import ReactExecutor


def _effective(context, args, tool="textfile.inspect"):
    task = TaskCreate(intent="read specification", context=context)
    ctx = SimpleNamespace(
        role="viewer",
        available_skills=[],
        injected_skills=[],
        tenant_id="t",
        user_id="u",
        run_id="r",
        task_context=None,
    )
    effective, _ = ReactExecutor._effective_args(tool, args, task, ctx, [])
    return effective


def test_file_index_falls_back_to_context_paths():
    effective = _effective({"paths": ["/tmp/a.txt", "/tmp/b.txt"]}, {"file_index": 0})
    assert effective["paths"] == ["/tmp/a.txt"]
    assert effective["text_paths"] == ["/tmp/a.txt"]


def test_file_index_second_item_from_context_paths():
    effective = _effective({"paths": ["/tmp/a.txt", "/tmp/b.txt"]}, {"file_index": 1})
    assert effective["paths"] == ["/tmp/b.txt"]


def test_file_index_out_of_range_does_not_clear_paths():
    effective = _effective({"paths": ["/tmp/a.txt", "/tmp/b.txt"]}, {"file_index": 9})
    assert effective["paths"] == ["/tmp/a.txt", "/tmp/b.txt"]


def test_typed_paths_take_precedence_over_generic_paths():
    effective = _effective(
        {
            "paths": ["/tmp/a.txt"],
            "text_paths": ["/tmp/real.txt"],
            "text_names": ["specification.txt"],
        },
        {"file_index": 0},
    )
    assert effective["text_paths"] == ["/tmp/real.txt"]
    assert effective["text_names"] == ["specification.txt"]
