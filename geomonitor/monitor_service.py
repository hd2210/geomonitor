from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

from .ai_platform_runner import AIPlatformRunner
from .answer_storage import AnswerStorage, read_raw_answers
from .astraflow_runner import AstraFlowRunner
from .config_loader import load_config
from .keyword_analyzer import KeywordAnalyzer
from .llm_client import AstraFlowLLMClient
from .models import AIPlatform, AnswerRecord, KeywordAnalysisRecord, Question, TargetKeyword, make_run_id
from .statistics_reporter import StatisticsReporter
from .user_store import UserStore, now_iso


SUCCESS_STATUSES = {"success", "partial_success"}


class MonitorJobManager:
    def __init__(self, store: UserStore, config_path: Path) -> None:
        self.store = store
        self.config_path = config_path
        self.lock = threading.Lock()
        self.running: set[int] = set()

    def start(self, monitor_id: int) -> None:
        with self.lock:
            if monitor_id in self.running:
                raise ValueError("监测任务已经在运行。")
            self.running.add(monitor_id)
        thread = threading.Thread(target=self._worker, args=(monitor_id,), daemon=True)
        thread.start()

    def retry_failed(self, monitor_id: int) -> None:
        with self.lock:
            if monitor_id in self.running:
                raise ValueError("监测任务已经在运行。")
            self.running.add(monitor_id)
        thread = threading.Thread(target=self._retry_worker, args=(monitor_id,), daemon=True)
        thread.start()

    def retry_answer(self, monitor_id: int, platform_id: str, question_id: str) -> None:
        with self.lock:
            if monitor_id in self.running:
                raise ValueError("监测任务已经在运行。")
            self.running.add(monitor_id)
        thread = threading.Thread(target=self._retry_answer_worker, args=(monitor_id, platform_id, question_id), daemon=True)
        thread.start()

    def _worker(self, monitor_id: int) -> None:
        try:
            asyncio.run(self._run(monitor_id))
        finally:
            with self.lock:
                self.running.discard(monitor_id)

    def _retry_worker(self, monitor_id: int) -> None:
        try:
            asyncio.run(self._retry_failed(monitor_id))
        finally:
            with self.lock:
                self.running.discard(monitor_id)

    def _retry_answer_worker(self, monitor_id: int, platform_id: str, question_id: str) -> None:
        try:
            asyncio.run(self._retry_single_answer(monitor_id, platform_id, question_id))
        finally:
            with self.lock:
                self.running.discard(monitor_id)

    async def _run(self, monitor_id: int) -> None:
        monitor = self.store.get_monitor(monitor_id)
        if not monitor:
            return
        run_id = make_run_id()
        self.store.update_monitor(monitor_id, status="running", run_id=run_id, progress_message="开始监测")
        try:
            config = load_config(self.config_path)
            selected = set(monitor["selected_platforms"])
            platforms = [platform for platform in config.ai_platforms if platform.platform_id in selected]
            questions = [Question(question_id=item["question_id"], question=item["question"]) for item in monitor["questions"]]
            if not platforms:
                raise RuntimeError("没有可用的 AI 平台。")
            if not questions:
                raise RuntimeError("没有可用的问题。")

            run_dir = Path(config.output_dir.format(run_id=run_id))
            storage = AnswerStorage(run_dir)
            storage.prepare()
            self.store.update_monitor(
                monitor_id,
                run_dir=str(run_dir),
                progress_total=len(platforms) * len(questions),
                progress_current=0,
                progress_message="正在查询 AI 平台",
            )

            answers, _ = await self._collect_answers(monitor_id, run_id, platforms, questions, storage, config.runner)
            await self._finalize_analysis(monitor_id, monitor, run_id, run_dir, platforms, answers, config.runner.timeout_seconds, "监测完成")
        except Exception as exc:  # noqa: BLE001
            self.store.update_monitor(
                monitor_id,
                status="failed",
                completed_at=now_iso(),
                error_message=str(exc),
                progress_message=f"监测失败：{exc}",
                notification_message=f"模拟短信：您的 AI 监测任务失败：{exc}",
            )

    async def _retry_failed(self, monitor_id: int) -> None:
        monitor = self.store.get_monitor(monitor_id)
        if not monitor:
            return
        run_id = str(monitor.get("run_id") or "")
        if not run_id:
            raise RuntimeError("当前监测还没有可重试的 run_id。")
        try:
            config = load_config(self.config_path)
            run_dir = Path(monitor.get("run_dir") or config.output_dir.format(run_id=run_id))
            storage = AnswerStorage(run_dir)
            storage.prepare()
            existing_answers = read_raw_answers(storage.raw_answers_path)
            failed_keys = {
                (answer.platform_id, answer.question_id)
                for answer in existing_answers
                if answer.status not in SUCCESS_STATUSES
            }
            if not failed_keys:
                self.store.update_monitor(
                    monitor_id,
                    status="completed",
                    progress_message="没有失败请求需要重试",
                    notification_message=f"模拟短信：您的 AI 监测任务 {run_id} 没有失败请求需要重试。",
                )
                return

            selected = set(monitor["selected_platforms"])
            platform_by_id = {
                platform.platform_id: platform
                for platform in config.ai_platforms
                if platform.platform_id in selected
            }
            question_by_id = {
                item["question_id"]: Question(question_id=item["question_id"], question=item["question"])
                for item in monitor["questions"]
            }
            retry_pairs: list[tuple[AIPlatform, Question]] = []
            missing_pairs: list[tuple[str, str]] = []
            for platform_id, question_id in sorted(failed_keys):
                platform = platform_by_id.get(platform_id)
                question = question_by_id.get(question_id)
                if platform and question:
                    retry_pairs.append((platform, question))
                else:
                    missing_pairs.append((platform_id, question_id))

            if not retry_pairs:
                missing = ", ".join(f"{platform}/{question}" for platform, question in missing_pairs)
                raise RuntimeError(f"失败请求对应的平台或问题已不存在：{missing}")

            self.store.update_monitor(
                monitor_id,
                status="retrying",
                error_message=None,
                completed_at=None,
                progress_current=0,
                progress_total=len(retry_pairs),
                progress_message=_retry_progress_message(config.runner, retry_pairs),
                notification_message=None,
            )
            retried_answers, _ = await self._collect_answer_pairs(monitor_id, run_id, retry_pairs, storage, config.runner)
            merged_answers = _replace_answer_records(existing_answers, retried_answers)
            storage.rewrite_answers(merged_answers)
            platforms = [platform for platform in config.ai_platforms if platform.platform_id in selected]
            await self._finalize_analysis(
                monitor_id,
                monitor,
                run_id,
                run_dir,
                platforms,
                merged_answers,
                config.runner.timeout_seconds,
                "重试完成，统计已更新",
            )
        except Exception as exc:  # noqa: BLE001
            self.store.update_monitor(
                monitor_id,
                status="failed",
                completed_at=now_iso(),
                error_message=str(exc),
                progress_message=f"重试失败：{exc}",
                notification_message=f"模拟短信：您的 AI 监测任务重试失败：{exc}",
            )

    async def _retry_single_answer(self, monitor_id: int, platform_id: str, question_id: str) -> None:
        monitor = self.store.get_monitor(monitor_id)
        if not monitor:
            return
        run_id = str(monitor.get("run_id") or "")
        if not run_id:
            raise RuntimeError("当前监测还没有可重试的 run_id。")
        try:
            config = load_config(self.config_path)
            selected = set(monitor["selected_platforms"])
            platform = next((item for item in config.ai_platforms if item.platform_id == platform_id and item.platform_id in selected), None)
            question = next(
                (
                    Question(question_id=item["question_id"], question=item["question"])
                    for item in monitor["questions"]
                    if item["question_id"] == question_id
                ),
                None,
            )
            if platform is None or question is None:
                raise RuntimeError(f"找不到要重试的平台或问题：{platform_id}/{question_id}")

            run_dir = Path(monitor.get("run_dir") or config.output_dir.format(run_id=run_id))
            storage = AnswerStorage(run_dir)
            storage.prepare()
            existing_answers = read_raw_answers(storage.raw_answers_path)
            existing_answer = next(
                (answer for answer in existing_answers if answer.platform_id == platform_id and answer.question_id == question_id),
                None,
            )
            self.store.update_monitor(
                monitor_id,
                status="retrying",
                error_message=None,
                completed_at=None,
                progress_current=0,
                progress_total=1,
                progress_message=f"正在单条重试：{platform.platform_name}/{question.question_id}",
                notification_message=None,
            )
            if existing_answer and _can_refresh_answer_artifacts(existing_answer) and platform.method == "browser":
                screenshot_path, html_path = storage.answer_paths(platform.platform_id, question.question_id)
                refreshed_answer = await AIPlatformRunner(config.runner).refresh_answer_artifacts(
                    existing_answer,
                    platform,
                    screenshot_path,
                    html_path,
                )
                merged_answers = _replace_answer_records(existing_answers, [refreshed_answer])
                storage.rewrite_answers(merged_answers)
                platforms = [item for item in config.ai_platforms if item.platform_id in selected]
                StatisticsReporter(run_id, platforms, []).write_citation_outputs(run_dir, merged_answers)
                self.store.update_monitor(
                    monitor_id,
                    status="completed",
                    completed_at=now_iso(),
                    progress_current=1,
                    progress_total=1,
                    progress_message="单条补采集完成，已保留原有品牌统计",
                    error_message=None,
                    notification_message=f"模拟短信：您的 AI 监测任务 {run_id} 单条补采集已完成。",
                )
                return

            retried_answers, _ = await self._collect_answer_pairs(monitor_id, run_id, [(platform, question)], storage, config.runner)
            merged_answers = _replace_answer_records(existing_answers, retried_answers)
            storage.rewrite_answers(merged_answers)
            platforms = [item for item in config.ai_platforms if item.platform_id in selected]
            await self._finalize_analysis(
                monitor_id,
                monitor,
                run_id,
                run_dir,
                platforms,
                merged_answers,
                config.runner.timeout_seconds,
                "单条重试完成，统计已更新",
            )
        except Exception as exc:  # noqa: BLE001
            self.store.update_monitor(
                monitor_id,
                status="failed",
                completed_at=now_iso(),
                error_message=str(exc),
                progress_message=f"单条重试失败：{exc}",
                notification_message=f"模拟短信：您的 AI 监测任务单条重试失败：{exc}",
            )

    async def _collect_answers(
        self,
        monitor_id: int,
        run_id: str,
        platforms: list[AIPlatform],
        questions: list[Question],
        storage: AnswerStorage,
        runner_config,
    ) -> tuple[list[AnswerRecord], list[KeywordAnalysisRecord]]:
        return await self._collect_answer_pairs(
            monitor_id,
            run_id,
            [(platform, question) for platform in platforms for question in questions],
            storage,
            runner_config,
        )

    async def _collect_answer_pairs(
        self,
        monitor_id: int,
        run_id: str,
        pairs: list[tuple[AIPlatform, Question]],
        storage: AnswerStorage,
        runner_config,
    ) -> tuple[list[AnswerRecord], list[KeywordAnalysisRecord]]:
        placeholder_analyzer = KeywordAnalyzer([TargetKeyword(keyword="__placeholder__")])
        answers: list[AnswerRecord] = []
        analyses: list[KeywordAnalysisRecord] = []
        state = {"current": 0}
        total = len(pairs)
        lock = asyncio.Lock()

        async def save_record(record: AnswerRecord) -> None:
            analysis = placeholder_analyzer.analyze(run_id, record.platform_id, record.question_id, record.answer_text)
            async with lock:
                storage.write_answer(record)
                storage.write_keyword_analysis(analysis)
                answers.append(record)
                analyses.append(analysis)
                state["current"] += 1
                self.store.update_monitor(
                    monitor_id,
                    progress_current=state["current"],
                    progress_total=total,
                    progress_message=f"{record.platform_name}/{record.question_id}: {record.status}",
                )

        if all(platform.method == "api" for platform, _ in pairs):
            semaphore = asyncio.Semaphore(max(1, runner_config.api_concurrency))

            async def run_api_question(platform: AIPlatform, question: Question) -> None:
                async with semaphore:
                    self.store.update_monitor(
                        monitor_id,
                        progress_current=state["current"],
                        progress_total=total,
                        progress_message=f"{platform.platform_name}/{question.question_id}: 查询中",
                    )
                    api_runner = AstraFlowRunner(runner_config)
                    raw_response_path = storage.api_response_path(platform.platform_id, question.question_id)
                    record = await api_runner.run_question(run_id, platform, question, raw_response_path)
                    await save_record(record)
                    await api_runner.random_delay()

            await asyncio.gather(*(run_api_question(platform, question) for platform, question in pairs))
            return answers, analyses

        semaphore = asyncio.Semaphore(max(1, runner_config.browser_concurrency))
        questions_by_platform: dict[str, tuple[AIPlatform, list[Question]]] = {}
        for platform, question in pairs:
            if platform.platform_id not in questions_by_platform:
                questions_by_platform[platform.platform_id] = (platform, [])
            questions_by_platform[platform.platform_id][1].append(question)

        async def run_browser_platform(platform: AIPlatform, platform_questions: list[Question]) -> None:
            async with semaphore:
                browser_runner = AIPlatformRunner(runner_config)
                api_runner = AstraFlowRunner(runner_config)
                for question in platform_questions:
                    self.store.update_monitor(
                        monitor_id,
                        progress_current=state["current"],
                        progress_total=total,
                        progress_message=f"{platform.platform_name}/{question.question_id}: 查询中",
                    )
                    if platform.method == "api":
                        raw_response_path = storage.api_response_path(platform.platform_id, question.question_id)
                        record = await api_runner.run_question(run_id, platform, question, raw_response_path)
                    else:
                        screenshot_path, html_path = storage.answer_paths(platform.platform_id, question.question_id)
                        record = await browser_runner.run_question(run_id, platform, question, screenshot_path, html_path)
                    await save_record(record)
                    if platform.method == "api":
                        await api_runner.random_delay()
                    else:
                        await browser_runner.random_delay()

        await asyncio.gather(*(run_browser_platform(platform, questions) for platform, questions in questions_by_platform.values()))
        return answers, analyses

    async def _finalize_analysis(
        self,
        monitor_id: int,
        monitor: dict[str, Any],
        run_id: str,
        run_dir: Path,
        platforms: list[AIPlatform],
        answers: list[AnswerRecord],
        timeout_seconds: int,
        done_message: str,
    ) -> None:
        storage = AnswerStorage(run_dir)
        self.store.update_monitor(monitor_id, progress_message="正在提取竞品品牌")
        competitor_payload = await asyncio.to_thread(
            AstraFlowLLMClient(timeout_seconds).extract_competitors,
            monitor["brand_name"],
            monitor["intention"],
            [_answer_to_dict(answer) for answer in answers],
        )
        keywords = _keywords_from_competitors(monitor["brand_name"], competitor_payload)
        analyzer = KeywordAnalyzer(tuple(keywords))
        analyses = [
            analyzer.analyze(answer.run_id, answer.platform_id, answer.question_id, answer.answer_text)
            for answer in answers
        ]
        storage.rewrite_keyword_analysis(analyses)
        reporter = StatisticsReporter(run_id, platforms, keywords)
        reporter.write_outputs(run_dir, answers, analyses)
        self.store.update_monitor(
            monitor_id,
            status="completed",
            completed_at=now_iso(),
            keywords=[{"keyword": item.keyword, "aliases": list(item.aliases)} for item in keywords],
            competitor_payload=competitor_payload,
            progress_current=len(answers),
            progress_total=len(answers),
            progress_message=done_message,
            error_message=None,
            notification_message=f"模拟短信：您的 AI 监测任务 {run_id} 已完成。",
        )


