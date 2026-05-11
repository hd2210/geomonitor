from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .ai_platform_runner import AIPlatformRunner
from .astraflow_runner import AstraFlowRunner
from .answer_storage import AnswerStorage, read_raw_answers
from .config_loader import load_config
from .keyword_analyzer import KeywordAnalyzer
from .models import AnswerRecord, KeywordAnalysisRecord, make_run_id
from .scheduler import run_forever
from .statistics_reporter import StatisticsReporter
from .web_server import serve_dashboard


def main() -> None:
    parser = argparse.ArgumentParser(description="AI platform keyword visibility monitor")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run one monitoring cycle")
    run_parser.add_argument("--config", required=True)
    run_parser.add_argument("--run-id")

    schedule_parser = subparsers.add_parser("schedule", help="Run according to schedule config")
    schedule_parser.add_argument("--config", required=True)

    analyze_parser = subparsers.add_parser("analyze", help="Analyze an existing run directory")
    analyze_parser.add_argument("--config", required=True)
    analyze_parser.add_argument("--run-dir", required=True)

    serve_parser = subparsers.add_parser("serve", help="Serve the local results dashboard")
    serve_parser.add_argument("--runs-dir", default="./data/ai_visibility_monitor/runs")
    serve_parser.add_argument("--config", default="./configs/sample_config.json")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)

    args = parser.parse_args()
    if args.command == "run":
        asyncio.run(run_once(args.config, run_id=args.run_id))
    elif args.command == "schedule":
        asyncio.run(schedule(args.config))
    elif args.command == "analyze":
        analyze_existing(args.config, args.run_dir)
    elif args.command == "serve":
        serve_dashboard(args.host, args.port, args.runs_dir, args.config)


async def schedule(config_path: str) -> None:
    config = load_config(config_path)
    if config.schedule is None:
        raise SystemExit("Config does not define schedule.")

    async def job() -> None:
        await run_once(config_path)

    await run_forever(config.schedule, job)


async def run_once(config_path: str, run_id: str | None = None) -> Path:
    config = load_config(config_path)
    current_run_id = run_id or make_run_id()
    run_dir = Path(config.output_dir.format(run_id=current_run_id))
    storage = AnswerStorage(run_dir)
    storage.prepare()

    browser_runner = AIPlatformRunner(config.runner)
    api_runner = AstraFlowRunner(config.runner)
    analyzer = KeywordAnalyzer(config.target_keywords)
    answers: list[AnswerRecord] = []
    analyses: list[KeywordAnalysisRecord] = []

    for platform_index, platform in enumerate(config.ai_platforms):
        for question_index, question in enumerate(config.questions):
            print(f"[{current_run_id}] {platform.platform_id}/{question.question_id}: asking")
            if platform.method == "api":
                raw_response_path = storage.api_response_path(platform.platform_id, question.question_id)
                record = await api_runner.run_question(current_run_id, platform, question, raw_response_path)
            else:
                screenshot_path, html_path = storage.answer_paths(platform.platform_id, question.question_id)
                record = await browser_runner.run_question(current_run_id, platform, question, screenshot_path, html_path)
            storage.write_answer(record)
            answers.append(record)

            analysis = analyzer.analyze(current_run_id, platform.platform_id, question.question_id, record.answer_text)
            storage.write_keyword_analysis(analysis)
            analyses.append(analysis)
            status_line = f"[{current_run_id}] {platform.platform_id}/{question.question_id}: {record.status}"
            if record.error_message:
                status_line += f" - {_short_error(record.error_message)}"
            elif record.screenshot_error:
                status_line += f" - screenshot: {_short_error(record.screenshot_error)}"
            print(status_line)

            is_last = platform_index == len(config.ai_platforms) - 1 and question_index == len(config.questions) - 1
            if not is_last:
                if platform.method == "api":
                    await api_runner.random_delay()
                else:
                    await browser_runner.random_delay()

    reporter = StatisticsReporter(current_run_id, config.ai_platforms, config.target_keywords)
    reporter.write_outputs(run_dir, answers, analyses)
    print(f"Run complete: {run_dir}")
    return run_dir


def analyze_existing(config_path: str, run_dir: str) -> None:
    config = load_config(config_path)
    run_path = Path(run_dir)
    run_id = run_path.name
    answers = read_raw_answers(run_path / "raw_answers.jsonl")
    analyzer = KeywordAnalyzer(config.target_keywords)
    analyses = [
        analyzer.analyze(answer.run_id or run_id, answer.platform_id, answer.question_id, answer.answer_text)
        for answer in answers
    ]
    storage = AnswerStorage(run_path)
    storage.prepare()
    storage.rewrite_keyword_analysis(analyses)
    reporter = StatisticsReporter(run_id, config.ai_platforms, config.target_keywords)
    reporter.write_outputs(run_path, answers, analyses)
    print(f"Analysis complete: {run_path}")


def _short_error(message: str, max_length: int = 180) -> str:
    first_line = " ".join(message.strip().splitlines()[0].split())
    if len(first_line) <= max_length:
        return first_line
    return first_line[: max_length - 3] + "..."


if __name__ == "__main__":
    main()
