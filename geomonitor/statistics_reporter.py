from __future__ import annotations

import csv
from pathlib import Path

from .models import (
    AIPlatform,
    AnswerRecord,
    GlobalSummary,
    KeywordAnalysisRecord,
    PlatformSummary,
    TargetKeyword,
)


class StatisticsReporter:
    def __init__(
        self,
        run_id: str,
        platforms: tuple[AIPlatform, ...] | list[AIPlatform],
        keywords: tuple[TargetKeyword, ...] | list[TargetKeyword],
    ) -> None:
        self.run_id = run_id
        self.platforms = tuple(platforms)
        self.keywords = tuple(keywords)

    def build_platform_summary(
        self,
        answers: list[AnswerRecord],
        analyses: list[KeywordAnalysisRecord],
    ) -> list[PlatformSummary]:
        successful = {key(answer.platform_id, answer.question_id) for answer in answers if answer.status in {"success", "partial_success"}}
        by_pair = {key(a.platform_id, a.question_id): a for a in analyses}
        summaries: list[PlatformSummary] = []

        for platform in self.platforms:
            platform_successes = [pair for pair in successful if pair.startswith(f"{platform.platform_id}\0")]
            total_questions = len(platform_successes)
            for keyword in self.keywords:
                ranks: list[int] = []
                appeared_count = 0
                for pair in platform_successes:
                    analysis = by_pair.get(pair)
                    if not analysis:
                        continue
                    match = next((item for item in analysis.keyword_analysis if item.keyword == keyword.keyword), None)
                    if match and match.appeared:
                        appeared_count += 1
                        if match.rank is not None:
                            ranks.append(match.rank)
                summaries.append(
                    PlatformSummary(
                        run_id=self.run_id,
                        platform_id=platform.platform_id,
                        keyword=keyword.keyword,
                        total_questions=total_questions,
                        appeared_count=appeared_count,
                        appearance_rate=_rate(appeared_count, total_questions),
                        ranks=tuple(ranks),
                        avg_rank=_avg(ranks),
                        best_rank=min(ranks) if ranks else None,
                        missed_count=max(total_questions - appeared_count, 0),
                    )
                )
        return summaries

    def build_global_summary(self, platform_summaries: list[PlatformSummary]) -> list[GlobalSummary]:
        summaries: list[GlobalSummary] = []
        for keyword in self.keywords:
            rows = [row for row in platform_summaries if row.keyword == keyword.keyword]
            total_answers = sum(row.total_questions for row in rows)
            appeared_count = sum(row.appeared_count for row in rows)
            ranks = tuple(rank for row in rows for rank in row.ranks)
            summaries.append(
                GlobalSummary(
                    run_id=self.run_id,
                    keyword=keyword.keyword,
                    total_answers=total_answers,
                    appeared_count=appeared_count,
                    appearance_rate=_rate(appeared_count, total_answers),
                    ranks=ranks,
                    avg_rank=_avg(list(ranks)),
                    best_rank=min(ranks) if ranks else None,
                    platform_breakdown=tuple(
                        {
                            "platform_id": row.platform_id,
                            "appeared_count": row.appeared_count,
                            "appearance_rate": row.appearance_rate,
                        }
                        for row in rows
                    ),
                )
            )
        return summaries

    def write_outputs(
        self,
        run_dir: str | Path,
        answers: list[AnswerRecord],
        analyses: list[KeywordAnalysisRecord],
    ) -> tuple[list[PlatformSummary], list[GlobalSummary]]:
        output = Path(run_dir)
        platform_summary = self.build_platform_summary(answers, analyses)
        global_summary = self.build_global_summary(platform_summary)
        self._write_platform_csv(output / "platform_summary.csv", platform_summary)
        self._write_global_csv(output / "global_summary.csv", global_summary)
        self._write_citation_csvs(output, answers)
        self._write_report(output / "report.md", answers, platform_summary, global_summary)
        return platform_summary, global_summary

    def write_citation_outputs(self, run_dir: str | Path, answers: list[AnswerRecord]) -> None:
        self._write_citation_csvs(Path(run_dir), answers)

    @staticmethod
    def _write_platform_csv(path: Path, rows: list[PlatformSummary]) -> None:
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "run_id",
                    "platform_id",
                    "keyword",
                    "total_questions",
                    "appeared_count",
                    "appearance_rate",
                    "avg_rank",
                    "best_rank",
                    "missed_count",
                ],
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {
                        "run_id": row.run_id,
                        "platform_id": row.platform_id,
                        "keyword": row.keyword,
                        "total_questions": row.total_questions,
                        "appeared_count": row.appeared_count,
                        "appearance_rate": f"{row.appearance_rate:.4f}",
                        "avg_rank": "" if row.avg_rank is None else f"{row.avg_rank:.4f}",
                        "best_rank": "" if row.best_rank is None else row.best_rank,
                        "missed_count": row.missed_count,
                    }
                )

    @staticmethod
    def _write_global_csv(path: Path, rows: list[GlobalSummary]) -> None:
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["run_id", "keyword", "total_answers", "appeared_count", "appearance_rate", "avg_rank", "best_rank"],
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {
                        "run_id": row.run_id,
                        "keyword": row.keyword,
                        "total_answers": row.total_answers,
                        "appeared_count": row.appeared_count,
                        "appearance_rate": f"{row.appearance_rate:.4f}",
                        "avg_rank": "" if row.avg_rank is None else f"{row.avg_rank:.4f}",
                        "best_rank": "" if row.best_rank is None else row.best_rank,
                    }
                )

    @staticmethod
    def _write_citation_csvs(output: Path, answers: list[AnswerRecord]) -> None:
        site_counts: dict[tuple[str, str], int] = {}
        page_counts: dict[tuple[str, str, str, str], int] = {}
        for answer in answers:
            for citation in answer.citations or []:
                site = str(citation.get("site_name") or "").strip()
                url = str(citation.get("url") or "").strip()
                title = str(citation.get("title") or url).strip()
                if not site or not url:
                    continue
                site_counts[(answer.platform_id, site)] = site_counts.get((answer.platform_id, site), 0) + 1
                page_key = (answer.platform_id, site, url, title)
                page_counts[page_key] = page_counts.get(page_key, 0) + 1

        with (output / "citation_summary.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["run_id", "platform_id", "site_name", "citation_count"])
            writer.writeheader()
            for (platform_id, site), count in sorted(site_counts.items(), key=lambda item: (-item[1], item[0][0], item[0][1])):
                writer.writerow({"run_id": answers[0].run_id if answers else "", "platform_id": platform_id, "site_name": site, "citation_count": count})

        with (output / "citation_pages.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["run_id", "platform_id", "site_name", "title", "url", "citation_count"])
            writer.writeheader()
            for (platform_id, site, url, title), count in sorted(page_counts.items(), key=lambda item: (-item[1], item[0][0], item[0][1], item[0][3])):
                writer.writerow(
                    {
                        "run_id": answers[0].run_id if answers else "",
                        "platform_id": platform_id,
                        "site_name": site,
                        "title": title,
                        "url": url,
                        "citation_count": count,
                    }
                )

    def _write_report(
        self,
        path: Path,
        answers: list[AnswerRecord],
        platform_rows: list[PlatformSummary],
        global_rows: list[GlobalSummary],
    ) -> None:
        failures = [answer for answer in answers if answer.status not in {"success", "partial_success"}]
        lines = [
            f"# AI Visibility Monitor Report",
            "",
            f"- Run ID: `{self.run_id}`",
            f"- Platforms: {len(self.platforms)}",
            f"- Questions executed: {len(answers)}",
            f"- Successful answers: {sum(1 for a in answers if a.status in {'success', 'partial_success'})}",
            f"- Keywords: {len(self.keywords)}",
            "",
            "## Platforms",
            "",
        ]
        lines.extend(f"- `{p.platform_id}`: {p.platform_name}" for p in self.platforms)
        lines.extend(["", "## Global Keyword Visibility", "", "| Keyword | Appeared | Total | Rate | Avg Rank | Best Rank |", "|---|---:|---:|---:|---:|---:|"])
        for row in global_rows:
            lines.append(
                f"| {row.keyword} | {row.appeared_count} | {row.total_answers} | {row.appearance_rate:.2%} | {_fmt(row.avg_rank)} | {_fmt(row.best_rank)} |"
            )

        lines.extend(["", "## Platform Keyword Comparison", "", "| Platform | Keyword | Appeared | Total | Rate | Avg Rank | Best Rank | Missed |", "|---|---|---:|---:|---:|---:|---:|---:|"])
        for row in platform_rows:
            lines.append(
                f"| {row.platform_id} | {row.keyword} | {row.appeared_count} | {row.total_questions} | {row.appearance_rate:.2%} | {_fmt(row.avg_rank)} | {_fmt(row.best_rank)} | {row.missed_count} |"
            )

        lines.extend(["", "## Ranking Notes", "", "Ranking uses explicit recommendation/list/table order when at least two monitored keywords appear in list-like rows. Otherwise it falls back to first mention order in the answer text."])

        lines.extend(["", "## Failed Or Abnormal Questions", ""])
        if not failures:
            lines.append("No failed, blocked, login-required, timeout, or empty-answer records.")
        else:
            lines.extend(["| Platform | Question ID | Status | Error |", "|---|---|---|---|"])
            for failure in failures:
                lines.append(
                    f"| {failure.platform_id} | {failure.question_id} | {failure.status} | {(failure.error_message or '').replace('|', '/')} |"
                )

        lines.extend(["", "## Output Files", "", "- `raw_answers.jsonl`", "- `keyword_analysis.jsonl`", "- `platform_summary.csv`", "- `global_summary.csv`", "- `citation_summary.csv`", "- `citation_pages.csv`", "- `api_responses/`"])
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def key(platform_id: str, question_id: str) -> str:
    return f"{platform_id}\0{question_id}"


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _avg(values: list[int]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _fmt(value: float | int | None) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)
