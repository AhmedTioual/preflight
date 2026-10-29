"""Synthetic demo data: clients, fund registers and client files.

Everything here is fictional. LEIs use the prefix 999900, which is not
allocated to any real issuer, and carry valid ISO 17442 check digits.
"""

from datetime import date

from app.services.template import lei_with_check_digits

NS_EQUITY = lei_with_check_digits("999900NSGLOBEQTY01")
NS_CREDIT = lei_with_check_digits("999900NSEUCREDIT01")
NS_REAL = lei_with_check_digits("999900NSREALASSET1")
RB_DIVERS = lei_with_check_digits("999900RBPATRIDIV01")
RB_BONDS = lei_with_check_digits("999900RBOBLIGEUR01")

# A realistic typo: the letter O typed as the digit 0. The check digits no longer match.
NS_EQUITY_TYPO = NS_EQUITY.replace("GLOB", "GL0B")

CLIENTS = [
    {
        "code": "NORTHSTAR",
        "name": "Northstar Capital",
        "client_type": "Alternative investment fund manager",
        "contact_name": "Laura Weiss",
        "contact_email": "laura.weiss@northstar-capital.example",
        "language": "en",
        "funds": [
            ("Northstar Global Equity Fund", NS_EQUITY, "EUR", "equity", 412_300_000.0, date(2026, 3, 31)),
            ("Northstar European Credit Fund", NS_CREDIT, "EUR", "fixed_income", 268_450_000.0, date(2026, 3, 31)),
            ("Northstar Real Assets Fund", NS_REAL, "EUR", "real_estate", 151_200_000.0, date(2026, 3, 31)),
        ],
    },
    {
        "code": "RIVERBEND",
        "name": "Riverbend Gestion",
        "client_type": "Management company",
        "contact_name": "Julien Moreau",
        "contact_email": "j.moreau@riverbend-gestion.example",
        "language": "fr",
        "funds": [
            ("Riverbend Patrimoine Diversifié", RB_DIVERS, "EUR", "multi_asset", 94_100_000.0, date(2025, 12, 31)),
            ("Riverbend Obligations Euro", RB_BONDS, "EUR", "fixed_income", 141_600_000.0, date(2025, 12, 31)),
        ],
    },
]

# --------------------------------------------------------------------------- Northstar (Excel, English)
NS_HEADERS = [
    "Fund Name", "LEI code", "Reporting date", "Ccy", "Investment strategy", "Net Asset Val (EUR k)",
    "Gross lev. %", "Commitment lev. %", "Liq. <1 day", "Liq. 2-7 days", "Liq. 8-30 days",
    "Liq. 31-90 days", "Liq. >90 days", "Nb of investors", "Top 5 investors %", "Largest investor %",
    "Internal comment",
]  # fmt: skip

# Q2 2026, first file: 3 errors and 2 warnings planted.
NS_Q2_V1 = [
    ["Northstar Global Equity Fund", NS_EQUITY_TYPO, "30/06/2026", "Euro", "Long/short equity", 431_250,
     118, 104, 62, 21, 12, 5, 0, 1284, 31.5, 9.8, "Final"],
    ["Northstar European Credit Fund", NS_CREDIT, "30/06/2026", "Euro", "Credit", 274_900,
     131, 142, 18, 34, 27, 12, 7, 412, 44.2, 14.6, "Leverage figures from risk team"],
    ["Northstar Real Assets Fund", NS_REAL, "30/06/2026", "Euro", "Real estate", 219_600,
     156, 149, 0, 0, 3, 12, 85, 37, 58.1, 63.4, "Includes June subscription"],
]  # fmt: skip

# Q2 2026, corrected file from the client: liquidity and investor concentration fixed.
# The LEI typo is still there (the client missed it); the analyst's correction carries over.
NS_Q2_V2 = [list(r) for r in NS_Q2_V1]
NS_Q2_V2[1][12] = 9  # Credit fund: liquidity now adds up to 100
NS_Q2_V2[2][15] = 23.4  # Real Assets: largest investor below the top 5

# Q3 2026: same layout, clean data.
NS_Q3 = [
    ["Northstar Global Equity Fund", NS_EQUITY, "30/09/2026", "Euro", "Long/short equity", 440_120,
     121, 106, 60, 22, 13, 5, 0, 1302, 30.9, 9.6, "Final"],
    ["Northstar European Credit Fund", NS_CREDIT, "30/09/2026", "Euro", "Credit", 279_350,
     128, 121, 19, 33, 27, 12, 9, 418, 43.7, 14.1, ""],
    ["Northstar Real Assets Fund", NS_REAL, "30/09/2026", "Euro", "Real estate", 224_480,
     152, 146, 0, 0, 4, 12, 84, 39, 57.2, 22.9, ""],
]  # fmt: skip

# --------------------------------------------------------------------------- Riverbend (CSV, French)
RB_HEADERS = [
    "Nom du fonds", "Code LEI", "Date de référence", "Devise", "Stratégie", "Actif net (milliers EUR)",
    "Levier brut (%)", "Levier engagement (%)", "Liquidité 1 jour", "Liquidité 2-7 jours",
    "Liquidité 8-30 jours", "Liquidité 31-90 jours", "Liquidité > 90 jours", "Nombre d'investisseurs",
    "Top 5 investisseurs (%)", "Premier investisseur (%)",
]  # fmt: skip

RB_Q1 = [
    ["Riverbend Patrimoine Diversifié", RB_DIVERS, "31/03/2026", "EUR", "Diversifié", "95 820",
     "142", "118", "0,41", "0,27", "0,19", "0,09", "0,04", "2 315", "21,4", "6,2"],
    ["Riverbend Obligations Euro", RB_BONDS, "31/03/2026", "EUR", "Obligataire", "143 950",
     "109", "103", "0,22", "0,38", "0,26", "0,1", "0,04", "860", "33,8", "11,5"],
]  # fmt: skip

RB_Q2 = [
    ["Riverbend Patrimoine Diversifié", RB_DIVERS, "30/06/2026", "EUR", "Diversifié", "124 310",
     "139", "117", "0,43", "0,26", "0,18", "0,09", "0,04", "2 402", "27,9", "12,8"],
    ["Riverbend Obligations Euro", RB_BONDS, "30/06/2026", "EUR", "Obligataire", "146 220",
     "111", "104", "0,23", "0,37", "0,26", "0,1", "0,04", "871", "33,1", "11,2"],
]  # fmt: skip

SAMPLES = {
    "northstar_q2_v1": {
        "file": "northstar_fund_return_2026-06-30.xlsx",
        "client": "NORTHSTAR",
        "period": date(2026, 6, 30),
        "title": "Northstar, June 2026",
        "description": "First file from a new client. Messy columns, 3 errors and 2 warnings.",
    },
    "northstar_q2_v2": {
        "file": "northstar_fund_return_2026-06-30_v2.xlsx",
        "client": "NORTHSTAR",
        "period": date(2026, 6, 30),
        "title": "Northstar, June 2026, corrected",
        "description": "The client's corrected file, sent after your query.",
    },
    "northstar_q3": {
        "file": "northstar_fund_return_2026-09-30.xlsx",
        "client": "NORTHSTAR",
        "period": date(2026, 9, 30),
        "title": "Northstar, September 2026",
        "description": "Next quarter, same layout. Columns are recognised automatically.",
    },
    "riverbend_q2": {
        "file": "riverbend_declaration_2026-06-30.csv",
        "client": "RIVERBEND",
        "period": date(2026, 6, 30),
        "title": "Riverbend, June 2026",
        "description": "French CSV with decimal commas and liquidity as fractions.",
    },
}
