import re
from typing import Any, Optional

NUM_RE = re.compile(r"[-+]?\d+(\.\d+)?")
BRACKET_CHOICE_RE = re.compile(r"\[([^\|\]]+)\|([^\]]+)\]")
PRICE_CURRENCY_ALIASES = {
    "divine": {"divine", "divine-orb"},
    "exalted": {"exalted", "exalted-orb"},
}


def expand_bracket_variants(text: str) -> list[str]:
    variants = {text}
    for _ in range(4):
        new_values: set[str] = set()
        for value in variants:
            match = BRACKET_CHOICE_RE.search(value)
            if not match:
                new_values.add(value)
                continue
            first, second = match.group(1), match.group(2)
            new_values.add(BRACKET_CHOICE_RE.sub(first, value, count=1))
            new_values.add(BRACKET_CHOICE_RE.sub(second, value, count=1))
            new_values.add(value)
        variants = new_values
    return sorted(variants)


def normalize_mod_text(text: str) -> str:
    normalized = NUM_RE.sub("#", text.strip())
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.lower().strip()


def extract_mod_lines(item: dict[str, Any]) -> list[str]:
    lines: list[str] = []

    def add_from(value: Any) -> None:
        if isinstance(value, list):
            for entry in value:
                if isinstance(entry, str) and entry.strip():
                    lines.append(entry.strip())

    for key in (
        "explicitMods",
        "implicitMods",
        "enchantMods",
        "craftedMods",
        "fracturedMods",
        "scourgeMods",
        "crucibleMods",
        "desecratedMods",
    ):
        add_from(item.get(key))

    extended = item.get("extended") or {}
    mods = extended.get("mods") or {}
    if isinstance(mods, dict):
        for key in ("explicit", "implicit", "enchant", "crafted", "fractured", "scourge", "desecrated"):
            add_from(mods.get(key))
    else:
        add_from(mods)

    if isinstance(mods, list):
        for entry in mods:
            if isinstance(entry, dict) and isinstance(entry.get("text"), str) and entry["text"].strip():
                lines.append(entry["text"].strip())

    uniq: list[str] = []
    seen = set()
    for line in lines:
        if line not in seen:
            seen.add(line)
            uniq.append(line)
    return uniq


def parse_price(listing: dict[str, Any], currency: str) -> Optional[float]:
    price = (listing or {}).get("price")
    if not price:
        return None
    amount = price.get("amount")
    if amount is None:
        return None

    cur = str(price.get("currency", "")).lower()
    allowed = PRICE_CURRENCY_ALIASES.get(currency, {currency})
    if cur not in allowed:
        return None
    return float(amount)
