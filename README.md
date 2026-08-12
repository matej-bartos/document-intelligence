# Document Intelligence

Platforma pro extrakci strukturovaných dat z dokumentů. Nahraješ PDF, sken nebo
obrázek, nadefinuješ si, jaká pole chceš, a dostaneš strukturovaná data — u každé
hodnoty s mírou jistoty a doslovným úryvkem z dokumentu, ze kterého pochází.

Postavené na Google Gemini ve **free tieru** — provoz nic nestojí. Vision je
součástí modelu, takže skeny nepotřebují samostatné OCR.

## Spuštění

```bash
pip install -r requirements.txt
cp .env.example .env
streamlit run app.py
```

Do `.env` doplň `GEMINI_API_KEY`. Klíč získáš zdarma a bez platební karty na
[aistudio.google.com/apikey](https://aistudio.google.com/apikey).

## Co to umí

| Sekce | K čemu je |
|---|---|
| **Extrakce** | Nahraješ dávku dokumentů, vybereš schéma, dostaneš tabulku výsledků + export do CSV / Excel / JSON |
| **Schémata** | Nadefinuješ vlastní pole — typ, popis, povinnost. Popis pole je zároveň instrukce pro model |
| **Kontrola** | Fronta polí, u kterých si model nebyl jistý. Oprava se ukládá vedle původní hodnoty, ne místo ní |
| **Metriky** | Latence a spotřeba tokenů na dokument, podíl polí k ruční kontrole, srovnání modelů a nastavení `effort` |

Předpřipravené šablony: faktura (CZ), účtenka, smlouva.

## Návrhová rozhodnutí

**Schema-driven, ne hardcoded.** Extrakci řídí uživatelem definované schéma, ne
prompt napsaný na jeden typ dokumentu. Schéma se překládá na response schéma,
kterým je odpověď modelu svázaná — odpadá parsování volného textu i retry smyčky
na rozbité JSON.

**Confidence jako ordinální škála, ne číslo.** Model vrací `high` / `medium` /
`low`. Jazykové modely neumí kalibrovat pravděpodobnost — číslo `0.87` by
budilo dojem přesnosti, kterou za tím nikdo nemá. Tříúrovňová škála je to
nejjemnější rozlišení, které model zvládne konzistentně.

**Source grounding u každého pole.** Model musí vrátit doslovný úryvek, ze
kterého hodnotu vzal. Je to nejlevnější obrana proti halucinaci — hodnota bez
zdroje je podezřelá a člověk ji umí ověřit za pár sekund. Zároveň to odhalí
záměny typu `IČO 2708244O` vs. `27082440`.

**Nenalezená hodnota je `null`, ne chybějící klíč.** Všechna pole jsou
v `required`, takže se rozlišuje „v dokumentu to není" od „model na pole
zapomněl".

**Lidská oprava nepřepisuje výstup modelu.** Obě hodnoty zůstávají v databázi
vedle sebe. Bez toho by nešlo zpětně změřit, jak je model přesný — a to je jediný
způsob, jak zjistit, jestli má smysl práh kontroly posouvat.

**`temperature = 0`.** Extrakce má být reprodukovatelná: na stejném dokumentu
chceme stejný výsledek, ne kreativní variaci.

**Provider je izolovaný v jednom souboru.** `core/provider.py` je jediné místo,
které zná konkrétní API. Zbytek aplikace pracuje s `ProviderResponse`, takže
výměna modelu neznamená zásah do schémat, ukládání ani UI.

## Poznámky k implementaci

Gemini nepoužívá plné JSON Schema, ale vlastní podmnožinu OpenAPI 3.0:
nullable se vyjadřuje polem `nullable`, ne unií s `null`,
`additionalProperties` se nepodporuje vůbec a u řetězců projde jen formát
`date-time`. Formát data se proto vynucuje popisem pole, ne `format`.
Překlad řeší `ExtractionSchema.to_response_schema()`.

`property_ordering` drží pořadí klíčů stabilní. U generativních modelů to není
kosmetika — pořadí, ve kterém pole vznikají, ovlivňuje výsledek, takže ho chceme
mít deterministické napříč běhy.

## Struktura

```
app.py              Streamlit UI — 4 sekce
core/
  schema.py         Field / ExtractionSchema → response schéma Gemini
  documents.py      soubor → neutrální části requestu, validace limitů
  provider.py       jediné místo, které zná Gemini API
  extractor.py      orchestrace: schéma + dokument → výsledek s metrikami
  store.py          SQLite: běhy, pole, uložená schémata
  templates.py      předpřipravená schémata
samples/            testovací faktura pro ověření, že pipeline běží
data/               SQLite databáze (v .gitignore)
```

## Modely

Výchozí je `gemini-3.6-flash`. Pinujeme konkrétní verzi, ne alias
`gemini-flash-latest` — za aliasem se model může vyměnit ze dne na den
a naměřená přesnost by přestala platit. U projektu, kde se kvalita měří, je
reprodukovatelnost důležitější než automatický upgrade.

Naměřeno na testovací faktuře (`samples/faktura_test.pdf`, 15 polí):

| Model | Latence | Výsledek |
|---|---|---|
| `gemini-3.6-flash` | ~24 s | 15/15 polí správně, nic ke kontrole |
| `gemini-3.1-flash-lite` | ~3 s | klíčová pole správně, nic ke kontrole |

Pozor: to, že model figuruje ve výpisu `models.list()`, ještě neznamená, že ho
klíč smí volat. Řada 2.5 se novým klíčům hlásí ve výpisu, ale při requestu
vrátí 404 „no longer available to new users". Proto výběr modelů řadí novější
generace první.

