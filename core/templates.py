"""Předpřipravená schémata. Slouží jako startovní bod — uživatel si je v UI upraví."""

from __future__ import annotations

from .schema import Column, ExtractionSchema, Field

INVOICE_CZ = ExtractionSchema(
    name="faktura_cz",
    description=(
        "Česká přijatá faktura / daňový doklad. Dodavatel je ten, kdo fakturu "
        "vystavil; odběratel ten, komu je určena."
    ),
    fields=[
        Field("invoice_number", "text", "Číslo faktury / variabilní symbol", required=True),
        Field("issue_date", "date", "Datum vystavení"),
        Field("due_date", "date", "Datum splatnosti"),
        Field("taxable_date", "date", "Datum uskutečnění zdanitelného plnění (DUZP)"),
        Field("supplier_name", "text", "Název dodavatele (kdo fakturuje)", required=True),
        Field("supplier_ico", "text", "IČO dodavatele, 8 číslic"),
        Field("supplier_dic", "text", "DIČ dodavatele, např. CZ12345678"),
        Field("customer_name", "text", "Název odběratele (komu je fakturováno)"),
        Field("customer_ico", "text", "IČO odběratele"),
        Field("total_without_vat", "number", "Celkem bez DPH"),
        Field("vat_amount", "number", "Celková částka DPH"),
        Field("total_with_vat", "number", "Celkem k úhradě včetně DPH", required=True),
        Field("currency", "enum", "Měna dokladu", options=["CZK", "EUR", "USD", "GBP"]),
        Field("bank_account", "text", "Číslo účtu nebo IBAN pro platbu"),
        Field(
            "line_items",
            "table",
            "Jednotlivé fakturované položky",
            columns=[
                Column("description", "text", "Popis položky"),
                Column("quantity", "number", "Množství"),
                Column("unit_price", "number", "Jednotková cena bez DPH"),
                Column("vat_rate", "number", "Sazba DPH v procentech"),
                Column("total", "number", "Celkem za položku"),
            ],
        ),
    ],
)

RECEIPT = ExtractionSchema(
    name="uctenka",
    description="Účtenka z pokladny nebo terminálu, typicky pro vyúčtování výdajů.",
    fields=[
        Field("merchant_name", "text", "Název obchodu nebo provozovny", required=True),
        Field("merchant_ico", "text", "IČO provozovatele"),
        Field("purchase_date", "date", "Datum nákupu"),
        Field("total_amount", "number", "Celková zaplacená částka", required=True),
        Field("vat_amount", "number", "Částka DPH, pokud je uvedena"),
        Field("currency", "enum", "Měna", options=["CZK", "EUR", "USD"]),
        Field(
            "payment_method",
            "enum",
            "Způsob platby",
            options=["karta", "hotovost", "převod", "jiné"],
        ),
        Field(
            "expense_category",
            "enum",
            "Do jaké nákladové kategorie účtenka spadá",
            options=[
                "cestovné",
                "strava",
                "ubytování",
                "kancelář",
                "IT a software",
                "marketing",
                "jiné",
            ],
        ),
    ],
)

CONTRACT = ExtractionSchema(
    name="smlouva",
    description="Obchodní smlouva — klíčové obchodní a termínové podmínky.",
    fields=[
        Field("contract_title", "text", "Název nebo předmět smlouvy", required=True),
        Field("party_a", "text", "První smluvní strana"),
        Field("party_b", "text", "Druhá smluvní strana"),
        Field("effective_date", "date", "Datum účinnosti"),
        Field("expiry_date", "date", "Datum ukončení, pokud je sjednáno"),
        Field("notice_period_days", "integer", "Výpovědní lhůta ve dnech"),
        Field("contract_value", "number", "Celková hodnota plnění"),
        Field("currency", "enum", "Měna", options=["CZK", "EUR", "USD"]),
        Field("auto_renewal", "boolean", "Obsahuje smlouva automatické prodloužení?"),
        Field("governing_law", "text", "Rozhodné právo"),
        Field("termination_clause", "text", "Doslovné znění ustanovení o ukončení"),
    ],
)

TEMPLATES: dict[str, ExtractionSchema] = {
    "Faktura (CZ)": INVOICE_CZ,
    "Účtenka": RECEIPT,
    "Smlouva": CONTRACT,
}
