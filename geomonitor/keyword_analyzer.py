from __future__ import annotations

import re

from .models import KeywordAnalysisRecord, KeywordMatch, TargetKeyword


LIST_LINE_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:(?:\d{1,3}|[A-Za-z])[\.)]|[-*+•]|[|])\s*(?P<text>.+?)\s*$"
)
ASCII_WORD_RE = re.compile(r"[A-Za-z0-9]")


class KeywordAnalyzer:
    def __init__(self, target_keywords: tuple[TargetKeyword, ...] | list[TargetKeyword]) -> None:
        self.target_keywords = tuple(target_keywords)

    def analyze(self, run_id: str, platform_id: str, question_id: str, answer_text: str | None) -> KeywordAnalysisRecord:
        text = answer_text or ""
        appearances = {kw.keyword: self._find_first(text, kw) for kw in self.target_keywords}
        ranks = self._rank_keywords(text, appearances)
        matches = tuple(
            KeywordMatch(
                keyword=kw.keyword,
                appeared=appearances[kw.keyword][0] is not None,
                rank=ranks.get(kw.keyword),
                first_position=appearances[kw.keyword][0],
                matched_alias=appearances[kw.keyword][1],
            )
            for kw in self.target_keywords
        )
        return KeywordAnalysisRecord(
            run_id=run_id,
            platform_id=platform_id,
            question_id=question_id,
            keyword_analysis=matches,
        )

    def _find_first(self, text: str, keyword: TargetKeyword) -> tuple[int | None, str | None]:
        best_position: int | None = None
        best_alias: str | None = None
        for alias in keyword.all_terms:
            match = _search_alias(text, alias)
            if match and (best_position is None or match.start() < best_position):
                best_position = match.start()
                best_alias = alias
        return best_position, best_alias

    def _rank_keywords(
        self,
        text: str,
        appearances: dict[str, tuple[int | None, str | None]],
    ) -> dict[str, int | None]:
        explicit_order = self._explicit_list_order(text)
        appeared_keywords = {keyword for keyword, (position, _) in appearances.items() if position is not None}
        ordered: list[str] = []

        for keyword in explicit_order:
            if keyword in appeared_keywords and keyword not in ordered:
                ordered.append(keyword)

        if len(ordered) < 2:
            ordered = [
                keyword
                for keyword, (position, _) in sorted(
                    appearances.items(),
                    key=lambda item: item[1][0] if item[1][0] is not None else 10**12,
                )
                if position is not None
            ]
        else:
            remaining = [
                keyword
                for keyword, (position, _) in sorted(
                    appearances.items(),
                    key=lambda item: item[1][0] if item[1][0] is not None else 10**12,
                )
                if position is not None and keyword not in ordered
            ]
            ordered.extend(remaining)

        return {keyword: index + 1 for index, keyword in enumerate(ordered)}

    def _explicit_list_order(self, text: str) -> list[str]:
        ordered: list[str] = []
        for line in text.splitlines():
            match = LIST_LINE_RE.match(line)
            if not match:
                continue
            line_text = match.group("text")
            keyword = self._first_keyword_in_text(line_text)
            if keyword and keyword not in ordered:
                ordered.append(keyword)
        return ordered

    def _first_keyword_in_text(self, text: str) -> str | None:
        best: tuple[int, str] | None = None
        for keyword in self.target_keywords:
            for alias in keyword.all_terms:
                match = _search_alias(text, alias)
                if match and (best is None or match.start() < best[0]):
                    best = (match.start(), keyword.keyword)
        return best[1] if best else None


def _search_alias(text: str, alias: str) -> re.Match | None:
    alias = alias.strip()
    if not alias:
        return None
    pattern = _alias_pattern(alias)
    return re.search(pattern, text, flags=re.IGNORECASE)


def _alias_pattern(alias: str) -> str:
    escaped = re.escape(alias)
    if ASCII_WORD_RE.search(alias):
        return rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])"
    return escaped
