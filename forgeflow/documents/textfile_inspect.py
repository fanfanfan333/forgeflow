"""INC44 §2.2 — real text / code file inspection (stdlib only).

Layer contract (design §1.3 / §8): this module **only reads** bytes. It never
writes a file and never calls a model — it reports the file's *measured*
shape (lines / chars / encoding / EOL style / BOM presence / a short preview).

Encoding is decided by re-using the **one** honest decoder the resource seam
already ships (:func:`forgeflow.resources.summaries._decode`: UTF-8(BOM) →
GB18030 → Latin-1-with-replacement) so a text file is classified identically
whether it was uploaded as a resource or handed straight to the text plane. A
lossy decode is reported explicitly (``lossy_decode``) — never hidden.

Nothing is guessed: bytes that cannot be decoded at all still decode via the
Latin-1 replacement path, so inspection is total (it never raises on content),
but the lossiness is surfaced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["TextFileStructure", "TextInspectionError", "detect_eol", "inspect_textfile"]

#: How many leading lines the inspect payload exposes.
_PREVIEW_LINES = 20

#: A UTF-8 byte-order mark.
_BOM_UTF8 = b"\xef\xbb\xbf"


class TextInspectionError(ValueError):
    """Raised when bytes cannot be treated as a text file at all."""


def detect_eol(text: str) -> str:
    """The dominant line-ending style: ``"crlf"`` / ``"cr"`` / ``"lf"``.

    Deterministic precedence: any ``\\r\\n`` wins (a mixed file keeps CRLF, the
    stricter / lossless choice), else any bare ``\\r`` (old-Mac), else ``lf``
    (the Unix default, also used for a file with no line break at all).
    """
    if "\r\n" in text:
        return "crlf"
    if "\r" in text:
        return "cr"
    return "lf"


def _decode(data: bytes) -> tuple[str, str]:
    """Re-use the resource seam's single honest decoder."""
    from forgeflow.resources.summaries import _decode as _resource_decode

    return _resource_decode(data)


@dataclass
class TextFileStructure:
    """The measured structure of a text / code file."""

    lines: int = 0
    chars: int = 0
    encoding: str = ""
    eol: str = "lf"
    has_bom: bool = False
    lossy_decode: bool = False
    preview: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lines": self.lines,
            "chars": self.chars,
            "encoding": self.encoding,
            "eol": self.eol,
            "has_bom": self.has_bom,
            "lossy_decode": self.lossy_decode,
            "preview": list(self.preview),
        }


def inspect_textfile(data: bytes, *, filename: str = "") -> TextFileStructure:
    """Read the **real** structure of a text file from its bytes.

    ``filename`` is accepted for symmetry with the other summarisers (the suffix
    is not used to decide anything — the bytes are the truth); it keeps the call
    sites uniform.

    Raises:
        TextInspectionError: ``data`` is empty (nothing to inspect).
    """
    if not isinstance(data, (bytes, bytearray)):
        raise TextInspectionError(f"文本字节非法：{type(data).__name__}")
    if not data:
        raise TextInspectionError("文本字节为空")
    raw = bytes(data)
    has_bom = raw.startswith(_BOM_UTF8)
    text, encoding = _decode(raw)
    lossy = encoding == "latin-1(replace)"
    lines = text.splitlines()
    return TextFileStructure(
        lines=len(lines),
        chars=len(text),
        encoding=encoding,
        eol=detect_eol(text),
        has_bom=has_bom,
        lossy_decode=lossy,
        preview=lines[:_PREVIEW_LINES],
    )
