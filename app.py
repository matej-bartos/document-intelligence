"""Document Intelligence — Streamlit rozhraní.

Nahraj dokumenty, vyber nebo nadefinuj schéma polí, dostaneš strukturovaná
data s confidence a zdrojovým úryvkem u každé hodnoty.
"""

from __future__ import annotations

import io
import json
import os
from typing import Any

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from core import documents, extractor, provider, store
from core.schema import Column, ExtractionSchema, Field, SchemaError
from core.templates import TEMPLATES

load_dotenv()

st.set_page_config(page_title="Document Intelligence", page_icon="📄", layout="wide")

CONFIDENCE_BADGE = {"high": "🟢", "medium": "🟡", "low": "🔴"}


# ------------------------------------------------------------------ zdroje


@st.cache_resource
def get_connection():
    return store.connect()


def _resolve_api_key() -> str | None:
    """Lokálně klíč čteme z .env, v Streamlit Cloudu ze Secrets.

    `st.secrets` sahá na soubor, který lokálně nemusí existovat — bez odchycení
    by aplikace spadla ještě předtím, než stihne nabídnout .env.
    """
    try:
        key = st.secrets.get("GEMINI_API_KEY")
        if key:
            return str(key)
    except Exception:  # noqa: BLE001 - lokálně secrets.toml běžně chybí
        pass
    return os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")


@st.cache_resource
def get_client():
    """Chybějící klíč hlásíme v UI, ne výjimkou."""
    try:
        return provider.build_client(_resolve_api_key()), None
    except provider.ProviderError as exc:
        return None, str(exc)


@st.cache_data(ttl=600)
def get_models() -> list[str]:
    if client is None:
        return provider.KNOWN_MODELS
    return provider.available_models(client)


conn = get_connection()
client, client_error = get_client()


def all_schemas() -> dict[str, ExtractionSchema]:
    """Šablony plus schémata uložená uživatelem (ta mají přednost)."""
    return {**TEMPLATES, **store.load_schemas(conn)}


# -------------------------------------------------------------- postranní


st.sidebar.title("📄 Document Intelligence")
page = st.sidebar.radio(
    "Sekce",
    ["Extrakce", "Schémata", "Kontrola", "Metriky"],
    label_visibility="collapsed",
)

if client is None:
    st.sidebar.error(client_error)
    st.sidebar.markdown(
        "Klíč je zdarma a bez platební karty: "
        "[aistudio.google.com/apikey](https://aistudio.google.com/apikey)"
    )
    model = provider.DEFAULT_MODEL
else:
    st.sidebar.success("Připojeno ke Gemini API")
    models = get_models()
    default_index = (
        models.index(provider.DEFAULT_MODEL) if provider.DEFAULT_MODEL in models else 0
    )
    model = st.sidebar.selectbox(
        "Model",
        models,
        index=default_index,
        help=(
            "Flash je výchozí kompromis rychlosti a přesnosti. Lite má vyšší "
            "limity ve free tieru, Pro je přesnější na nekvalitních skenech, "
            "ale limity vyčerpáš rychleji."
        ),
    )

st.sidebar.caption("Běží na free tieru — bez nákladů, ale s denním limitem requestů.")


# --------------------------------------------------------------- extrakce


def render_extraction() -> None:
    st.header("Extrakce")
    st.caption(
        "Nahraj dokumenty a vyber schéma. Model vrátí u každého pole hodnotu, "
        "míru jistoty a doslovný úryvek, ze kterého ji vzal."
    )

    schemas = all_schemas()
    col_a, col_b, col_c = st.columns([2, 1, 1])
    with col_a:
        schema_name = st.selectbox("Schéma", list(schemas.keys()))
    with col_b:
        effort = st.select_slider(
            "Effort",
            options=list(provider.THINKING_BUDGETS),
            value="medium",
            help=(
                "Rozpočet tokenů na uvažování: low ho vypíná úplně, xhigh ho nechá "
                "na modelu. U čitelných strukturovaných dokumentů stačí low/medium; "
                "high nasazuj na nekvalitní skeny a složité tabulky. Dopad na "
                "latenci a spotřebu tokenů uvidíš v sekci Metriky."
            ),
        )
    with col_c:
        threshold = st.selectbox(
            "Práh kontroly",
            ["medium", "high"],
            help=(
                "medium = na člověka jdou jen pole s nízkou jistotou. "
                "high = kontroluje se i vše odvozené."
            ),
        )

    schema = schemas[schema_name]
    with st.expander(f"Pole ve schématu ({len(schema.fields)})"):
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "pole": f.name,
                        "typ": f.kind,
                        "povinné": "ano" if f.required else "",
                        "popis": f.description,
                    }
                    for f in schema.fields
                ]
            ),
            width="stretch",
            hide_index=True,
        )

    extra = st.text_area(
        "Dodatečné pokyny (nepovinné)",
        placeholder="Např.: Faktury jsou od jednoho dodavatele, IČO je vždy v patičce.",
        height=70,
    )

    uploads = st.file_uploader(
        "Dokumenty",
        type=documents.SUPPORTED_EXTENSIONS,
        accept_multiple_files=True,
    )

    disabled = client is None or not uploads
    if st.button("Spustit extrakci", type="primary", disabled=disabled):
        _run_batch(uploads, schema, schema_name, effort, extra)

    if "last_results" in st.session_state:
        _render_results(st.session_state["last_results"], threshold)


