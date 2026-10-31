"""Delegation children must actually be able to read parent files.

2026-10-31, b32c891 round: research experts failed because they had
no file-plane tool and were never told a file_index existed.
"""

from types import SimpleNamespace

from forgeflow.runtime.delegation import ROLE_TOOLS
from forgeflow.runtime.react_executor import ReactExecutor


def test_research_role_can_read_uploaded_files():
    assert "textfile.inspect" in ROLE_TOOLS["research"]
    assert "document.inspect" in ROLE_TOOLS["research"]


def test_resource_block_discloses_child_context_text_paths():
    task = SimpleNamespace(
        context={
            "text_paths": ["/tmp/specification.txt"],
            "text_names": ["specification.txt"],
            "paths": ["/tmp/specification.txt"],
        }
    )
    block = ReactExecutor._resource_context(task)
    assert "specification.txt" in block
    assert "textfile.inspect" in block
    assert "file_index=0" in block
