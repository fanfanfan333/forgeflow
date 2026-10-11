"""Deterministic horizontal whitespace cleanup; never changes data tokens."""

import re


def normalize_whitespace(text: str) -> str:
    return "\n".join(re.sub(r"[ \t]+", " ", line).strip(" \t") for line in text.split("\n"))
