"""Tiny invoicing helpers — the INC26 code-capability fixture (INC26 P0-8).

This module ships **one deliberate seed defect** for the code-capability
acceptance: :func:`with_tax` SUBTRACTS the tax instead of ADDING it, so the
fixture's own test (``test_invoice.py``) necessarily fails on the unmodified
code. The isolated-workspace agent is expected to read the test, read this
module, fix the operator, and re-run the suite.

Nothing here needs a third-party dependency — it is a pure-stdlib project so the
workspace test command can run it with any real interpreter that has pytest.
"""

from __future__ import annotations

#: The sales-tax rate applied to an invoice subtotal.
TAX_RATE = 0.1


def subtotal(amounts: list[float]) -> float:
    """Return the un-taxed sum of ``amounts``."""
    return sum(amounts)


def with_tax(amounts: list[float]) -> float:
    """Return the sum of ``amounts`` **with** tax added.

    Seed defect (INC26 P0-8): the tax is subtracted rather than added.
    """
    base = subtotal(amounts)
    return base - base * TAX_RATE


def invoice_total(amounts: list[float]) -> float:
    """Return the tax-inclusive invoice total, rounded to cents."""
    return round(with_tax(amounts), 2)
