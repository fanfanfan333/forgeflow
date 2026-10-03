"""INC46 T23 — the document-validation **package**.

History
-------
This started life as the single module ``forgeflow.documents.validation``
(INC43 S4 — the honest tri-state ``verify_*`` family: ``True`` measured-and-OK,
``False`` measured-and-violated, ``None`` **not measured**). T23 (the five-layer
validation stack, design §「五层验证栈」) needs a place to live *beside* it, so the
module became a package.

The migration is **additive and import-path-preserving** (task book: 「把它转成包，
但保持公开导入路径逐字不变」):

* the original module was moved **verbatim** to
  :mod:`~forgeflow.documents.validation.legacy` (no logic change);
* this ``__init__`` **re-exports** its public surface, so *both*

      from forgeflow.documents.validation import verify_docx        # package path
      from forgeflow.documents import verify_docx                    # facade path

  keep working with the **same names** and the **same** :data:`__all__`;
* the new layers live in sibling sub-modules and are re-exported here for
  convenience (they are *not* added to ``__all__`` — that stays exactly the
  legacy surface, byte-for-byte).

Package layout
--------------
* :mod:`~forgeflow.documents.validation.legacy`      — INC43/44/45 ``verify_*``;
* :mod:`~forgeflow.documents.validation.verdict`     — the layer verdict model;
* :mod:`~forgeflow.documents.validation.structural`  — **L1** structural;
* :mod:`~forgeflow.documents.validation.invariants`  — **L2** invariants;
* :mod:`~forgeflow.documents.validation.format`      — **L3** format fidelity;
* :mod:`~forgeflow.documents.validation.render`      — **L4** render compare;
* :mod:`~forgeflow.documents.validation.semantic`    — **L5** LLM-as-judge;
* :mod:`~forgeflow.documents.validation.stack`       — the orchestrator.
"""

from __future__ import annotations

# --- INC43 S4 — the original tri-state verification surface (names unchanged) --
from forgeflow.documents.validation.legacy import (
    VerifyReport,
    verify_docx,
    verify_pdf,
    verify_pptx,
    verify_sheet,
    verify_textfile,
)

#: The legacy public surface, **verbatim** (task book: 「保持 __all__ 与原名」).
__all__ = [
    "VerifyReport",
    "verify_docx",
    "verify_pptx",
    "verify_textfile",
    "verify_sheet",
    "verify_pdf",
]

# --- INC46 T23 — the five-layer validation stack (additive re-exports) ---------
# These are deliberately imported *after* the legacy names so an import of this
# package always yields the legacy surface first. They are re-exported for
# callers that prefer ``forgeflow.documents.validation.validate_document`` but are
# NOT part of ``__all__`` (which stays the legacy six).
from forgeflow.documents.validation.stack import validate_document
from forgeflow.documents.validation.verdict import (
    LayerVerdict,
    ValidationConfig,
    ValidationVerdict,
)
