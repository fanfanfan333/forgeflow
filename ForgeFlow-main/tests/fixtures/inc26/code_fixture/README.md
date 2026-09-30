# INC26 code-capability fixture (`billing`)

A tiny pure-stdlib Python project used by the INC26 **code-capability** acceptance
(INC26 P0-8 / AC-14 / AC-15):

* `billing/invoice.py` — a real module with **one seed defect**: `with_tax()`
  subtracts the tax instead of adding it.
* `test_invoice.py` — a test that **necessarily fails** on the unmodified code
  (1 passed, 2 failed).

The QA driver copies this directory into a scratch source repo
(`D:\Temp\inc26\...`), registers that copy as a `local_path` **code resource**,
then runs a real code task on it. The engine copies the source into its own
isolated workspace, so the scratch source repo's working tree must be
byte-for-byte unchanged by the run (AC-14).

> This directory is excluded from the main pytest collection by the neighbouring
> `conftest.py` (`collect_ignore_glob`), because its `test_invoice.py` is
> *intended* to fail — it is a fixture, not part of the ForgeFlow suite.
