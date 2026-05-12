from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

from .models import AnswerRecord, KeywordAnalysisRecord


class AnswerStorage:
    def __init__(self, run_dir: str | Path) -> None:
        self.run_dir = Path(run_dir)
        self.screenshots_dir = self.run_dir / "screenshots"
        self.html_dir = self.run_dir / "html"
        self.api_responses_dir = self.run_dir / "api_responses"
        self.raw_answers_path = self.run_dir / "raw_answers.jsonl"
        self.keyword_analysis_path = self.run_dir / "keyword_analysis.jsonl"

    def prepare(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        self.html_dir.mkdir(parents=True, exist_ok=True)
        self.api_responses_dir.mkdir(parents=True, exist_ok=True)

    def answer_paths(self, platform_id: str, question_id: str) -> tuple[Path, Path]:
        safe_platform = _safe_name(platform_id)
        safe_question = _safe_name(question_id)
        screenshot_path = self.screenshots_dir / f"{safe_platform}_{safe_question}.png"
        html_path = self.html_dir / f"{safe_platform}_{safe_question}.html"
        return screenshot_path, html_path

    def api_response_path(self, platform_id: str, question_id: str) -> Path:
        safe_platform = _safe_name(platform_id)
        safe_question = _safe_name(question_id)
        return self.api_responses_dir / f"{safe_platform}_{safe_question}.json"

    def write_answer(self, record: AnswerRecord) -> None:
        self._append_jsonl(self.raw_answers_path, _without_none(asdict(record)))

    def write_keyword_analysis(self, record: KeywordAnalysisRecord) -> None:
        payload = asdict(record)
        payload["keyword_analysis"] = [asdict(match) for match in record.keyword_analysis]
        self._append_jsonl(self.keyword_analysis_path, payload)

    def rewrite_keyword_analysis(self, records: Iterable[KeywordAnalysisRecord]) -> None:
        with self.keyword_analysis_path.open("w", encoding="utf-8") as handle:
            for record in records:
                payload = asdict(record)
                payload["keyword_analysis"] = [asdict(match) for match in record.keyword_analysis]
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def rewrite_answers(self, records: Iterable[AnswerRecord]) -> None:
        with self.raw_answers_path.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(_without_none(asdict(record)), ensure_ascii=False) + "\n")

    @staticmethod
    def _append_jsonl(path: Path, payload: dict) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def read_raw_answers(path: str | Path) -> list[AnswerRecord]:
    records: list[AnswerRecord] = []
    source = Path(path)
    if not source.exists():
        return records
    with source.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                payload = json.loads(line)
                allowed = AnswerRecord.__dataclass_fields__.keys()
                records.append(AnswerRecord(**{key: value for key, value in payload.items() if key in allowed}))
    return records


def _safe_name(value: str) -> str:
    return "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in value)


def _without_none(payload: dict) -> dict:
    return {key: value for key, value in payload.items() if value is not None}