def _keywords_from_competitors(brand_name: str, payload: dict[str, Any]) -> list[TargetKeyword]:
    target = payload.get("target_brand") if isinstance(payload.get("target_brand"), dict) else {}
    target_aliases = _clean_aliases(target.get("aliases", []), brand_name)
    keywords = [TargetKeyword(keyword=brand_name, aliases=tuple(target_aliases))]
    seen = {brand_name.casefold()}
    competitors = payload.get("competitors") if isinstance(payload.get("competitors"), list) else []
    for item in competitors:
        if not isinstance(item, dict):
            continue
        brand = str(item.get("brand", "")).strip()
        if not brand:
            continue
        key = brand.casefold()
        if key in seen:
            continue
        seen.add(key)
        keywords.append(TargetKeyword(keyword=brand, aliases=tuple(_clean_aliases(item.get("aliases", []), brand))))
        if len(keywords) >= 11:
            break
    return keywords


def _clean_aliases(value: Any, keyword: str) -> list[str]:
    if not isinstance(value, list):
        return []
    seen = {keyword.casefold()}
    aliases: list[str] = []
    for item in value:
        alias = str(item).strip()
        key = alias.casefold()
        if alias and key not in seen:
            seen.add(key)
            aliases.append(alias)
    return aliases


def _answer_to_dict(answer: AnswerRecord) -> dict[str, Any]:
    return {
        "run_id": answer.run_id,
        "timestamp": answer.timestamp,
        "platform_id": answer.platform_id,
        "platform_name": answer.platform_name,
        "question_id": answer.question_id,
        "question": answer.question,
        "answer_text": answer.answer_text,
        "status": answer.status,
        "error_message": answer.error_message,
    }


