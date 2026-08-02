import re


def normalize_text(value: str) -> str:
    normalized = value.lower().replace("ё", "е")
    normalized = re.sub(r"[^a-zа-я0-9]+", " ", normalized)
    normalized = " ".join(normalized.split())
    return normalized
