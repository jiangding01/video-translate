"""Zero-dependency leaf: language/width/limit pure functions shared by qa, segment and render.

`reading_units` 的口径对标 BaoCut：标点与空白免费，CJK 语言下 ASCII 字母与数字各计半个单位
（Latin 词在 CJK 字幕里占位与阅读负担都远小于一个汉字）。它只服务 QA 的 `reading_speed` 判定；
翻译侧预算 `max_target_characters` 仍按字符数计，不用这套口径。
"""

from __future__ import annotations

from unicodedata import category, east_asian_width


def is_cjk_language(language: str | None) -> bool:
    return bool(language and language.casefold().startswith(("zh", "ja", "ko")))


def is_japanese_or_korean(language: str | None) -> bool:
    return bool(language and language.casefold().startswith(("ja", "ko")))


def character_limit(language: str | None) -> int:
    return 18 if is_cjk_language(language) else 64


def reading_speed_limit(language: str | None) -> int:
    if is_japanese_or_korean(language):
        return 13
    return 9 if is_cjk_language(language) else 20


def reading_units(text: str, language: str | None) -> int:
    """QA 阅读单位数：忽略空白与标点；CJK 下 ASCII 字母数字计 0.5，其余计 1，向上取整。"""
    cjk = is_cjk_language(language)
    halves = 0
    for character in text:
        if category(character)[0] in {"P", "Z"} or character.isspace():
            continue
        halves += 1 if cjk and character.isascii() and character.isalnum() else 2
    return (halves + 1) // 2


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
