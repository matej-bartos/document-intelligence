"""Definice extrakčního schématu a překlad do response schématu Gemini.

Uživatel definuje pole (název, typ, popis, povinnost). Z toho se generuje
schéma, kterým se svazuje odpověď modelu. Každé pole se vrací jako trojice
value / confidence / source_text, takže výsledek jde ověřit proti
zdrojovému dokumentu.

Gemini používá vlastní dialekt (podmnožinu OpenAPI 3.0), ne plné JSON Schema:
nullable se vyjadřuje polem `nullable`, ne unií s null, `additionalProperties`
se nepodporuje vůbec a u řetězců projde jen formát `date-time`. Datum proto
vynucujeme popisem, ne formátem.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Any, Literal

FieldKind = Literal["text", "number", "integer", "date", "boolean", "enum", "table"]

CONFIDENCE_LEVELS = ["high", "medium", "low"]

#: Pořadí od nejjistějšího; používá se pro prahování review fronty.
CONFIDENCE_RANK = {"high": 2, "medium": 1, "low": 0}

_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class SchemaError(ValueError):
    """Neplatná definice schématu — chytáme dřív, než se pošle request."""


@dataclass
class Column:
    """Sloupec tabulkového pole. Vždy skalární, tabulky se nevnořují."""

    name: str
    kind: FieldKind = "text"
    description: str = ""

    def json_type(self) -> dict[str, Any]:
        return _scalar_type(self.kind, self.description)


@dataclass
class Field:
    name: str
    kind: FieldKind = "text"
    description: str = ""
    required: bool = False
    options: list[str] = field(default_factory=list)  # jen pro kind="enum"
    columns: list[Column] = field(default_factory=list)  # jen pro kind="table"

    def validate(self) -> None:
        if not _IDENT_RE.match(self.name):
            raise SchemaError(
                f"Neplatný název pole '{self.name}'. Použij malá písmena, číslice "
                "a podtržítka, začni písmenem (např. 'invoice_number')."
            )
        if self.kind == "enum" and not self.options:
            raise SchemaError(f"Pole '{self.name}' typu enum musí mít alespoň jednu možnost.")
        if self.kind == "table":
            if not self.columns:
                raise SchemaError(f"Tabulkové pole '{self.name}' musí mít alespoň jeden sloupec.")
            seen: set[str] = set()
            for col in self.columns:
                if not _IDENT_RE.match(col.name):
                    raise SchemaError(
                        f"Neplatný název sloupce '{col.name}' v poli '{self.name}'."
                    )
                if col.name in seen:
                    raise SchemaError(
                        f"Duplicitní sloupec '{col.name}' v poli '{self.name}'."
                    )
                seen.add(col.name)


@dataclass
class ExtractionSchema:
    name: str
    description: str = ""
    fields: list[Field] = field(default_factory=list)

    def validate(self) -> None:
        if not self.fields:
            raise SchemaError("Schéma musí obsahovat alespoň jedno pole.")
        seen: set[str] = set()
        for f in self.fields:
            f.validate()
            if f.name in seen:
                raise SchemaError(f"Duplicitní pole '{f.name}'.")
            seen.add(f.name)

    # ---------------------------------------------------------------- export

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExtractionSchema":
        fields = []
        for raw in data.get("fields", []):
            cols = [Column(**c) for c in raw.get("columns", [])]
            fields.append(
                Field(
                    name=raw["name"],
                    kind=raw.get("kind", "text"),
                    description=raw.get("description", ""),
                    required=raw.get("required", False),
                    options=raw.get("options", []),
                    columns=cols,
                )
            )
        return cls(
            name=data.get("name", "untitled"),
            description=data.get("description", ""),
            fields=fields,
        )

    # ------------------------------------------------------- response schema

    def to_response_schema(self) -> dict[str, Any]:
        """Přeloží schéma do dialektu, který bere Gemini `response_schema`.

        Všechna pole jsou v `required` — model tedy musí vrátit každé z nich.
        Nenalezenou hodnotu vrací jako null, ne jako chybějící klíč; to je
        záměr, protože „nenašel jsem" a „zapomněl jsem" chceme rozlišit.

        `property_ordering` drží pořadí klíčů stabilní. U generativních modelů
        to není kosmetika — pořadí, ve kterém pole vznikají, ovlivňuje výsledek,
        takže ho chceme mít deterministické napříč běhy.
        """
        self.validate()

        properties = {f.name: _field_wrapper(f) for f in self.fields}
        names = [f.name for f in self.fields]

        return {
            "type": "OBJECT",
            "properties": {
                "fields": {
                    "type": "OBJECT",
                    "properties": properties,
                    "required": names,
                    "property_ordering": names,
                },
                "document_type": {
                    "type": "STRING",
                    "description": (
                        "Stručné označení typu dokumentu, jak jej model rozpoznal "
                        "(např. 'faktura', 'dodací list', 'neznámý')."
                    ),
                },
                "notes": {
                    "type": "STRING",
                    "nullable": True,
                    "description": (
                        "Poznámka pro člověka, pokud je s dokumentem něco v nepořádku "
                        "(nečitelný sken, chybějící stránka, rozpor v číslech). "
                        "Jinak null."
                    ),
                },
            },
            "required": ["fields", "document_type", "notes"],
            "property_ordering": ["fields", "document_type", "notes"],
        }


# --------------------------------------------------------------------- utils

_GEMINI_TYPES: dict[str, str] = {
    "number": "NUMBER",
    "integer": "INTEGER",
    "boolean": "BOOLEAN",
    "date": "STRING",
    "text": "STRING",
    "enum": "STRING",
}


def _scalar_type(kind: FieldKind, description: str = "") -> dict[str, Any]:
    """Skalární typ hodnoty, vždy nullable (null = v dokumentu nenalezeno)."""
    schema: dict[str, Any] = {
        "type": _GEMINI_TYPES.get(kind, "STRING"),
        "nullable": True,
    }

    desc = description
    if kind == "date":
        # Gemini u řetězců podporuje jen format "date-time", takže tvar data
        # vynucujeme popisem — jinak by request spadl na validaci schématu.
        desc = f"{description} Vrať ve formátu YYYY-MM-DD.".strip()

    if desc:
        schema["description"] = desc
    return schema


def _field_wrapper(f: Field) -> dict[str, Any]:
    """Obalí hodnotu pole do trojice value / confidence / source_text."""
    if f.kind == "table":
        row_props = {c.name: c.json_type() for c in f.columns}
        col_names = [c.name for c in f.columns]
        value_schema: dict[str, Any] = {
            "type": "ARRAY",
            "description": f.description or f"Řádky tabulky {f.name}",
            "items": {
                "type": "OBJECT",
                "properties": row_props,
                "required": col_names,
                "property_ordering": col_names,
            },
        }
    elif f.kind == "enum":
        value_schema = {"type": "STRING", "enum": f.options, "nullable": True}
        if f.description:
            value_schema["description"] = f.description
    else:
        value_schema = _scalar_type(f.kind, f.description)

    return {
        "type": "OBJECT",
        "properties": {
            "value": value_schema,
            "confidence": {
                "type": "STRING",
                "enum": CONFIDENCE_LEVELS,
                "description": (
                    "high = hodnota je v dokumentu explicitně a jednoznačně uvedena; "
                    "medium = hodnota je odvozená nebo částečně nečitelná; "
                    "low = hádáš, nebo je v dokumentu rozpor."
                ),
            },
            "source_text": {
                "type": "STRING",
                "nullable": True,
                "description": (
                    "Doslovný úryvek z dokumentu, ze kterého hodnota pochází. "
                    "Nikdy neparafrázuj. Pokud hodnota v dokumentu není, vrať null."
                ),
            },
        },
        "required": ["value", "confidence", "source_text"],
        "property_ordering": ["value", "confidence", "source_text"],
    }
