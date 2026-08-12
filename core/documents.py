"""Načtení nahraných souborů do neutrálních částí requestu.

Tahle vrstva schválně nezná konkrétního providera — vrací jen `DocPart`
(typ, MIME, data). Převod do formátu API řeší `core/provider.py`. Díky tomu
jde vyměnit model bez zásahu do práce se soubory.

PDF posíláme jako celý soubor — Gemini si ho přečte nativně včetně skenů,
takže nepotřebujeme samostatné OCR.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any

#: Limit celkového requestu u inline dat. Nad tím se musí použít File API.
MAX_INLINE_BYTES = 15 * 1024 * 1024
MAX_PDF_PAGES = 1000

IMAGE_MEDIA_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "heic": "image/heic",
}

TEXT_EXTENSIONS = {"txt", "md", "csv", "json", "xml", "html"}

SUPPORTED_EXTENSIONS = ["pdf", *IMAGE_MEDIA_TYPES.keys(), *TEXT_EXTENSIONS]


class DocumentError(ValueError):
    """Soubor nejde odeslat — špatný formát nebo přes limit."""


@dataclass
class DocPart:
    """Jeden kus obsahu requestu. Buď binární data, nebo prostý text."""

    kind: str  # "binary" | "text"
    mime_type: str | None = None
    data: bytes | None = None
    text: str | None = None


@dataclass
class LoadedDocument:
    filename: str
    kind: str  # "pdf" | "image" | "text"
    size_bytes: int
    page_count: int | None
    parts: list[DocPart]
    warnings: list[str] = field(default_factory=list)


def load_document(filename: str, data: bytes) -> LoadedDocument:
    """Převede bajty souboru na části requestu. Validuje limity předem."""
    if not data:
        raise DocumentError(f"Soubor '{filename}' je prázdný.")

    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    warnings: list[str] = []

    if len(data) > MAX_INLINE_BYTES:
        raise DocumentError(
            f"Soubor '{filename}' má {len(data) / 1e6:.1f} MB, limit pro inline "
            f"odeslání je {MAX_INLINE_BYTES / 1e6:.0f} MB. Rozděl ho na části."
        )

    if ext == "pdf":
        page_count = _count_pdf_pages(data, warnings)
        if page_count is not None and page_count > MAX_PDF_PAGES:
            raise DocumentError(
                f"PDF '{filename}' má {page_count} stran, limit je {MAX_PDF_PAGES}."
            )
        parts = [DocPart("binary", mime_type="application/pdf", data=data)]
        return LoadedDocument(filename, "pdf", len(data), page_count, parts, warnings)

    if ext in IMAGE_MEDIA_TYPES:
        parts = [DocPart("binary", mime_type=IMAGE_MEDIA_TYPES[ext], data=data)]
        return LoadedDocument(filename, "image", len(data), 1, parts, warnings)

    if ext in TEXT_EXTENSIONS:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("cp1250", errors="replace")
            warnings.append(
                "Soubor nebyl v UTF-8, dekódován jako CP1250 — zkontroluj diakritiku."
            )
        parts = [DocPart("text", text=f"<document>\n{text}\n</document>")]
        return LoadedDocument(filename, "text", len(data), None, parts, warnings)

    raise DocumentError(
        f"Nepodporovaná přípona '.{ext}'. Podporované: {', '.join(SUPPORTED_EXTENSIONS)}."
    )


def _count_pdf_pages(data: bytes, warnings: list[str]) -> int | None:
    """Spočítá strany PDF. Selhání není fatální — limit ohlídá i server."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return None

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            warnings.append("PDF je zaheslované — extrakce nemusí projít.")
        return len(reader.pages)
    except Exception as exc:  # noqa: BLE001 - poškozené PDF nechceme řešit typově
        warnings.append(f"Počet stran se nepodařilo zjistit ({exc.__class__.__name__}).")
        return None
