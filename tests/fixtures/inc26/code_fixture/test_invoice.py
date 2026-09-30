"""Fixture test — must FAIL on the unmodified fixture code (INC26 P0-8).

On the shipped (defective) code:
  * ``test_subtotal_sums`` passes;
  * ``test_invoice_total_adds_tax`` and ``test_invoice_total_multiple`` fail,
    because :func:`billing.invoice.with_tax` subtracts the tax.

After the agent repairs the operator, all three pass.
"""

from __future__ import annotations

from billing.invoice import invoice_total, subtotal


def test_subtotal_sums() -> None:
    assert subtotal([10.0, 20.0, 30.0]) == 60.0


def test_invoice_total_adds_tax() -> None:
    # 100 * (1 + 0.1) == 110.0 ; the defect yields 90.0
    assert invoice_total([100.0]) == 110.0


def test_invoice_total_multiple() -> None:
    # (10 + 20) * 1.1 == 33.0 ; the defect yields 27.0
    assert invoice_total([10.0, 20.0]) == 33.0