def _run_batch(uploads, schema, schema_name, effort, extra) -> None:
    results: list[extractor.ExtractionResult] = []
    progress = st.progress(0.0, text="Startuji…")

    for i, upload in enumerate(uploads, start=1):
        progress.progress((i - 1) / len(uploads), text=f"Zpracovávám {upload.name}…")
        try:
            doc = documents.load_document(upload.name, upload.getvalue())
        except documents.DocumentError as exc:
            st.error(str(exc))
            continue

        for warning in doc.warnings:
            st.warning(f"{upload.name}: {warning}")

        result = extractor.extract(
            client,
            doc,
            schema,
            model=model,
            effort=effort,
            extra_instructions=extra,
        )
        store.save_extraction(conn, result, schema_name)
        results.append(result)

    progress.progress(1.0, text="Hotovo")
    st.session_state["last_results"] = results


def _render_results(results: list[extractor.ExtractionResult], threshold: str) -> None:
    ok = [r for r in results if r.ok]
    st.divider()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Dokumentů", len(results))
    c2.metric("Úspěšně", f"{len(ok)}/{len(results)}")
    c3.metric(
        "Tokenů celkem",
        f"{sum(r.prompt_tokens + r.output_tokens for r in results):,}",
    )
    if results:
        c4.metric(
            "Průměrná latence",
            f"{sum(r.latency_ms for r in results) / len(results) / 1000:.1f} s",
        )

    for result in results:
        flagged = result.needs_review(threshold) if result.ok else []
        label = "✅" if result.ok and not flagged else ("⚠️" if result.ok else "❌")
        suffix = f" — {len(flagged)} polí ke kontrole" if flagged else ""

        with st.expander(f"{label} {result.filename}{suffix}", expanded=len(results) == 1):
            if result.error:
                st.error(result.error)
                continue

            if result.notes:
                st.warning(f"Poznámka modelu: {result.notes}")

            st.caption(
                f"Typ: {result.document_type or 'neurčeno'} · "
                f"{result.model} / effort {result.effort} · "
                f"{result.latency_ms / 1000:.1f} s · "
                f"{result.prompt_tokens:,} vstupních / {result.output_tokens:,} "
                f"výstupních tokenů"
                + (
                    f" · {result.thinking_tokens:,} na uvažování"
                    if result.thinking_tokens
                    else ""
                )
            )

            scalars = [f for f in result.fields if f.kind != "table"]
            tables = [f for f in result.fields if f.kind == "table"]

            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "": CONFIDENCE_BADGE.get(f.confidence, "⚪"),
                            "pole": f.name,
                            "hodnota": _display(f.value),
                            "zdroj v dokumentu": (f.source_text or "—")[:120],
                        }
                        for f in scalars
                    ]
                ),
                width="stretch",
                hide_index=True,
            )

            for table in tables:
                st.markdown(f"**{table.name}** {CONFIDENCE_BADGE.get(table.confidence, '')}")
                if table.value:
                    st.dataframe(
                        pd.DataFrame(table.value), width="stretch", hide_index=True
                    )
                else:
                    st.caption("Žádné řádky nenalezeny.")

    if ok:
        st.divider()
        _render_export(ok)


def _render_export(results: list[extractor.ExtractionResult]) -> None:
    df = _to_dataframe(results)
    st.subheader("Export")
    c1, c2, c3 = st.columns(3)

    c1.download_button(
        "CSV",
        df.to_csv(index=False).encode("utf-8-sig"),
        "extrakce.csv",
        "text/csv",
        width="stretch",
    )

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name="extrakce")
    c2.download_button(
        "Excel",
        buffer.getvalue(),
        "extrakce.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        width="stretch",
    )

    payload = [
        {
            "filename": r.filename,
            "document_type": r.document_type,
            "notes": r.notes,
            "fields": {
                f.name: {
                    "value": f.value,
                    "confidence": f.confidence,
                    "source_text": f.source_text,
                }
                for f in r.fields
            },
        }
        for r in results
    ]
    c3.download_button(
        "JSON",
        json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        "extrakce.json",
        "application/json",
        width="stretch",
    )


