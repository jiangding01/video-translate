"""Zero-dependency leaf: language/width/limit pure functions shared by qa, segment and render."""

from __future__ import annotations

from unicodedata import east_asian_width


def is_cjk_language(language: str | None) -> bool:
    return bool(language and language.casefold().startswith(("zh", "ja", "ko")))


def character_limit(language: str | None) -> int:
    return 18 if is_cjk_language(language) else 64


def reading_speed_limit(language: str | None) -> int:
    return 9 if is_cjk_language(language) else 20


def display_width(text: str) -> int:
    return sum(2 if east_asian_width(character) in {"W", "F"} else 1 for character in text)


def display_width_limit(language: str | None) -> int:
    limit = character_limit(language)
    return limit * 2 if is_cjk_language(language) else limit


def display_width_review_limit(
    language: str | None,
    *,
    video_width: int | None = None,
    video_height: int | None = None,
) -> int:
    if is_cjk_language(language):
        return display_width_limit(language)
    if not video_width or not video_height:
        return 72
    aspect_ratio = video_width / video_height
    if aspect_ratio < 1.2:
        return 56
    if aspect_ratio < 1.6:
        return 64
    return 72
