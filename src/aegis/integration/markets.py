"""Configuration catalogue, checked against primary platform sources on 2026-11-16.

A market profile is NOT a claim of live authorization or a completed vendor certification.
Warehouse calendar/timezone must always be configured separately (countries can span zones).
"""
from ..core.util import content_hash

AMAZON_MARKETS = {
    "CA": ("A2EUQ1WTGCTBG2", "NA", "CAD"), "US": ("ATVPDKIKX0DER", "NA", "USD"),
    "MX": ("A1AM78C64UM0Y8", "NA", "MXN"), "BR": ("A2Q3Y263D00KWC", "NA", "BRL"),
    "IE": ("A28R8C7NBKEWEA", "EU", "EUR"), "ES": ("A1RKKUPIHCS9HS", "EU", "EUR"),
    "GB": ("A1F83G8C2ARO7P", "EU", "GBP"), "FR": ("A13V1IB3VIYZZH", "EU", "EUR"),
    "BE": ("AMEN7PMS3EDWL", "EU", "EUR"), "NL": ("A1805IZSGTT6HS", "EU", "EUR"),
    "DE": ("A1PA6795UKMFR9", "EU", "EUR"), "IT": ("APJ6JRA9NG5V4", "EU", "EUR"),
    "SE": ("A2NODRKZP88ZB9", "EU", "SEK"), "ZA": ("AE08WJ6YKNBMC", "EU", "ZAR"),
    "PL": ("A1C3SOZRARQ6R3", "EU", "PLN"), "EG": ("ARBP9OOSHTCHU", "EU", "EGP"),
    "TR": ("A33AVAJ2PDY3EV", "EU", "TRY"), "SA": ("A17E79C6D8DWNP", "EU", "SAR"),
    "AE": ("A2VIGQ35RCS4UG", "EU", "AED"), "IN": ("A21TJRUUN4KGV", "EU", "INR"),
    "SG": ("A19VAU5U5O7RUS", "FE", "SGD"), "AU": ("A39IBJ37TRP1C6", "FE", "AUD"),
    "JP": ("A1VC38T7YXB528", "FE", "JPY"),
}
TIKTOK_MARKETS = {"US": "USD", "GB": "GBP", "DE": "EUR", "FR": "EUR", "IT": "EUR", "ES": "EUR",
                  "IE": "EUR", "AT": "EUR", "BE": "EUR", "NL": "EUR", "PL": "PLN",
                  "MX": "MXN", "BR": "BRL", "JP": "JPY", "SG": "SGD", "MY": "MYR",
                  "TH": "THB", "VN": "VND", "PH": "PHP", "ID": "IDR"}


def platform_source(kind: str, config: dict) -> str | None:
    if kind == "amazon_sp":
        identity = [config["seller_id"], config["marketplace_id"]]
    elif kind == "tiktok_shop":
        identity = [config["shop_id"], config["market"]]
    else:
        return None
    return kind + ":" + content_hash(identity)


def catalogue() -> dict:
    return {"verified_on": "2026-11-16", "verification_level": "configuration-only; live account testing required",
            "amazon": [{"country": code, "marketplace_id": item[0], "region": item[1], "currency": item[2]}
                       for code, item in sorted(AMAZON_MARKETS.items())],
            "tiktok_shop": [{"country": code, "currency": currency} for code, currency in sorted(TIKTOK_MARKETS.items())]}