def _to_dataframe(results: list[extractor.ExtractionResult]) -> pd.DataFrame:
    """Jeden řádek na dokument; tabulková pole se serializují do JSONu."""
    rows = []
    for r in results:
        row: dict[str, Any] = {"soubor": r.filename, "typ": r.document_type}
        for f in r.fields:
            row[f.name] = _display(f.value)
            row[f"{f.name}__confidence"] = f.confidence
        rows.append(row)
    return pd.DataFrame(rows)


def _display(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


# ---------------------------------------------------------------- schémata


def render_schemas() -> None:
    st.header("Schémata")
    st.caption(
        "Schéma říká, co chceš z dokumentu dostat. Popis pole je pro model "
        "instrukce — čím konkrétnější, tím lepší výsledek."
    )

    schemas = all_schemas()
    base_name = st.selectbox("Vyjít ze schématu", list(schemas.keys()))
    base = schemas[base_name]

    rows = []
    for f in base.fields:
        if f.kind == "table":
            detail = ", ".join(f"{c.name}:{c.kind}" for c in f.columns)
        else:
            detail = ", ".join(f.options)
        rows.append(
            {
                "name": f.name,
                "kind": f.kind,
                "description": f.description,
                "required": f.required,
                "options / columns": detail,
            }
        )

    st.markdown(
        "U typu `enum` vypiš do posledního sloupce povolené hodnoty oddělené čárkou. "
        "U typu `table` vypiš sloupce ve tvaru `nazev:typ`, např. "
        "`description:text, quantity:number`."
    )

    edited = st.data_editor(
        pd.DataFrame(rows),
        num_rows="dynamic",
        width="stretch",
        column_config={
            "kind": st.column_config.SelectboxColumn(
                options=["text", "number", "integer", "date", "boolean", "enum", "table"],
                required=True,
            ),
            "required": st.column_config.CheckboxColumn(),
        },
        key="schema_editor",
    )

    c1, c2 = st.columns(2)
    new_name = c1.text_input("Název schématu", value=base.name)
    new_desc = c2.text_input("Popis (kontext pro model)", value=base.description)

    if st.button("Uložit schéma", type="primary"):
        try:
            schema = _schema_from_editor(new_name, new_desc, edited)
            schema.validate()
        except SchemaError as exc:
            st.error(str(exc))
        else:
            store.save_schema(conn, schema)
            st.success(f"Schéma '{schema.name}' uloženo.")
            st.rerun()

    saved = store.load_schemas(conn)
    if saved:
        st.divider()
        st.subheader("Uložená schémata")
        for name in saved:
            c1, c2 = st.columns([4, 1])
            c1.write(f"`{name}` — {len(saved[name].fields)} polí")
            if c2.button("Smazat", key=f"del_{name}"):
                store.delete_schema(conn, name)
                st.rerun()


def _schema_from_editor(name: str, description: str, df: pd.DataFrame) -> ExtractionSchema:
    fields: list[Field] = []
    for _, row in df.iterrows():
        field_name = str(row.get("name", "")).strip()
        if not field_name:
            continue

        kind = str(row.get("kind", "text")).strip() or "text"
        detail = str(row.get("options / columns") or "").strip()

        options: list[str] = []
        columns: list[Column] = []
        if kind == "enum":
            options = [p.strip() for p in detail.split(",") if p.strip()]
        elif kind == "table":
            for part in detail.split(","):
                part = part.strip()
                if not part:
                    continue
                col_name, _, col_kind = part.partition(":")
                columns.append(
                    Column(col_name.strip(), (col_kind.strip() or "text"))  # type: ignore[arg-type]
                )

        fields.append(
            Field(
                name=field_name,
                kind=kind,  # type: ignore[arg-type]
                description=str(row.get("description") or "").strip(),
                required=bool(row.get("required", False)),
                options=options,
                columns=columns,
            )
        )

    return ExtractionSchema(name=name.strip(), description=description.strip(), fields=fields)


# ---------------------------------------------------------------- kontrola


def render_review() -> None:
    st.header("Kontrola")
    st.caption(
        "Pole, u kterých si model nebyl jistý. Oprava se ukládá vedle původní "
        "hodnoty — původní se nepřepisuje, protože ji potřebujeme pro měření přesnosti."
    )

    levels = st.multiselect(
        "Zobrazit úrovně jistoty", ["low", "medium"], default=["low"]
    )
    if not levels:
        st.info("Vyber alespoň jednu úroveň.")
        return

    queue = store.review_queue(conn, tuple(levels))
    if not queue:
        st.success("Fronta je prázdná — nic nečeká na kontrolu.")
        return

    st.write(f"**{len(queue)}** polí ke kontrole")

    for item in queue[:50]:
        badge = CONFIDENCE_BADGE.get(item["confidence"], "⚪")
        with st.expander(
            f"{badge} {item['filename']} → `{item['field_name']}` = "
            f"{_display(item['value']) or '(prázdné)'}"
        ):
            st.caption(
                f"Schéma: {item['schema_name']} · {item['created_at']} · "
                f"jistota: {item['confidence']}"
            )
            if item["source_text"]:
                st.markdown("**Zdroj v dokumentu:**")
                st.code(item["source_text"], language=None)
            else:
                st.caption("Model neuvedl zdrojový úryvek — hodnota v dokumentu nebyla.")

            corrected = st.text_input(
                "Správná hodnota",
                value=_display(item["value"]),
                key=f"fix_{item['id']}",
            )
            c1, c2 = st.columns(2)
            if c1.button("Uložit opravu", key=f"save_{item['id']}", type="primary"):
                store.apply_review(conn, item["id"], corrected)
                st.rerun()
            if c2.button("Hodnota je správně", key=f"ok_{item['id']}"):
                store.apply_review(conn, item["id"], None)
                st.rerun()


# ----------------------------------------------------------------- metriky


def render_metrics() -> None:
    st.header("Metriky")
    st.caption(
        "Bez čísel o ceně, latenci a podílu ruční kontroly nejde rozhodnout, "
        "jestli je pipeline nasaditelná."
    )

    s = store.stats(conn)
    if not s["runs"]:
        st.info("Zatím žádné běhy. Spusť extrakci v sekci Extrakce.")
        return

    runs = s["runs"]
    total_tokens = (s["prompt_tokens"] or 0) + (s["output_tokens"] or 0)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Zpracováno dokumentů", runs)
    c2.metric("Úspěšnost", f"{(s['ok_runs'] or 0) / runs:.0%}")
    c3.metric("Průměrná latence", f"{s['avg_latency'] / 1000:.1f} s")
    c4.metric("Tokenů / dokument", f"{total_tokens / runs:,.0f}")

    c1, c2, c3 = st.columns(3)
    c1.metric("Vstupní tokeny", f"{s['prompt_tokens']:,}")
    c2.metric("Výstupní tokeny", f"{s['output_tokens']:,}")
    c3.metric(
        "Na uvažování",
        f"{s['thinking_tokens']:,}",
        help=(
            "Tokeny spotřebované uvažováním modelu. Řídí je slider Effort — "
            "tohle je páka, kterou zkracuješ latenci."
        ),
    )

    counts = s["confidence_counts"]
    total_fields = sum(counts.values())
    if total_fields:
        st.subheader("Rozložení jistoty napříč poli")
        dist = pd.DataFrame(
            [
                {"jistota": level, "počet": counts.get(level, 0)}
                for level in ["high", "medium", "low"]
            ]
        )
        st.bar_chart(dist.set_index("jistota"), horizontal=True)
        low = counts.get("low", 0)
        st.caption(
            f"{low / total_fields:.1%} polí vyžaduje ruční kontrolu. "
            "Tohle je číslo, které rozhoduje o návratnosti automatizace — "
            "při vysokém podílu ušetří pipeline méně práce, než se čeká."
        )

    st.subheader("Historie běhů")
    history = store.list_extractions(conn, limit=100)
    df = pd.DataFrame(history)
    if not df.empty:
        view = df[
            [
                "created_at",
                "filename",
                "schema_name",
                "model",
                "effort",
                "ok",
                "latency_ms",
                "error",
            ]
        ].copy()
        view["ok"] = view["ok"].map({1: "✅", 0: "❌"})
        view["latency_ms"] = (view["latency_ms"] / 1000).round(1)
        view = view.rename(
            columns={
                "created_at": "čas",
                "filename": "soubor",
                "schema_name": "schéma",
                "ok": "stav",
                "latency_ms": "latence (s)",
                "error": "chyba",
            }
        )
        st.dataframe(view, width="stretch", hide_index=True)

        done = df[df["ok"] == 1]
        by_effort = (
            done.groupby(["model", "effort"])
            .agg(
                dokumentů=("id", "count"),
                latence_s=("latency_ms", lambda x: x.mean() / 1000),
                tokenů_na_uvažování=("thinking_tokens", "mean"),
                výstupních_tokenů=("output_tokens", "mean"),
            )
            .round(1)
        )
        if len(by_effort) > 1:
            st.subheader("Srovnání podle modelu a effort")
            st.dataframe(by_effort, width="stretch")
            st.caption(
                "Tohle je ta zajímavá tabulka: ukazuje, co reálně stojí vyšší "
                "kvalita uvažování a jestli se na tvých datech vůbec projeví. "
                "Až budeš mít evaluační sadu, přibude sem sloupec s přesností — "
                "teprve pak jde říct, jestli se vyšší effort vyplatí."
            )


# --------------------------------------------------------------------- run

if page == "Extrakce":
    render_extraction()
elif page == "Schémata":
    render_schemas()
elif page == "Kontrola":
    render_review()
else:
    render_metrics()
