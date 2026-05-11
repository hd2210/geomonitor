from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


AnswerStatus = Literal[
    "success",
    "partial_success",
    "failed",
    "login_required",
    "blocked",
    "timeout",
    "empty_answer",
]


@dataclass(frozen=True)
class Question:
    question_id: str
    question: str


@dataclass(frozen=True)
class TargetKeyword:
    keyword: str
    aliases: tuple[str, ...] = field(default_factory=tuple)

    @property
    def all_terms(self) -> tuple[str, ...]:
        terms = [self.keyword, *self.aliases]
        seen: set[str] = set()
        unique: list[str] = []
        for term in terms:
            normalized = term.strip().casefold()
            if term.strip() and normalized not in seen:
                seen.add(normalized)
                unique.append(term.strip())
        return tuple(unique)


@dataclass(frozen=True)
class PlatformSelectors:
    input: str = "textarea, [contenteditable='true']"
    submit: str | None = "button[type='submit']"
    answer_container: str = "main"
    answer_item: str | None = None
    stop_generating: str | None = None
    done_indicator: str | None = None
    login_indicator: str | None = None
    blocked_indicator: str | None = None


@dataclass(frozen=True)
class AIPlatform:
    platform_id: str
    platform_name: str
    url: str | None = None
    method: Literal["browser", "api"] = "browser"
    selectors: PlatformSelectors = field(default_factory=PlatformSelectors)
    model: str | None = None
    api_base_url: str | None = None
    web_search: bool = True
    web_search_vendor: str | None = None


@dataclass(frozen=True)
class RunnerConfig:
    timeout_seconds: int = 120
    min_delay_seconds: float = 10
    max_delay_seconds: float = 30
    headless: bool = False
    browser_profile_dir: str = "./data/browser-profiles"
    browser_channel: str | None = None
    browser_executable_path: str | None = None
    browser_cdp_url: str | None = None
    viewport_width: int = 1440
    viewport_height: int = 1200


@dataclass(frozen=True)
class ScheduleConfig:
    type: Literal["interval", "daily", "weekly", "cron"]
    every_seconds: int | None = None
    time: str | None = None
    days: tuple[str, ...] = field(default_factory=tuple)
    cron: str | None = None


@dataclass(frozen=True)
class MonitorConfig:
    questions: tuple[Question, ...]
    target_keywords: tuple[TargetKeyword, ...]
    ai_platforms: tuple[AIPlatform, ...]
    schedule: ScheduleConfig | None
    output_dir: str
    runner: RunnerConfig = field(default_factory=RunnerConfig)


@dataclass
class AnswerRecord:
    run_id: str
    timestamp: str
    platform_id: str
    platform_name: str
    question_id: str
    question: str
    answer_text: str | None = None
    screenshot_path: str | None = None
    raw_html_path: str | None = None
    raw_response_path: str | None = None
    status: AnswerStatus = "failed"
    error_message: str | None = None
    screenshot_error: str | None = None


@dataclass(frozen=True)
class KeywordMatch:
    keyword: str
    appeared: bool
    rank: int | None
    first_position: int | None
    matched_alias: str | None


@dataclass(frozen=True)
class KeywordAnalysisRecord:
    run_id: str
    platform_id: str
    question_id: str
    keyword_analysis: tuple[KeywordMatch, ...]


@dataclass(frozen=True)
class PlatformSummary:
    run_id: str
    platform_id: str
    keyword: str
    total_questions: int
    appeared_count: int
    appearance_rate: float
    ranks: tuple[int, ...]
    avg_rank: float | None
    best_rank: int | None
    missed_count: int


@dataclass(frozen=True)
class GlobalSummary:
    run_id: str
    keyword: str
    total_answers: int
    appeared_count: int
    appearance_rate: float
    ranks: tuple[int, ...]
    avg_rank: float | None
    best_rank: int | None
    platform_breakdown: tuple[dict[str, Any], ...]


def make_run_id(now: datetime | None = None) -> str:
    current = now or datetime.now().astimezone()
    return current.strftime("%Y%m%d_%H%M%S")
