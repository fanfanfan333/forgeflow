"""Standalone OpenHands runner package (INC25 W2).

This package is executed by the *openhands* virtualenv's interpreter, never
imported by ForgeFlow. It must NOT ``import forgeflow`` — the runner speaks the
JSONL contract declared (ForgeFlow-side) in ``forgeflow/codeplane/protocol.py``
and re-declares the same literal constants locally. A drift test pins both rules.

See ``run_code_task.py`` for the entry point and ``README.md`` for the contract.
"""
