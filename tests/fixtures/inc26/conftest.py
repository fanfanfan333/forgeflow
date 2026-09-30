"""INC26 test fixtures — keep the *deliberately failing* code fixture out of the
main suite's collection.

``tests/fixtures/inc26/code_fixture/`` is a real little Python project whose
``test_invoice.py`` is **supposed** to fail on the unmodified code (it is the
seed-defect fixture for the code-capability acceptance, INC26 P0-8). If the main
suite collected it, the offline/real regression tracks would go red for no
reason. The QA driver copies the fixture into a scratch source repo itself, so
nothing here needs to be importable by the suite.
"""

from __future__ import annotations

# Paths are relative to this conftest.py's directory.
collect_ignore_glob = ["code_fixture/*"]
collect_ignore = [
    "code_fixture/test_invoice.py",
    "code_fixture/billing",
]
