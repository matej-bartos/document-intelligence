"""Extrakční jádro — jedno volání modelu na jeden dokument.

Odpověď je svázaná response schématem, takže nemusíme parsovat volný text
ani řešit retry na rozbité JSON. Model vrací u každého pole hodnotu,
confidence a doslovný zdrojový úryvek.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from google import genai

from . import provider
from .documents import LoadedDocument
from .schema import CONFIDENCE_RANK, ExtractionSchema

#: Free tier nic nestojí. Sazby za 1M tokenů si doplň z aktuálního ceníku,
#: pokud chceš promítnout, co by dávka stála v placeném režimu — projekce
#: se pak objeví v Metrikách. Nechávám nuly, ať čísla nelžou.
PAID_RATES: dict[str, dict[str, float]] = {}

SYSTEM_PROMPT = """\
Jsi extrakční engine pro dokumenty. Dostaneš jeden dokument a schéma polí, \
která z něj máš vytáhnout. Vracíš strukturovaná data podle zadaného schématu.

Pravidla, která platí bez výjimky:

1. Extrahuj jen to, co v dokumentu skutečně je. Nikdy nedopočítávej, \
nedomýšlej ani nedoplňuj hodnoty z obecné znalosti. Když pole v dokumentu není, \
vrať value = null a confidence = "low".

2. Pole source_text musí být doslovný úryvek z dokumentu, ze kterého hodnota \
pochází — zkopírovaný znak po znaku, bez parafráze a bez úprav. Slouží k tomu, \
aby si člověk mohl hodnotu ověřit. Když je value null, je null i source_text.

3. Confidence nastavuj střízlivě:
   - "high" = hodnota je v dokumentu explicitně uvedená a jednoznačně čitelná
   - "medium" = hodnota je odvozená z kontextu, částečně nečitelná, nebo je \
v dokumentu na více místech v mírně odlišné podobě
   - "low" = hádáš, dokument je v tom místě nečitelný, nebo si hodnoty odporují
   Radši označ pole jako "low" a nech ho projít kontrolou, než abys tipoval \
s vysokou jistotou. Falešná jistota je horší než přiznaná nejistota.

4. Čísla vracej jako čísla, bez měnových symbolů a bez oddělovačů tisíců. \
Desetinnou čárku převeď na tečku. Data vracej ve formátu YYYY-MM-DD; \
u českých dokumentů počítej s formátem DD.MM.YYYY.

5. Do pole notes napiš krátkou poznámku, pokud je s dokumentem něco v nepořádku \
— nečitelný sken, zjevně chybějící stránka, nesedící součet, dokument jiného typu, \
než schéma očekává. Jinak vrať null.
"""


@dataclass
class FieldResult:
    name: str
    value: Any
    confidence: str
    source_text: str | None
    kind: str

    @property
    def is_empty(self) -> bool:
        return self.value is None or self.value == [] or self.value == ""


@dataclass
class ExtractionResult:
    filename: str
    ok: bool
    model: str = provider.DEFAULT_MODEL
    effort: str = "medium"
    fields: list[FieldResult] = field(default_factory=list)
    document_type: str | None = None
    notes: str | None = None
    raw: dict[str, Any] | None = None
    latency_ms: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0
    finish_reason: str | None = None
    error: str | None = None

    def needs_review(self, threshold: str = "medium") -> list[FieldResult]:
        """Pole pod prahem confidence.

        Threshold "medium" znamená: propusť jen "high" a "medium",
        vše ostatní jde na člověka.
        """
        limit = CONFIDENCE_RANK[threshold]
        return [f for f in self.fields if CONFIDENCE_RANK.get(f.confidence, 0) < limit]

    @property
    def review_ratio(self) -> float:
        if not self.fields:
            return 0.0
        return len(self.needs_review()) / len(self.fields)


def estimate_cost(model: str, prompt_tokens: int, output_tokens: int) -> float:
    """Projekce ceny v placeném režimu. Bez vyplněných sazeb vrací 0."""
    rates = PAID_RATES.get(model)
    if not rates:
        return 0.0
    return (
        prompt_tokens * rates.get("input", 0.0)
        + output_tokens * rates.get("output", 0.0)
    ) / 1_000_000


def extract(
    client: genai.Client,
    document: LoadedDocument,
    schema: ExtractionSchema,
    *,
    model: str = provider.DEFAULT_MODEL,
    effort: str = "medium",
    extra_instructions: str = "",
) -> ExtractionResult:
    """Vytáhne z dokumentu pole podle schématu. Chyby vrací, neháže."""
    started = time.perf_counter()

    response = provider.generate(
        client,
        model=model,
        system_prompt=SYSTEM_PROMPT,
        parts=document.parts,
        instructions=_build_instructions(schema, extra_instructions),
        response_schema=schema.to_response_schema(),
        effort=effort,
    )

    result = ExtractionResult(
        filename=document.filename,
        ok=False,
        model=model,
        effort=effort,
        latency_ms=int((time.perf_counter() - started) * 1000),
        prompt_tokens=response.prompt_tokens,
        output_tokens=response.output_tokens,
        thinking_tokens=response.thinking_tokens,
        cached_tokens=response.cached_tokens,
        finish_reason=response.finish_reason,
    )
    result.cost_usd = estimate_cost(model, result.prompt_tokens, result.output_tokens)

    if response.error or not response.text:
        result.error = response.error or "Model nevrátil odpověď."
        return result

    try:
        payload = json.loads(response.text)
    except json.JSONDecodeError as exc:
        result.error = f"Odpověď nešla naparsovat jako JSON: {exc}"
        return result

    result.raw = payload
    result.document_type = payload.get("document_type")
    result.notes = payload.get("notes")
    result.fields = _normalize_fields(payload.get("fields", {}), schema)
    result.ok = True
    return result


# ------------------------------------------------------------------ interní


def _build_instructions(schema: ExtractionSchema, extra: str) -> str:
    lines = [f"Z přiloženého dokumentu vytáhni pole podle schématu '{schema.name}'."]
    if schema.description:
        lines.append(f"Kontext schématu: {schema.description}")

    lines.append("\nPole k extrakci:")
    for f in schema.fields:
        parts = [f"- {f.name} ({f.kind})"]
        if f.description:
            parts.append(f"— {f.description}")
        if f.required:
            parts.append("[povinné]")
        if f.kind == "enum":
            parts.append(f"povolené hodnoty: {', '.join(f.options)}")
        if f.kind == "table":
            cols = ", ".join(f"{c.name} ({c.kind})" for c in f.columns)
            parts.append(f"sloupce: {cols}")
        lines.append(" ".join(parts))

    if extra.strip():
        lines.append(f"\nDodatečné pokyny k tomuto běhu:\n{extra.strip()}")

    return "\n".join(lines)


def _normalize_fields(
    raw_fields: dict[str, Any], schema: ExtractionSchema
) -> list[FieldResult]:
    """Sjednotí odpověď do plochého seznamu ve stejném pořadí jako schéma."""
    results: list[FieldResult] = []
    for f in schema.fields:
        item = raw_fields.get(f.name) or {}
        results.append(
            FieldResult(
                name=f.name,
                value=item.get("value"),
                confidence=item.get("confidence", "low"),
                source_text=item.get("source_text"),
                kind=f.kind,
            )
        )
    return results