def _replace_answer_records(existing: list[AnswerRecord], replacements: list[AnswerRecord]) -> list[AnswerRecord]:
    by_key = {(answer.platform_id, answer.question_id): answer for answer in replacements}
    seen: set[tuple[str, str]] = set()
    merged: list[AnswerRecord] = []
    for answer in existing:
        pair = (answer.platform_id, answer.question_id)
        if pair in by_key:
            merged.append(by_key[pair])
            seen.add(pair)
        else:
            merged.append(answer)
    for answer in replacements:
        pair = (answer.platform_id, answer.question_id)
        if pair not in seen:
            merged.append(answer)
            seen.add(pair)
    return merged


def _can_refresh_answer_artifacts(answer: AnswerRecord) -> bool:
    if answer.status != "partial_success" or not answer.answer_text or not answer.answer_url:
        return False
    if answer.error_message:
        return False
    return bool(answer.screenshot_error or answer.citation_error)


def _retry_progress_message(runner_config, retry_pairs: list[tuple[AIPlatform, Question]]) -> str:
    if all(platform.method == "api" for platform, _ in retry_pairs):
        return f"正在并发重试失败请求（API 并发 {max(1, runner_config.api_concurrency)}）"
    return f"正在并发重试失败请求（浏览器平台并发 {max(1, runner_config.browser_concurrency)}）"
