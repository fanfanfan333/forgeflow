import io

from forgeflow.resources.preview import preview_binary


def test_sheet_preview_keeps_formulas_and_limits_rows_and_columns():
    import openpyxl

    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["金额", "合计"])
    sheet.append([42, "=A2*2"])
    sheet.append([99, "=A3*2"])
    sheet.cell(1, 80, "outside-preview")
    buffer = io.BytesIO()
    book.save(buffer)
    original = buffer.getvalue()
    result = preview_binary(original, "table.xlsx", 1)
    assert result["available"] and result["truncated"]
    assert result["rows"][0][:2] == ["42", "=A2*2"]
    assert len(result["columns"]) == 50
    assert len(result["rows"]) == 1
    assert buffer.getvalue() == original


def test_corrupt_binary_has_readable_failure_without_exposing_content():
    result = preview_binary(b"private-content-invalid-zip", "bad.docx", 20)
    assert not result["available"]
    assert result["content"] == ""
    assert "private-content" not in result["note"]
