"""Deterministic horizontal whitespace cleanup; never changes data tokens."""

import re


def normalize_whitespace(text: str) -> str:
    return "\n".join(re.sub(r"[ \t]+", " ", line).strip(" \t") for line in text.split("\n"))



def whitespace_only_request(intent: str) -> bool:
    return bool(re.search(
        r"(?:清洗|清理|整理|去掉|去除|规范).*?(?:空格|空白)|(?:trim|whitespace)", intent, re.I
    )) and not re.search(
        r"替换|改为|新增|添加|删除|总结|摘要|翻译|replace|add|delete|summari|translat", intent, re.I
    )


def verify_office_cleanup(original: bytes, edited: bytes, intent: str, kind: str) -> bool | None:
    """Verify a whitespace-only request against the entire normalized Office package."""
    if not whitespace_only_request(intent):
        return None
    import io
    import zipfile
    from xml.etree import ElementTree

    if kind == "docx":
        from forgeflow.documents.docx_edit import EditOp, apply_edits
        expected, _ = apply_edits(original, [EditOp(op="normalize_whitespace")])
    elif kind == "xlsx":
        from forgeflow.documents.sheet_edit import SheetEditOp, apply_edits
        expected, _ = apply_edits(original, [SheetEditOp(op="normalize_whitespace")])
    else:
        raise ValueError("Unsupported cleanup format")

    def contents(data: bytes) -> dict[str, bytes]:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        if "docProps/core.xml" in entries:
            root = ElementTree.fromstring(entries["docProps/core.xml"])
            # Saving may update the modification time; document properties otherwise count.
            for node in root.findall("{http://purl.org/dc/terms/}modified"):
                root.remove(node)
            entries["docProps/core.xml"] = ElementTree.tostring(root)
        return entries

    return contents(expected) == contents(edited)
