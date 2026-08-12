"""Provider vrstva — jediné místo, které zná konkrétní API.

Zbytek aplikace pracuje s `ProviderResponse`, takže výměna modelu
neznamená zásah do schémat, ukládání ani UI.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from google import genai
from google.genai import errors, types

from .documents import DocPart

# Na strojích, kde do HTTPS vstupuje antivirus nebo firemní proxy, selže
# ověření certifikátu proti bundlu, který si Python nese s sebou — podepisující
# CA je jen v úložišti operačního systému. truststore přesměruje ověřování tam.
# Bez toho spadne každé volání na CERTIFICATE_VERIFY_FAILED.
try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:  # pragma: no cover - na čistém systému není potřeba
    pass

#: Pinujeme konkrétní verzi, ne alias `gemini-flash-latest`. Za aliasem se
#: model může vyměnit ze dne na den a naměřená přesnost by přestala platit —
#: u projektu, kde se kvalita měří, je reprodukovatelnost důležitější než
#: automatický upgrade.
DEFAULT_MODEL = "gemini-3.6-flash"

#: Fallback nabídka, když se nepodaří načíst seznam modelů z API.
KNOWN_MODELS = [
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
    "gemini-flash-latest",
    "gemini-pro-latest",
]

#: Modely, které sice umí generateContent, ale na extrakci z dokumentů nejsou:
#: generátory obrázků, převod na řeč, robotika, ovládání počítače.
_EXCLUDED_MARKERS = (
    "image",
    "tts",
    "robotics",
    "computer-use",
    "embedding",
    "omni",
    "customtools",
)

#: Kolik tokenů smí model spotřebovat na uvažování.
#: 0 = vypnuto, -1 = model si rozhodne sám. Tohle je hlavní páka
#: mezi kvalitou a rychlostí, proto je vytažená do UI.
THINKING_BUDGETS = {"low": 0, "medium": 2048, "high": 8192, "xhigh": -1}

MAX_OUTPUT_TOKENS = 16_000


class ProviderError(RuntimeError):
    """Providera se nepodařilo inicializovat (typicky chybí klíč)."""


@dataclass
class ProviderResponse:
    text: str | None = None
    prompt_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cached_tokens: int = 0
    finish_reason: str | None = None
    error: str | None = None


def build_client(api_key: str | None = None) -> genai.Client:
    """Vytvoří klienta. Klíč bere z argumentu, jinak z GEMINI_API_KEY / GOOGLE_API_KEY."""
    key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not key:
        raise ProviderError(
            "Chybí API klíč. Založ zdarma na https://aistudio.google.com/apikey "
            "a ulož ho jako GEMINI_API_KEY — lokálně do .env, "
            "ve Streamlit Cloudu do Settings → Secrets."
        )
    try:
        return genai.Client(api_key=key)
    except Exception as exc:  # noqa: BLE001 - SDK hází různé typy
        raise ProviderError(f"Klienta se nepodařilo vytvořit: {exc}") from exc


def available_models(client: genai.Client) -> list[str]:
    """Textové modely použitelné na extrakci. Při chybě vrací statický seznam.

    Pozor: to, že model figuruje ve výpisu, ještě neznamená, že ho klíč smí
    volat — starší generace vracejí 404 „no longer available to new users"
    až při skutečném requestu. Novější řadíme první, aby výchozí volba
    padla na model, který funguje.
    """
    try:
        names = {
            m.name.removeprefix("models/")
            for m in client.models.list()
            if "generateContent" in (m.supported_actions or [])
        }
    except Exception:  # noqa: BLE001 - offline nebo neplatný klíč
        return KNOWN_MODELS

    usable = [
        n
        for n in names
        if n.startswith("gemini")
        and not any(marker in n for marker in _EXCLUDED_MARKERS)
    ]
    if not usable:
        return KNOWN_MODELS

    # Novější generace první; aliasy (`*-latest`) až na konec.
    def sort_key(name: str) -> tuple:
        head = name.removeprefix("gemini-").split("-")[0]
        try:
            version = float(head)
        except ValueError:
            version = -1.0
        return (-version, "preview" in name, name)

    return sorted(usable, key=sort_key)


def generate(
    client: genai.Client,
    *,
    model: str,
    system_prompt: str,
    parts: list[DocPart],
    instructions: str,
    response_schema: dict[str, Any],
    effort: str = "medium",
) -> ProviderResponse:
    """Jedno volání modelu. Chyby vrací v `ProviderResponse.error`, neháže."""
    contents = [_to_genai_part(p) for p in parts]
    contents.append(types.Part.from_text(text=instructions))

    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        response_mime_type="application/json",
        response_schema=response_schema,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        # Extrakce má být reprodukovatelná — na stejném dokumentu chceme
        # stejný výsledek, ne kreativní variace.
        temperature=0.0,
        thinking_config=types.ThinkingConfig(
            thinking_budget=THINKING_BUDGETS.get(effort, 2048)
        ),
    )

    try:
        response = client.models.generate_content(
            model=model, contents=contents, config=config
        )
    except errors.ClientError as exc:
        return ProviderResponse(error=_client_error_message(exc))
    except errors.ServerError as exc:
        return ProviderResponse(error=f"Chyba na straně serveru: {exc}. Zkus to znovu.")
    except errors.APIError as exc:
        return ProviderResponse(error=f"Chyba API: {exc}")

    return _to_provider_response(response)


# ------------------------------------------------------------------ interní


def _to_genai_part(part: DocPart) -> types.Part:
    if part.kind == "binary":
        return types.Part.from_bytes(data=part.data or b"", mime_type=part.mime_type or "")
    return types.Part.from_text(text=part.text or "")


def _to_provider_response(response: Any) -> ProviderResponse:
    usage = getattr(response, "usage_metadata", None)
    result = ProviderResponse(
        prompt_tokens=_int(getattr(usage, "prompt_token_count", 0)),
        output_tokens=_int(getattr(usage, "candidates_token_count", 0)),
        thinking_tokens=_int(getattr(usage, "thoughts_token_count", 0)),
        cached_tokens=_int(getattr(usage, "cached_content_token_count", 0)),
    )

    # Blokace promptu nastane dřív, než vznikne kandidát — kontrolujeme první.
    feedback = getattr(response, "prompt_feedback", None)
    block_reason = getattr(feedback, "block_reason", None)
    if block_reason:
        result.error = f"Dokument byl zablokován filtrem ({block_reason})."
        return result

    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        result.error = "Model nevrátil žádnou odpověď."
        return result

    finish = getattr(candidates[0], "finish_reason", None)
    result.finish_reason = str(getattr(finish, "value", finish) or "")

    if result.finish_reason == "MAX_TOKENS":
        result.error = (
            "Odpověď byla useknuta na limitu tokenů — schéma je nejspíš příliš "
            "rozsáhlé nebo má dokument moc řádků v tabulce. Zkus rozdělit schéma."
        )
        return result

    if result.finish_reason not in ("STOP", ""):
        result.error = f"Generování skončilo předčasně ({result.finish_reason})."
        return result

    text = getattr(response, "text", None)
    if not text:
        result.error = "Model vrátil prázdnou odpověď."
        return result

    result.text = text
    return result


def _client_error_message(exc: errors.ClientError) -> str:
    code = getattr(exc, "code", None)
    if code == 429:
        return (
            "Vyčerpán limit free tieru (429). Počkej minutu, nebo zkus "
            "gemini-2.5-flash-lite, který má vyšší limity."
        )
    if code in (401, 403):
        return "Neplatný nebo neautorizovaný API klíč (%s)." % code
    if code == 400:
        return f"Request odmítnut (400): {exc}"
    return f"Chyba klienta: {exc}"


def _int(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) else 0
