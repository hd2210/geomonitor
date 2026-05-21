from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .models import AIPlatform, AnswerRecord, BrowserAccount, Question, RunnerConfig


HUMAN_VERIFICATION_TEXTS = (
    "verify you are human",
    "checking if the site connection is secure",
    "please verify you are human",
    "cloudflare",
    "请验证您是真人",
    "正在检查您是否是真人",
    "验证码",
    "安全检查",
    "安全验证",
    "unusual traffic",
)

MIN_RESPONSE_SECONDS = 12
STABLE_RESPONSE_ROUNDS = 5


class AIPlatformRunner:
    def __init__(self, runner_config: RunnerConfig) -> None:
        self.config = runner_config

    async def run_question(
        self,
        run_id: str,
        platform: AIPlatform,
        question: Question,
        screenshot_path: Path,
        html_path: Path,
        account: BrowserAccount | None = None,
    ) -> AnswerRecord:
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        base = AnswerRecord(
            run_id=run_id,
            timestamp=timestamp,
            platform_id=platform.platform_id,
            platform_name=platform.platform_name,
            question_id=question.question_id,
            question=question.question,
            account_id=account.account_id if account else None,
            account_name=account.account_name if account else None,
        )

        if platform.browser_mode == "cdp":
            return await self._run_question_with_direct_cdp(platform, question, screenshot_path, html_path, base, account)

        try:
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
            from playwright.async_api import async_playwright
        except ImportError:
            base.status = "failed"
            base.error_message = "Playwright is not installed. Run: pip install -r requirements.txt"
            return base

        profile_dir = Path(self.config.browser_profile_dir) / platform.platform_id
        profile_dir.mkdir(parents=True, exist_ok=True)

        try:
            async with async_playwright() as playwright:
                context, page, should_close_context = await self._open_context_and_page(playwright, platform, profile_dir)
                page.set_default_timeout(self.config.timeout_seconds * 1000)
                try:
                    await page.goto(platform.url, wait_until="domcontentloaded")
                    await _close_blocking_popups(page)

                    try:
                        await self._open_new_conversation(page, platform)
                    except Exception as exc:  # noqa: BLE001
                        base.status = "blocked" if _is_blocked_exception(str(exc)) else "failed"
                        base.error_message = f"Failed to open a new conversation: {exc}"
                        await self._save_html(page, html_path, base)
                        await self._pause_for_blocked_debug(page, base.status, base.error_message)
                        return base

                    await self._submit_question(page, platform, question.question)
                    popup_closed, popup_error = await _close_blocking_popups(page)
                    if not popup_closed:
                        base.status = "blocked"
                        base.error_message = popup_error or "Blocking popup could not be closed after submitting question."
                        await self._save_html(page, html_path, base)
                        await self._pause_for_blocked_debug(page, base.status, base.error_message)
                        return base
                    answer_text = await self._wait_and_extract_answer(page, platform)
                    await self._save_html(page, html_path, base)

                    if not answer_text.strip():
                        base.status = "empty_answer"
                        base.error_message = "Answer container was found but extracted text was empty."
                        return base

                    base.answer_text = answer_text
                    base.answer_url = page.url
                    base.raw_html_path = _relative_run_path(html_path)
                    try:
                        await self._screenshot_answer(page, platform, screenshot_path)
                        base.screenshot_path = _relative_run_path(screenshot_path)
                        base.status = "success"
                    except Exception as exc:  # noqa: BLE001
                        base.status = "partial_success"
                        base.screenshot_error = str(exc)
                    try:
                        base.citations = await self._extract_citations(page, platform)
                    except Exception as exc:  # noqa: BLE001
                        base.citation_error = str(exc)
                        base.status = "partial_success"
                    return base
                except (PlaywrightTimeoutError, TimeoutError) as exc:
                    base.status = "timeout"
                    base.error_message = f"Response timeout after {self.config.timeout_seconds} seconds: {exc}"
                    await self._try_save_html(page, html_path, base)
                    return base
                except Exception as exc:  # noqa: BLE001
                    message = str(exc)
                    base.status = "blocked" if _is_blocked_exception(message) else "failed"
                    base.error_message = message
                    await self._try_save_html(page, html_path, base)
                    await self._pause_for_blocked_debug(page, base.status, base.error_message)
                    return base
                finally:
                    if should_close_context:
                        await context.close()
                    else:
                        await page.close()
        except Exception as exc:  # noqa: BLE001
            base.status = "failed"
            base.error_message = str(exc)
            return base

    async def _run_question_with_direct_cdp(
        self,
        platform: AIPlatform,
        question: Question,
        screenshot_path: Path,
        html_path: Path,
        base: AnswerRecord,
        account: BrowserAccount | None = None,
    ) -> AnswerRecord:
        cdp_url = _normalize_cdp_url((account.cdp_url if account else None) or platform.cdp_url or self.config.browser_cdp_url or "http://127.0.0.1:9222")
        try:
            await self._ensure_cdp_chrome(platform, cdp_url, account)
            result = await asyncio.to_thread(
                _run_direct_cdp_question,
                cdp_url,
                platform.url or "https://www.doubao.com/chat/",
                question.question,
                self.config.timeout_seconds,
                screenshot_path,
                html_path,
                tuple(platform.citation_triggers or _default_citation_triggers(platform.platform_id)),
                platform.platform_id,
            )
            base.answer_text = result["answer_text"]
            base.answer_url = result["answer_url"]
            base.raw_html_path = _relative_run_path(html_path)
            base.screenshot_path = _relative_run_path(screenshot_path) if screenshot_path.exists() else None
            base.citations = result.get("citations", [])
            base.screenshot_error = result.get("screenshot_error")
            base.citation_error = result.get("citation_error")
            if not base.answer_text.strip():
                base.status = "empty_answer"
                base.error_message = "Answer container was found but extracted text was empty."
            elif base.screenshot_error or base.citation_error:
                base.status = "partial_success"
            else:
                base.status = "success"
            return base
        except TimeoutError as exc:
            base.status = "timeout"
            base.error_message = f"Response timeout after {self.config.timeout_seconds} seconds: {exc}"
            return base
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            base.status = "blocked" if _is_blocked_exception(message) else "failed"
            base.error_message = message
            return base

    async def refresh_answer_artifacts(
        self,
        record: AnswerRecord,
        platform: AIPlatform,
        screenshot_path: Path,
        html_path: Path,
    ) -> AnswerRecord:
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        refreshed = AnswerRecord(
            run_id=record.run_id,
            timestamp=timestamp,
            platform_id=record.platform_id,
            platform_name=record.platform_name,
            question_id=record.question_id,
            question=record.question,
            answer_text=record.answer_text,
            answer_url=record.answer_url,
            screenshot_path=record.screenshot_path,
            raw_html_path=record.raw_html_path,
            raw_response_path=record.raw_response_path,
            status=record.status,
            error_message=record.error_message,
            screenshot_error=None,
            citations=list(record.citations or []),
            citation_error=None,
        )
        if not refreshed.answer_url:
            raise RuntimeError("This answer has no saved answer_url.")

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            refreshed.status = "failed"
            refreshed.error_message = "Playwright is not installed. Run: pip install -r requirements.txt"
            return refreshed

        profile_dir = Path(self.config.browser_profile_dir) / platform.platform_id
        profile_dir.mkdir(parents=True, exist_ok=True)

        try:
            async with async_playwright() as playwright:
                context, page, should_close_context = await self._open_context_and_page(playwright, platform, profile_dir)
                page.set_default_timeout(self.config.timeout_seconds * 1000)
                try:
                    await page.goto(refreshed.answer_url, wait_until="domcontentloaded")
                    await page.wait_for_timeout(1500)
                    await _close_blocking_popups(page)
                    await self._save_html(page, html_path, refreshed)
                    refreshed.answer_url = page.url
                    try:
                        await self._screenshot_answer(page, platform, screenshot_path)
                        refreshed.screenshot_path = _relative_run_path(screenshot_path)
                    except Exception as exc:  # noqa: BLE001
                        refreshed.screenshot_error = str(exc)
                    try:
                        refreshed.citations = await self._extract_citations(page, platform)
                    except Exception as exc:  # noqa: BLE001
                        refreshed.citation_error = str(exc)
                    refreshed.status = "success" if not refreshed.screenshot_error and not refreshed.citation_error else "partial_success"
                    refreshed.error_message = None
                    return refreshed
                finally:
                    if should_close_context:
                        await context.close()
                    else:
                        await page.close()
        except Exception as exc:  # noqa: BLE001
            refreshed.status = "partial_success"
            refreshed.error_message = None
            refreshed.citation_error = refreshed.citation_error or str(exc)
            return refreshed

    async def random_delay(self) -> None:
        delay = random.uniform(self.config.min_delay_seconds, self.config.max_delay_seconds)
        if delay > 0:
            await asyncio.sleep(delay)

    def _launch_options(self, profile_dir: Path) -> dict:
        launch_options = {
            "user_data_dir": str(profile_dir),
            "headless": self.config.headless,
            "viewport": {"width": self.config.viewport_width, "height": self.config.viewport_height},
        }
        if self.config.browser_channel:
            launch_options["channel"] = self.config.browser_channel
        if self.config.browser_executable_path:
            launch_options["executable_path"] = self.config.browser_executable_path
        return launch_options

    async def _open_context_and_page(self, playwright, platform: AIPlatform, profile_dir: Path):
        if platform.browser_mode == "cdp":
            cdp_url = _normalize_cdp_url(platform.cdp_url or self.config.browser_cdp_url or "http://127.0.0.1:9222")
            await self._ensure_cdp_chrome(platform, cdp_url)
            browser = await playwright.chromium.connect_over_cdp(cdp_url)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = await context.new_page()
            await page.set_viewport_size({"width": self.config.viewport_width, "height": self.config.viewport_height})
            return context, page, False

        launch_options = self._launch_options(profile_dir)
        context = await self._launch_persistent_context_with_profile_retry(playwright, launch_options)
        page = context.pages[0] if context.pages else await context.new_page()
        return context, page, True

    async def _ensure_cdp_chrome(self, platform: AIPlatform, cdp_url: str, account: BrowserAccount | None = None) -> None:
        if await asyncio.to_thread(_cdp_is_available, cdp_url):
            if await asyncio.to_thread(_cdp_accepts_websocket, cdp_url):
                return
            await asyncio.to_thread(_terminate_cdp_process, cdp_url)
            await asyncio.sleep(1)

        chrome_path = _resolve_chrome_path((account.chrome_path if account else None) or platform.chrome_path or self.config.browser_executable_path)
        user_data_dir = _resolve_cdp_user_data_dir(platform, self.config.browser_profile_dir, account)
        user_data_dir.mkdir(parents=True, exist_ok=True)
        port = _cdp_port(cdp_url)
        if port is None:
            raise RuntimeError(f"Invalid CDP URL for {platform.platform_name}: {cdp_url}")

        args = [
            chrome_path,
            f"--remote-debugging-port={port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ]
        try:
            subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"Failed to start Chrome for {platform.platform_name}{_account_label(account)}. "
                "Set Chrome path in /admin if Chrome is installed in a custom location."
            ) from exc

        deadline = asyncio.get_running_loop().time() + 15
        while asyncio.get_running_loop().time() < deadline:
            if await asyncio.to_thread(_cdp_is_available, cdp_url):
                return
            await asyncio.sleep(0.5)
        raise RuntimeError(f"Chrome CDP endpoint was not available after launch for {platform.platform_name}{_account_label(account)}: {cdp_url}")

    async def _launch_persistent_context_with_profile_retry(self, playwright, launch_options: dict):
        deadline = asyncio.get_running_loop().time() + self.config.profile_lock_wait_seconds
        last_error: Exception | None = None
        while True:
            try:
                return await playwright.chromium.launch_persistent_context(**launch_options)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if not _is_profile_in_use_error(str(exc)) or asyncio.get_running_loop().time() >= deadline:
                    raise
                print(
                    "Browser profile is already in use. Close the previous login/debug Chromium window; "
                    "retrying in 2 seconds...",
                    flush=True,
                )
                await asyncio.sleep(2)
        raise RuntimeError(str(last_error))

    async def prepare_login(self, platform: AIPlatform, account: BrowserAccount | None = None) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError("Playwright is not installed. Run: pip install -r requirements.txt") from exc

        profile_dir = Path(self.config.browser_profile_dir) / platform.platform_id
        profile_dir.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as playwright:
            if platform.browser_mode == "cdp" and account is not None:
                cdp_url = _normalize_cdp_url(account.cdp_url or platform.cdp_url or self.config.browser_cdp_url or "http://127.0.0.1:9222")
                await self._ensure_cdp_chrome(platform, cdp_url, account)
                browser = await playwright.chromium.connect_over_cdp(cdp_url)
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = await context.new_page()
                await page.set_viewport_size({"width": self.config.viewport_width, "height": self.config.viewport_height})
                should_close_context = False
            else:
                context, page, should_close_context = await self._open_context_and_page(playwright, platform, profile_dir)
            try:
                await page.goto(platform.url, wait_until="domcontentloaded")
                print(f"Login browser opened for {platform.platform_name}{_account_label(account)}. Sign in, then close the browser window.")
                if should_close_context:
                    while context.pages:
                        await asyncio.sleep(1)
                else:
                    while not page.is_closed():
                        await asyncio.sleep(1)
            finally:
                if should_close_context:
                    try:
                        await context.close()
                    except Exception:
                        pass

    async def _detect_blockers(self, page, platform: AIPlatform) -> tuple[str, str] | None:
        input_locator = await _first_visible_locator(page, platform.selectors.input, timeout=4000)
        if input_locator is None:
            await _try_open_new_conversation_from_page(page, platform)
            input_locator = await _first_visible_locator(page, platform.selectors.input, timeout=5000)
        if input_locator is None and platform.platform_id == "yuanbao":
            input_locator = await _find_yuanbao_input(page, timeout=6000)
        if input_locator is None:
            return "blocked", f"Input selector was not visible after trying to open a new conversation: {platform.selectors.input}"

        popup_closed, popup_error = await _close_blocking_popups(page)
        if not popup_closed:
            return "blocked", popup_error or "Blocking popup could not be closed."
        return None

    async def _open_new_conversation(self, page, platform: AIPlatform) -> None:
        if platform.platform_id == "yuanbao":
            input_locator = await _find_yuanbao_input(page, timeout=8000)
            if input_locator is None:
                raise RuntimeError(f"Blocked: Yuanbao input was not visible on the default page: {platform.selectors.input}")
            return

        if platform.new_chat_url:
            await page.goto(platform.new_chat_url, wait_until="domcontentloaded")
            await asyncio.sleep(1)
        elif platform.selectors.new_chat:
            locator = await _first_visible_locator(page, platform.selectors.new_chat, 7000)
            if locator is None:
                input_locator = await _first_visible_locator(page, platform.selectors.input, 3000)
                if input_locator is not None and not await self._has_existing_answer(page, platform):
                    return
                opened = await _try_open_new_conversation_from_page(page, platform)
                if not opened:
                    raise RuntimeError(
                        f"Blocked: input was not visible and no new conversation control could be clicked: {platform.selectors.new_chat}"
                    )
            else:
                await locator.click()
                await asyncio.sleep(1)
        else:
            opened = await _try_open_new_conversation_from_page(page, platform)
            if not opened:
                raise RuntimeError("Blocked: no input or new conversation control was available.")

        input_locator = await _first_visible_locator(page, platform.selectors.input, 10000)
        if input_locator is None and platform.platform_id == "yuanbao":
            input_locator = await _find_yuanbao_input(page, timeout=6000)
        if input_locator is None:
            raise RuntimeError(f"Blocked: input was not visible after opening a new conversation: {platform.selectors.input}")

    async def _has_existing_answer(self, page, platform: AIPlatform) -> bool:
        if platform.selectors.answer_item:
            locator = await _first_visible_locator(page, platform.selectors.answer_item, 500)
            if locator is not None:
                return True
        text = await _body_text(page)
        return bool(text and "内容由AI生成" in text and platform.platform_id not in {"kimi", "tongyi"})

    async def _submit_question(self, page, platform: AIPlatform, question: str) -> None:
        selectors = platform.selectors
        input_locator = await _first_visible_locator(page, selectors.input, self.config.timeout_seconds * 1000)
        if input_locator is None and platform.platform_id == "yuanbao":
            input_locator = await _find_yuanbao_input(page, timeout=6000)
        if input_locator is None:
            raise RuntimeError(f"Input selector was not visible: {selectors.input}")
        try:
            await self._fill_question_input(page, input_locator, question)
        except Exception:
            popup_closed, popup_error = await _close_blocking_popups(page)
            if not popup_closed:
                raise RuntimeError(f"Blocked: {popup_error or 'blocking popup could not be closed before submitting'}")
            input_locator = await _first_visible_locator(page, selectors.input, 3000)
            if input_locator is None and platform.platform_id == "yuanbao":
                input_locator = await _find_yuanbao_input(page, timeout=3000)
            if input_locator is None:
                raise RuntimeError(f"Input selector was not visible after closing popup: {selectors.input}")
            await self._fill_question_input(page, input_locator, question)
        await page.wait_for_timeout(700)

        if selectors.submit:
            submit_locator = await _first_visible_locator(page, selectors.submit, 3000)
        else:
            submit_locator = None

        if submit_locator:
            try:
                if await _locator_looks_disabled(submit_locator):
                    await input_locator.press("Enter")
                else:
                    await submit_locator.click()
            except Exception:
                await input_locator.press("Enter")
        else:
            await input_locator.press("Enter")

    async def _fill_question_input(self, page, input_locator, question: str) -> None:
        await input_locator.click()
        try:
            await input_locator.fill(question)
        except Exception:
            await page.keyboard.press("Meta+A")
            await page.keyboard.type(question)

    async def _wait_and_extract_answer(self, page, platform: AIPlatform) -> str:
        selectors = platform.selectors
        deadline = asyncio.get_running_loop().time() + self.config.timeout_seconds
        started_at = asyncio.get_running_loop().time()
        last_text = ""
        stable_rounds = 0

        container_selector = selectors.answer_item or selectors.answer_container
        await _first_attached_locator(page, "body", 5000)
        while asyncio.get_running_loop().time() < deadline:
            popup_closed, popup_error = await _close_blocking_popups(page)
            if not popup_closed:
                raise RuntimeError(f"Blocked: {popup_error or 'blocking popup could not be closed during response'}")

            text = await self._extract_text(page, platform)
            elapsed = asyncio.get_running_loop().time() - started_at
            is_generating = bool(selectors.stop_generating and await _is_visible(page, selectors.stop_generating, timeout=300))
            if text.strip() and text == last_text:
                stable_rounds += 1
            else:
                stable_rounds = 0
                last_text = text

            if is_generating:
                stable_rounds = 0
                await asyncio.sleep(2)
                continue

            if selectors.done_indicator and await _is_visible(page, selectors.done_indicator, timeout=300) and text.strip():
                return text
            if selectors.stop_generating and not is_generating and text.strip() and elapsed >= MIN_RESPONSE_SECONDS:
                stable_rounds += 1
            if stable_rounds >= STABLE_RESPONSE_ROUNDS and text.strip() and elapsed >= MIN_RESPONSE_SECONDS:
                return text
            await asyncio.sleep(2)

        raise TimeoutError(f"Response timeout after {self.config.timeout_seconds} seconds")

    async def _extract_text(self, page, platform: AIPlatform) -> str:
        selectors = platform.selectors
        if selectors.answer_item:
            text = await _best_text(page, selectors.answer_item)
            return text.strip()
        locator = await _last_locator(page, selectors.answer_container)
        if locator is None:
            return ""
        return (await locator.inner_text()).strip()

    async def _screenshot_answer(self, page, platform: AIPlatform, screenshot_path: Path) -> None:
        if platform.platform_id == "yuanbao":
            await _screenshot_yuanbao(page, screenshot_path)
            return
        if platform.platform_id == "tongyi":
            await _screenshot_tongyi(page, screenshot_path)
            return
        await _expand_scrollable_page(page)
        await page.screenshot(path=str(screenshot_path), full_page=True)

    async def _extract_citations(self, page, platform: AIPlatform) -> list[dict[str, str]]:
        triggers = platform.citation_triggers or _default_citation_triggers(platform.platform_id)
        if not triggers:
            return []
        citations, debug_lines = await _click_trigger_and_collect_citations(page, triggers, platform.platform_id)
        _log_citation_debug(platform.platform_id, debug_lines)
        normalized = _filter_platform_citations(_normalize_citations(citations), platform.platform_id, page.url)
        if not normalized and platform.platform_id in {"deepseek", "doubao", "yuanbao", "tongyi", "wenxin"}:
            tail = " | ".join(debug_lines[-8:])
            raise RuntimeError(f"Citation trigger was configured but no citation links were collected. Debug: {tail}")
        return normalized

    async def _pause_for_blocked_debug(self, page, status: str, error_message: str | None) -> None:
        if status not in {"blocked", "login_required"} or self.config.pause_on_blocked_seconds <= 0 or self.config.headless:
            return
        print(
            f"Browser paused for {self.config.pause_on_blocked_seconds}s because {status}: {error_message or ''}. "
            "Inspect the page or close the browser to continue.",
            flush=True,
        )
        deadline = asyncio.get_running_loop().time() + self.config.pause_on_blocked_seconds
        while asyncio.get_running_loop().time() < deadline:
            try:
                if page.is_closed():
                    return
            except Exception:
                return
            await asyncio.sleep(1)

    async def _save_html(self, page, html_path: Path, record: AnswerRecord) -> None:
        record.answer_url = record.answer_url or page.url
        html_path.write_text(await page.content(), encoding="utf-8")
        record.raw_html_path = _relative_run_path(html_path)

    async def _try_save_html(self, page, html_path: Path, record: AnswerRecord) -> None:
        try:
            await self._save_html(page, html_path, record)
        except Exception:
            pass


class _DirectCDPClient:
    def __init__(self, websocket_url: str, timeout: int) -> None:
        try:
            import websocket
        except ImportError as exc:
            raise RuntimeError("websocket-client is required for CDP mode. Run: pip install -r requirements.txt") from exc
        self.websocket = websocket.create_connection(websocket_url, timeout=timeout)
        self.next_id = 0

    def close(self) -> None:
        try:
            self.websocket.close()
        except Exception:
            pass

    def call(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        message_id = self.next_id
        self.websocket.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        while True:
            raw = self.websocket.recv()
            message = json.loads(raw)
            if message.get("id") != message_id:
                continue
            if "error" in message:
                error = message["error"]
                raise RuntimeError(f"CDP {method} failed: {error.get('message') or error}")
            return message.get("result", {})

    def eval(self, expression: str, timeout_seconds: int | None = None):
        if timeout_seconds:
            self.websocket.settimeout(timeout_seconds)
        try:
            result = self.call(
                "Runtime.evaluate",
                {
                    "expression": expression,
                    "awaitPromise": True,
                    "returnByValue": True,
                    "timeout": (timeout_seconds or 30) * 1000,
                },
            )
        finally:
            self.websocket.settimeout(None)
        if result.get("exceptionDetails"):
            text = result["exceptionDetails"].get("text") or "Runtime evaluation failed"
            raise RuntimeError(text)
        remote = result.get("result", {})
        return remote.get("value")


def _run_direct_cdp_question(
    cdp_url: str,
    target_url: str,
    question: str,
    timeout_seconds: int,
    screenshot_path: Path,
    html_path: Path,
    citation_triggers: tuple[str, ...],
    platform_id: str,
) -> dict:
    target = _create_cdp_target(cdp_url, target_url)
    client = _DirectCDPClient(target["webSocketDebuggerUrl"], timeout=max(timeout_seconds, 30))
    try:
        client.call("Page.enable")
        client.call("Runtime.enable")
        client.call("DOM.enable")
        client.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": 1440, "height": 1200, "deviceScaleFactor": 1, "mobile": False},
        )
        client.call("Page.bringToFront")
        if not _same_url_without_fragment(_cdp_current_url(client), target_url):
            client.call("Page.navigate", {"url": target_url})
        _cdp_wait_for_ready(client, timeout_seconds)
        _cdp_close_blocking_popups(client)
        if platform_id == "doubao":
            _cdp_submit_doubao_question(client, question)
        elif platform_id == "wenxin":
            _cdp_submit_wenxin_question(client, question)
        else:
            _cdp_submit_generic_question(client, question)
        answer_text = _cdp_wait_for_answer(client, question, timeout_seconds)
        answer_url = _cdp_current_url(client)
        html = _cdp_html(client)
        html_path.parent.mkdir(parents=True, exist_ok=True)
        html_path.write_text(html, encoding="utf-8")

        screenshot_error = None
        try:
            _cdp_save_full_page_screenshot(client, screenshot_path)
        except Exception as exc:  # noqa: BLE001
            screenshot_error = str(exc)

        citation_error = None
        citations: list[dict[str, str]] = []
        try:
            citations = _cdp_extract_citations(client, cdp_url, target.get("id"), citation_triggers, platform_id, answer_url)
        except Exception as exc:  # noqa: BLE001
            citation_error = str(exc)
        return {
            "answer_text": answer_text,
            "answer_url": answer_url,
            "screenshot_error": screenshot_error,
            "citation_error": citation_error,
            "citations": citations,
        }
    finally:
        client.close()
        _close_cdp_target(cdp_url, target)


def _create_cdp_target(cdp_url: str, target_url: str) -> dict:
    request = Request(f"{cdp_url.rstrip('/')}/json/new?{quote(target_url, safe=':/?&=%')}", method="PUT")
    with urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _close_cdp_target(cdp_url: str, target: dict) -> None:
    target_id = target.get("id")
    if not target_id:
        return
    try:
        with urlopen(f"{cdp_url.rstrip('/')}/json/close/{quote(str(target_id), safe='')}", timeout=5):
            return
    except Exception:
        return


def _list_cdp_targets(cdp_url: str) -> list[dict]:
    try:
        with urlopen(cdp_url.rstrip("/") + "/json/list", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return payload if isinstance(payload, list) else []
    except Exception:
        return []


def _cdp_wait_for_ready(client: _DirectCDPClient, timeout_seconds: int) -> None:
    import time

    end = time.monotonic() + timeout_seconds
    while time.monotonic() < end:
        try:
            state = client.eval("document.readyState")
            if state in {"interactive", "complete"}:
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise TimeoutError("CDP page did not become ready.")


def _cdp_close_blocking_popups(client: _DirectCDPClient) -> None:
    client.eval(
        """
        (() => {
          const words = ['稍后再说','稍后','我知道了','知道了','关闭','取消'];
          const nodes = [...document.querySelectorAll('button,[role=button],a,div,span')];
          for (const node of nodes) {
            const text = (node.innerText || node.textContent || '').trim();
            if (words.some(word => text === word || text.includes(word)) && node.offsetParent !== null) {
              node.click();
            }
          }
          return true;
        })()
        """
    )


def _cdp_submit_doubao_question(client: _DirectCDPClient, question: str) -> None:
    point = _cdp_wait_for_input_point(client, "doubao", timeout_seconds=30)
    if not point:
        raise RuntimeError(f"Input selector was not visible in CDP page. {_cdp_input_diagnostics(client, 'doubao')}")
    import time

    client.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": point["x"], "y": point["y"]})
    client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1})
    client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1})
    client.call("Input.insertText", {"text": question})
    time.sleep(0.5)
    client.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13})
    client.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13})
    time.sleep(1)
    clicked = client.eval(
        """
        (() => {
          const nodes = [...document.querySelectorAll('button,[role=button],a,div')];
          const visible = nodes.filter(el => {
            const rect = el.getBoundingClientRect();
            const text = (el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || '').trim();
            return rect.width > 8 && rect.height > 8 && rect.left >= 0 && rect.top >= 0 &&
              /发送|send/i.test(text + ' ' + el.className + ' ' + el.id);
          });
          const el = visible[visible.length - 1];
          if (!el) return false;
          el.click();
          return true;
        })()
        """
    )
    if clicked:
        time.sleep(1)


def _cdp_submit_generic_question(client: _DirectCDPClient, question: str) -> None:
    _cdp_submit_doubao_question(client, question)


def _cdp_click_point(client: _DirectCDPClient, point: dict) -> None:
    client.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": point["x"], "y": point["y"]})
    client.call(
        "Input.dispatchMouseEvent",
        {"type": "mousePressed", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1},
    )
    client.call(
        "Input.dispatchMouseEvent",
        {"type": "mouseReleased", "x": point["x"], "y": point["y"], "button": "left", "clickCount": 1},
    )


def _cdp_clear_active_input(client: _DirectCDPClient) -> None:
    import time

    client.eval(
        """
        (() => {
          const el = document.activeElement;
          if (!el) return false;
          if ('value' in el && typeof el.select === 'function') {
            el.select();
            return true;
          }
          const selection = window.getSelection();
          if (!selection) return false;
          const range = document.createRange();
          range.selectNodeContents(el);
          selection.removeAllRanges();
          selection.addRange(range);
          return true;
        })()
        """
    )
    client.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8})
    client.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8})
    time.sleep(0.2)


def _cdp_type_text_as_keys(client: _DirectCDPClient, text: str) -> None:
    import time

    for index, char in enumerate(text.replace("\r", " ").replace("\n", " ")):
        client.call("Input.dispatchKeyEvent", {"type": "char", "text": char, "unmodifiedText": char})
        if index % 12 == 0:
            time.sleep(0.02)


def _cdp_wenxin_input_state(client: _DirectCDPClient, question: str) -> dict:
    state = client.eval(
        f"""
        (() => {{
          const question = {json.dumps(question)};
          const selectors = [
            '[data-slate-editor="true"][contenteditable="true"]',
            '[contenteditable="true"][role="textbox"]',
            '[contenteditable="true"]',
            'textarea',
            '[role="textbox"]'
          ];
          const visible = (el) => {{
            const rect = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            return rect.width > 20 && rect.height > 10 &&
              rect.bottom > 0 && rect.right > 0 &&
              style.visibility !== 'hidden' && style.display !== 'none';
          }};
          const textOf = (el) => ('value' in el ? el.value : (el.innerText || el.textContent || '')).trim();
          const candidates = selectors.flatMap((selector) => [...document.querySelectorAll(selector)]).filter(visible);
          const input = candidates[candidates.length - 1];
          const inputText = input ? textOf(input) : '';
          const buttons = [...document.querySelectorAll('button,[role=button],a,div,span')].filter((el) => {{
            const rect = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            const text = (el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || '').trim();
            const name = `${{el.className || ''}} ${{el.id || ''}}`;
            const ariaDisabled = el.getAttribute('aria-disabled') === 'true';
            const disabled = Boolean(el.disabled) || el.hasAttribute('disabled');
            return rect.width > 8 && rect.height > 8 &&
              style.visibility !== 'hidden' && style.display !== 'none' &&
              style.pointerEvents !== 'none' &&
              Number(style.opacity || 1) > 0.2 &&
              !disabled && !ariaDisabled &&
              !/disabled|disable|inactive/.test(name.toLowerCase()) &&
              (/发送|send/i.test(text) || /send/i.test(name));
          }});
          return {{
            ok: Boolean(input && inputText.includes(question)),
            text: inputText,
            occurrenceCount: question ? inputText.split(question).length - 1 : 0,
            hasInput: Boolean(input),
            sendReady: buttons.length > 0
          }};
        }})()
        """,
        timeout_seconds=5,
    )
    return state if isinstance(state, dict) else {"ok": False, "text": "", "hasInput": False, "sendReady": False}


def _cdp_click_wenxin_send_button(client: _DirectCDPClient) -> bool:
    point = client.eval(
        """
        (() => {
          const nodes = [...document.querySelectorAll('button,[role=button],a,div,span')];
          const candidates = nodes.filter((el) => {
            const rect = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            const text = (el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || '').trim();
            const name = `${el.className || ''} ${el.id || ''}`;
            const ariaDisabled = el.getAttribute('aria-disabled') === 'true';
            const disabled = Boolean(el.disabled) || el.hasAttribute('disabled');
            return rect.width > 8 && rect.height > 8 &&
              rect.bottom > 0 && rect.right > 0 &&
              style.visibility !== 'hidden' && style.display !== 'none' &&
              style.pointerEvents !== 'none' &&
              Number(style.opacity || 1) > 0.2 &&
              !disabled && !ariaDisabled &&
              !/disabled|disable|inactive/.test(name.toLowerCase()) &&
              (/发送|send/i.test(text) || /send/i.test(name));
          });
          const el = candidates[candidates.length - 1];
          if (!el) return null;
          el.scrollIntoView({block: 'center', inline: 'center'});
          const rect = el.getBoundingClientRect();
          return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2};
        })()
        """,
        timeout_seconds=5,
    )
    if not point:
        return False
    _cdp_click_point(client, point)
    return True


def _cdp_wenxin_empty_input_warning(client: _DirectCDPClient) -> bool:
    return bool(
        client.eval(
            """
            (() => /你输入的内容为空|输入内容为空|请输入内容|没有输入内容/.test(document.body?.innerText || ''))()
            """,
            timeout_seconds=5,
        )
    )


def _cdp_fill_wenxin_question(client: _DirectCDPClient, point: dict, question: str) -> dict:
    _cdp_click_point(client, point)
    _cdp_clear_active_input(client)
    _cdp_type_text_as_keys(client, question)
    state = _cdp_wenxin_input_state(client, question)
    if int(state.get("occurrenceCount") or 0) > 1:
        _cdp_clear_active_input(client)
        _cdp_type_text_as_keys(client, question)
        state = _cdp_wenxin_input_state(client, question)
    return state


def _cdp_submit_wenxin_question(client: _DirectCDPClient, question: str) -> None:
    import time

    point = _cdp_wait_for_input_point(client, "wenxin", timeout_seconds=30, clear_existing=False)
    if not point:
        diagnostics = _cdp_input_diagnostics(client, "wenxin")
        blocked_marker = _blocked_text_match(diagnostics)
        if blocked_marker:
            raise RuntimeError(f"Blocked: {blocked_marker}")
        raise RuntimeError(f"Wenxin CDP input was not visible. {diagnostics}")

    input_state = _cdp_fill_wenxin_question(client, point, question)
    if not input_state or not input_state.get("ok"):
        text = input_state.get("text") if isinstance(input_state, dict) else ""
        diagnostics = _cdp_input_diagnostics(client, "wenxin")
        blocked_marker = _blocked_text_match(diagnostics)
        if blocked_marker:
            raise RuntimeError(f"Blocked: {blocked_marker}")
        raise RuntimeError(f"Wenxin input did not accept text before submit: current={text[:80]!r}. {diagnostics}")

    time.sleep(0.5)
    if not _cdp_click_wenxin_send_button(client):
        raise RuntimeError("Wenxin send button was not enabled after filling input.")
    time.sleep(1)
    if _cdp_wenxin_empty_input_warning(client):
        point = _cdp_wait_for_input_point(client, "wenxin", timeout_seconds=10, clear_existing=False)
        if not point:
            raise RuntimeError(f"Wenxin submit reported empty input and input disappeared. {_cdp_input_diagnostics(client, 'wenxin')}")
        input_state = _cdp_fill_wenxin_question(client, point, question)
        if not input_state or not input_state.get("ok"):
            text = input_state.get("text") if isinstance(input_state, dict) else ""
            raise RuntimeError(f"Wenxin retry input did not accept text: current={text[:80]!r}. {_cdp_input_diagnostics(client, 'wenxin')}")
        if not _cdp_click_wenxin_send_button(client):
            raise RuntimeError("Wenxin send button was not enabled after retry filling input.")
        time.sleep(1)
        if _cdp_wenxin_empty_input_warning(client):
            raise RuntimeError("Wenxin submit still reported empty input after keyboard retry.")


def _cdp_wait_for_input_point(
    client: _DirectCDPClient,
    platform_id: str,
    timeout_seconds: int,
    *,
    clear_existing: bool = True,
) -> dict | None:
    import time

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        point = client.eval(
            f"""
            (() => {{
              const platformId = {json.dumps(platform_id)};
              const clearExisting = {json.dumps(clear_existing)};
              const selectors = platformId === 'wenxin'
                ? [
                    '[data-slate-editor="true"][contenteditable="true"]',
                    '[contenteditable="true"][role="textbox"]',
                    '[contenteditable="true"]',
                    'textarea',
                    '[role="textbox"]',
                    '[data-placeholder]'
                  ]
                : [
                    'textarea',
                    '[contenteditable="true"]',
                    '.ql-editor',
                    '[data-placeholder]',
                    '[role="textbox"]'
                  ];
              const visible = (el) => {{
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return rect.width > 20 && rect.height > 10 &&
                  rect.bottom > 0 && rect.right > 0 &&
                  style.visibility !== 'hidden' && style.display !== 'none' &&
                  style.pointerEvents !== 'none' &&
                  Number(style.opacity || 1) > 0.05;
              }};
              const candidates = selectors
                .flatMap((selector) => [...document.querySelectorAll(selector)])
                .filter(visible)
                .filter((el) => {{
                  const text = (el.innerText || el.textContent || el.value || '').trim();
                  const placeholder = el.getAttribute('placeholder') || el.getAttribute('data-placeholder') || '';
                  const merged = `${{text}} ${{placeholder}} ${{el.className || ''}} ${{el.id || ''}}`;
                  return !/历史对话|最近对话|新对话|搜索/.test(merged) || /发送|输入|提问|帮我|需要什么帮助|placeholder|editor|textarea/i.test(merged);
                }});
              const el = candidates[candidates.length - 1];
              if (!el) return null;
              el.scrollIntoView({{block: 'center', inline: 'center'}});
              el.focus();
              if (clearExisting) {{
                if ('value' in el) {{
                  el.value = '';
                }} else {{
                  el.textContent = '';
                }}
                try {{
                  el.dispatchEvent(new InputEvent('input', {{bubbles: true, inputType: 'deleteContentBackward', data: null}}));
                }} catch {{
                  el.dispatchEvent(new Event('input', {{bubbles: true}}));
                }}
                el.dispatchEvent(new Event('change', {{bubbles: true}}));
              }}
              const rect = el.getBoundingClientRect();
              return {{
                x: rect.left + Math.min(rect.width - 8, Math.max(8, rect.width / 2)),
                y: rect.top + Math.min(rect.height - 8, Math.max(8, rect.height / 2))
              }};
            }})()
            """,
            timeout_seconds=5,
        )
        if point:
            return point
        time.sleep(1)
    return None


def _cdp_input_diagnostics(client: _DirectCDPClient, platform_id: str) -> str:
    try:
        state = client.eval(
            f"""
            (() => {{
              const platformId = {json.dumps(platform_id)};
              const selectors = [
                '[data-slate-editor="true"][contenteditable="true"]',
                '[contenteditable="true"][role="textbox"]',
                '[contenteditable="true"]',
                'textarea',
                '[role="textbox"]',
                '[contenteditable]',
                '[data-placeholder]',
                '.ql-editor',
                '[class*="input" i]',
                '[class*="editor" i]'
              ];
              const visible = (el) => {{
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return rect.width > 8 && rect.height > 8 &&
                  rect.bottom > 0 && rect.right > 0 &&
                  style.visibility !== 'hidden' && style.display !== 'none';
              }};
              const counts = Object.fromEntries(selectors.map((selector) => [selector, document.querySelectorAll(selector).length]));
              const visibleInputs = selectors
                .flatMap((selector) => [...document.querySelectorAll(selector)].map((el) => ({selector, el})))
                .filter((item) => visible(item.el))
                .slice(0, 8)
                .map((item) => {{
                  const rect = item.el.getBoundingClientRect();
                  return {{
                    selector: item.selector,
                    tag: item.el.tagName.toLowerCase(),
                    role: item.el.getAttribute('role') || '',
                    contenteditable: item.el.getAttribute('contenteditable') || '',
                    placeholder: item.el.getAttribute('placeholder') || item.el.getAttribute('data-placeholder') || '',
                    text: (item.el.innerText || item.el.textContent || item.el.value || '').replace(/\\s+/g, ' ').trim().slice(0, 80),
                    box: `${Math.round(rect.width)}x${Math.round(rect.height)}@${Math.round(rect.left)},${Math.round(rect.top)}`
                  }};
                }});
              return {{
                url: location.href,
                title: document.title,
                counts,
                visibleInputs,
                bodyText: (document.body?.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 500)
              }};
            }})()
            """,
            timeout_seconds=10,
        )
        if not isinstance(state, dict):
            return "diagnostics unavailable."
        visible_inputs = state.get("visibleInputs") if isinstance(state.get("visibleInputs"), list) else []
        return (
            f"url={state.get('url')}; title={state.get('title')}; "
            f"visible_inputs={visible_inputs}; body={str(state.get('bodyText') or '')[:300]}"
        )
    except Exception as exc:  # noqa: BLE001
        return f"diagnostics error: {exc}"


def _cdp_wait_for_answer(client: _DirectCDPClient, question: str, timeout_seconds: int) -> str:
    import time

    start = time.monotonic()
    end = start + timeout_seconds
    last_text = ""
    stable_rounds = 0
    while time.monotonic() < end:
        text = client.eval(
            """
            (() => {
              const selectors = [
                '[data-testid*="message"]',
                '[class*="message"]',
                '[class*="answer"]',
                '[class*="markdown"]',
                'main'
              ];
              const chunks = [];
              for (const selector of selectors) {
                for (const el of document.querySelectorAll(selector)) {
                  const text = (el.innerText || '').trim();
                  if (text && text.length > 20) chunks.push(text);
                }
              }
              const unique = [...new Set(chunks)];
              return unique.length ? unique[unique.length - 1] : (document.body.innerText || '').trim();
            })()
            """,
            timeout_seconds=10,
        ) or ""
        if question in text and len(text) < len(question) + 80:
            text = ""
        generating = bool(
            client.eval(
                """
                (() => /停止|生成中|思考中|正在|开始全网搜索/.test(document.body.innerText || ''))()
                """,
                timeout_seconds=5,
            )
        )
        elapsed = time.monotonic() - start
        if text.strip() and text == last_text:
            stable_rounds += 1
        else:
            stable_rounds = 0
            last_text = text
        if text.strip() and not generating and elapsed >= MIN_RESPONSE_SECONDS and stable_rounds >= STABLE_RESPONSE_ROUNDS:
            return text.strip()
        time.sleep(2)
    if last_text.strip():
        return last_text.strip()
    raise TimeoutError("CDP response timeout.")


def _cdp_current_url(client: _DirectCDPClient) -> str:
    return client.eval("location.href") or ""


def _cdp_html(client: _DirectCDPClient) -> str:
    return client.eval("document.documentElement.outerHTML", timeout_seconds=10) or ""


def _cdp_save_full_page_screenshot(client: _DirectCDPClient, screenshot_path: Path) -> None:
    metrics = client.call("Page.getLayoutMetrics")
    content_size = metrics.get("contentSize", {})
    width = max(1, int(content_size.get("width") or 1440))
    height = max(1, int(content_size.get("height") or 1200))
    result = client.call(
        "Page.captureScreenshot",
        {
            "format": "png",
            "captureBeyondViewport": True,
            "clip": {"x": 0, "y": 0, "width": width, "height": height, "scale": 1},
        },
    )
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
    screenshot_path.write_bytes(base64.b64decode(result["data"]))


def _cdp_extract_citations(
    client: _DirectCDPClient,
    cdp_url: str,
    target_id: str | None,
    citation_triggers: tuple[str, ...],
    platform_id: str,
    page_url: str,
) -> list[dict[str, str]]:
    payload = json.dumps(list(citation_triggers), ensure_ascii=False)
    client.eval(
        f"""
        (() => {{
          const triggers = {payload};
          const nodes = [...document.querySelectorAll('button,[role=button],a,span,div')];
          const candidates = nodes.filter(el => {{
            const text = (el.innerText || el.textContent || '').trim();
            const rect = el.getBoundingClientRect();
            return text && rect.width > 10 && rect.height > 8 && triggers.some(t => text.includes(t));
          }});
          const el = candidates[candidates.length - 1];
          if (el) el.click();
          return Boolean(el);
        }})()
        """,
        timeout_seconds=10,
    )
    import time

    time.sleep(10)
    raw = client.eval(
        """
        (() => {
          const items = [];
          const roots = [
            ...document.querySelectorAll('.w-full'),
            ...document.querySelectorAll('[class*="reference"], [class*="source"], [class*="citation"]'),
            document.body
          ];
          const seen = new Set();
          for (const root of roots) {
            for (const a of root.querySelectorAll('a[href]')) {
              const href = a.href;
              if (!href || seen.has(href)) continue;
              seen.add(href);
              const text = (a.innerText || a.textContent || '').trim();
              const container = a.closest('li,article,[class*="item"],[class*="card"],div') || a;
              const fullText = (container.innerText || text || '').trim();
              items.push({
                title: fullText.split('\\n').filter(Boolean)[0] || text || href,
                site_name: new URL(href).hostname.replace(/^www\\./, ''),
                url: href
              });
            }
          }
          return items;
        })()
        """,
        timeout_seconds=10,
    )
    if not isinstance(raw, list):
        raw = []
    if platform_id == "wenxin":
        raw = [*raw, *_cdp_collect_wenxin_citations_by_clicking_cards(client, cdp_url, target_id, page_url)]
    return _filter_platform_citations(_normalize_citations(raw), platform_id, page_url)


def _cdp_collect_wenxin_citations_by_clicking_cards(
    client: _DirectCDPClient,
    cdp_url: str,
    target_id: str | None,
    original_url: str,
) -> list[dict[str, str]]:
    import time

    candidates = client.eval(
        """
        (() => {
          const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity) !== 0 &&
              rect.width >= 180 && rect.height >= 60 && rect.left >= window.innerWidth * 0.45 &&
              rect.bottom > 0 && rect.top < window.innerHeight;
          };
          const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
          const roots = Array.from(document.querySelectorAll('[class*="SourcesViewer"], [class*="webListContainer"]'))
            .filter((el) => {
              const rect = el.getBoundingClientRect();
              return rect.left >= window.innerWidth * 0.45 && rect.width >= 240;
            });
          const root = roots.sort((a, b) => b.getBoundingClientRect().width * b.getBoundingClientRect().height - a.getBoundingClientRect().width * a.getBoundingClientRect().height)[0] || document.body;
          const scroller = findScroller(root);
          const snapshots = [];
          for (let round = 0; round < 6; round += 1) {
            snapshots.push(...collectCards(root, round));
            if (!scroller) break;
            const beforeTop = scroller.scrollTop;
            scroller.scrollTop = Math.min(scroller.scrollHeight, scroller.scrollTop + Math.max(scroller.clientHeight * 0.85, 300));
            scroller.dispatchEvent(new Event('scroll', {bubbles: true}));
            if (Math.abs(scroller.scrollTop - beforeTop) < 2) break;
          }
          const cards = snapshots.sort((a, b) => a.round - b.round || a.top - b.top || b.area - a.area);
          const selected = [];
          const selectedTexts = new Set();
          for (const item of cards) {
            if (selected.some((other) => other.el.contains(item.el) || item.el.contains(other.el))) continue;
            const key = item.text.slice(0, 120);
            if (selectedTexts.has(key)) continue;
            selectedTexts.add(key);
            selected.push(item);
            if (selected.length >= 40) break;
          }
          return selected.map((item, index) => ({
            index,
            text: item.text,
            x: Math.round(item.left + item.width * 0.5),
            y: Math.round(item.top + item.height * 0.5),
            width: Math.round(item.width),
            height: Math.round(item.height)
          }));

          function collectCards(root, round) {
            return Array.from(root.querySelectorAll('[class^="item__"], [class*=" item__"], [class*="item__"], li, article, section'))
              .filter(visible)
              .map((el) => {
                const rect = el.getBoundingClientRect();
                const text = textOf(el);
                return {el, text, area: rect.width * rect.height, top: rect.top, left: rect.left, width: rect.width, height: rect.height, round};
              })
              .filter((item) => item.text.length >= 20 && /\\d{4}[-/]\\d{1,2}[-/]\\d{1,2}|搜狗|百度|搜狐|网易|新浪|腾讯|凤凰|今日头条|百家号|知乎|什么值得买|太平洋|中关村/.test(item.text));
          }

          function findScroller(root) {
            const nodes = [root, ...Array.from(root.querySelectorAll('*'))]
              .filter((el) => {
                const style = getComputedStyle(el);
                return el.scrollHeight > el.clientHeight + 20 && /(auto|scroll)/.test(`${style.overflowY} ${style.overflow}`);
              })
              .sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
            return nodes[0] || (root.scrollHeight > root.clientHeight + 20 ? root : null);
          }
        })()
        """,
        timeout_seconds=10,
    )
    if not isinstance(candidates, list):
        return []

    items: list[dict[str, str]] = []
    for candidate in candidates[:40]:
        if not isinstance(candidate, dict):
            continue
        text = str(candidate.get("text") or "")
        title, site_name = _parse_wenxin_card_text(text)
        before_ids = {str(item.get("id")) for item in _list_cdp_targets(cdp_url)}
        x = float(candidate.get("x") or 0)
        y = float(candidate.get("y") or 0)
        if x <= 0 or y <= 0:
            continue
        client.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        client.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
        client.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
        url = _cdp_wait_for_wenxin_clicked_url(client, cdp_url, target_id, before_ids, original_url)
        if _is_external_citation_url(url, "wenxin"):
            items.append({"title": title or url, "site_name": site_name or _site_name_from_url(url), "url": url})
        elif title or site_name:
            items.append({"title": title or text[:120], "site_name": site_name, "url": ""})
        current_url = _cdp_current_url(client)
        if current_url != original_url:
            try:
                client.call("Page.navigate", {"url": original_url})
                _cdp_wait_for_ready(client, 20)
                time.sleep(1)
            except Exception:
                pass
        time.sleep(0.5)
    return _dedupe_citation_items(items)


def _cdp_wait_for_wenxin_clicked_url(
    client: _DirectCDPClient,
    cdp_url: str,
    target_id: str | None,
    before_ids: set[str],
    original_url: str,
) -> str:
    import time

    deadline = time.monotonic() + 6
    last_external = ""
    while time.monotonic() < deadline:
        current_url = _cdp_current_url(client)
        if _is_external_citation_url(current_url, "wenxin") and not _same_url_without_fragment(current_url, original_url):
            return current_url
        for target in _list_cdp_targets(cdp_url):
            opened_id = str(target.get("id") or "")
            if not opened_id or opened_id == str(target_id or "") or opened_id in before_ids:
                continue
            url = str(target.get("url") or "")
            if _is_external_citation_url(url, "wenxin"):
                last_external = url
                _close_cdp_target(cdp_url, target)
                return last_external
        time.sleep(0.3)
    return last_external


def _cdp_is_available(cdp_url: str) -> bool:
    try:
        with urlopen(cdp_url.rstrip("/") + "/json/version", timeout=1.5) as response:
            return 200 <= response.status < 300
    except Exception:
        return False


def _cdp_accepts_websocket(cdp_url: str) -> bool:
    try:
        import websocket

        with urlopen(cdp_url.rstrip("/") + "/json/version", timeout=1.5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        websocket_url = payload.get("webSocketDebuggerUrl")
        if not websocket_url:
            return False
        sock = websocket.create_connection(websocket_url, timeout=2)
        sock.close()
        return True
    except Exception:
        return False


def _terminate_cdp_process(cdp_url: str) -> None:
    port = _cdp_port(cdp_url)
    if port is None:
        return
    try:
        if os.name == "nt":
            output = subprocess.check_output(["netstat", "-ano", "-p", "tcp"], text=True, stderr=subprocess.DEVNULL)
            pids: set[str] = set()
            for line in output.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3].upper() == "LISTENING":
                    pids.add(parts[4])
            for pid in pids:
                subprocess.run(["taskkill", "/PID", pid, "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        else:
            output = subprocess.check_output(["lsof", "-ti", f"tcp:{port}"], text=True, stderr=subprocess.DEVNULL)
            for pid in {item.strip() for item in output.splitlines() if item.strip()}:
                subprocess.run(["kill", pid], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    except Exception:
        return


def _normalize_cdp_url(cdp_url: str) -> str:
    value = cdp_url.strip().rstrip("/")
    if not value.startswith(("http://", "https://")):
        value = f"http://{value}"
    return value


def _cdp_port(cdp_url: str) -> int | None:
    parsed = urlparse(cdp_url)
    if parsed.port:
        return parsed.port
    if parsed.scheme == "http":
        return 80
    if parsed.scheme == "https":
        return 443
    return None


def _resolve_cdp_user_data_dir(platform: AIPlatform, browser_profile_dir: str, account: BrowserAccount | None = None) -> Path:
    account_profile = account.chrome_user_data_dir if account else None
    if account_profile:
        profile_dir = Path(account_profile).expanduser()
    elif platform.chrome_user_data_dir:
        profile_dir = Path(platform.chrome_user_data_dir).expanduser()
    else:
        profile_root = Path(browser_profile_dir).expanduser()
        profile_dir = profile_root.parent / "cdp-profiles" / platform.platform_id
        if account is not None:
            profile_dir = profile_dir / account.account_id
    if not profile_dir.is_absolute():
        profile_dir = Path.cwd() / profile_dir
    return profile_dir


def _account_label(account: BrowserAccount | None) -> str:
    if account is None:
        return ""
    label = account.account_name or account.account_id
    return f"/{label}"


def _resolve_chrome_path(configured_path: str | None) -> str:
    if configured_path:
        return str(Path(configured_path).expanduser())

    candidates: list[str | None] = []
    if os.name == "nt":
        candidates.extend(
            [
                os.path.join(os.environ.get("PROGRAMFILES", ""), "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
            ]
        )
    elif sys.platform == "darwin":
        candidates.append("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    else:
        candidates.extend(
            [
                shutil.which("google-chrome"),
                shutil.which("google-chrome-stable"),
                shutil.which("chromium"),
                shutil.which("chromium-browser"),
            ]
        )

    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise RuntimeError("Google Chrome was not found. Set Chrome path in /admin for CDP browser mode.")


async def _is_visible(page, selector: str, timeout: int) -> bool:
    return await _first_visible_locator(page, selector, timeout) is not None


async def _first_visible_locator(page, selector: str, timeout: int):
    deadline = asyncio.get_running_loop().time() + timeout / 1000
    candidates = [part.strip() for part in selector.split("||") if part.strip()]
    while asyncio.get_running_loop().time() < deadline:
        for candidate_selector in candidates:
            try:
                locators = page.locator(candidate_selector)
                count = await locators.count()
                for index in range(count):
                    candidate = locators.nth(index)
                    try:
                        if await candidate.is_visible(timeout=250):
                            return candidate
                    except Exception:
                        continue
            except Exception:
                continue
        await asyncio.sleep(0.25)
    return None


async def _first_attached_locator(page, selector: str, timeout: int):
    deadline = asyncio.get_running_loop().time() + timeout / 1000
    candidates = [part.strip() for part in selector.split("||") if part.strip()]
    while asyncio.get_running_loop().time() < deadline:
        for candidate_selector in candidates:
            try:
                locators = page.locator(candidate_selector)
                count = await locators.count()
                if count:
                    return locators.nth(count - 1)
            except Exception:
                continue
        await asyncio.sleep(0.25)
    return None


async def _try_open_new_conversation_from_page(page, platform: AIPlatform) -> bool:
    if platform.platform_id == "yuanbao":
        return await _find_yuanbao_input(page, timeout=2000) is not None

    selectors = [
        platform.selectors.new_chat,
        "button[aria-label*='New' i]",
        "button[aria-label*='新建']",
        "button[aria-label*='创建']",
        "button[title*='New' i]",
        "button[title*='新建']",
        "button[title*='创建']",
        "a[href*='new_chat']",
        "a[href*='new-chat']",
        "a[href*='new']",
        "text=New chat",
        "text=Start new chat",
        "text=新建对话",
        "text=新建会话",
        "text=新对话",
        "text=创建对话",
        "text=创建会话",
        "text=开启新对话",
        "text=发起新对话",
        "text=开始对话",
    ]
    selector = " || ".join(item for item in selectors if item)
    for _ in range(3):
        locator = await _first_visible_locator(page, selector, timeout=1200)
        if locator is None:
            return False
        try:
            await locator.click(timeout=1500)
            await page.wait_for_timeout(1000)
            input_locator = await _first_visible_locator(page, platform.selectors.input, timeout=1500)
            if input_locator is not None:
                return True
        except Exception:
            continue
    return await _first_visible_locator(page, platform.selectors.input, timeout=1000) is not None


async def _find_yuanbao_input(page, timeout: int):
    deadline = asyncio.get_running_loop().time() + timeout / 1000
    selectors = (
        "#search-bar [contenteditable='true'] || "
        "[class*='input'] [contenteditable='true'] || "
        "[class*='editor'] [contenteditable='true'] || "
        "[data-placeholder] || "
        "[contenteditable='true'] || textarea"
    )
    while asyncio.get_running_loop().time() < deadline:
        locator = await _first_visible_locator(page, selectors, timeout=800)
        if locator is not None:
            return locator
        try:
            await page.goto("https://yuanbao.tencent.com/", wait_until="domcontentloaded")
        except Exception:
            pass
        await page.wait_for_timeout(1200)
    return None


async def _close_blocking_popups(page) -> tuple[bool, str | None]:
    try:
        if not await _has_blocking_popup(page):
            return True, None

        close_selectors = (
            "button[aria-label*='close' i] || button[aria-label*='关闭'] || "
            "[aria-label*='close' i] || [aria-label*='关闭'] || "
            "button[title*='close' i] || button[title*='关闭'] || "
            "[class*='close' i] || [class*='modal-close' i] || "
            "text=关闭 || text=× || text=稍后再说 || text=稍后 || text=我知道了 || text=知道了 || text=取消"
        )
        for _ in range(4):
            close_locator = await _first_visible_locator(page, close_selectors, timeout=700)
            if close_locator is None:
                break
            try:
                await close_locator.click(timeout=1000)
            except Exception:
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass
            await page.wait_for_timeout(500)
            if not await _has_blocking_popup(page):
                return True, None

        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(500)
        except Exception:
            pass
        if await _has_blocking_popup(page):
            return False, "Blocking popup was visible and could not be closed."
        return True, None
    except Exception as exc:  # noqa: BLE001
        return False, f"Failed while checking blocking popup: {exc}"


async def _has_blocking_popup(page) -> bool:
    try:
        return bool(
            await page.evaluate(
                """
                () => {
                  const markers = [
                    'verify you are human',
                    'checking if the site connection is secure',
                    'please verify you are human',
                    'cloudflare',
                    '请验证您是真人',
                    '正在检查您是否是真人',
                    '验证码',
                    '安全检查',
                    '安全验证',
                    'unusual traffic'
                  ];
                  const bodyText = (document.body?.innerText || '').toLowerCase();
                  if (!markers.some((marker) => bodyText.includes(marker.toLowerCase()))) {
                    return false;
                  }

                  const isVisible = (el) => {
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) {
                      return false;
                    }
                    const rect = el.getBoundingClientRect();
                    return rect.width >= 160 && rect.height >= 100 && rect.bottom > 0 && rect.right > 0 &&
                      rect.left < viewportWidth && rect.top < viewportHeight;
                  };

                  const viewportWidth = window.innerWidth || document.documentElement.clientWidth;
                  const viewportHeight = window.innerHeight || document.documentElement.clientHeight;

                  const verificationLayer = (el, style, rect) => {
                    const role = (el.getAttribute('role') || '').toLowerCase();
                    const ariaModal = (el.getAttribute('aria-modal') || '').toLowerCase();
                    const className = String(el.className || '').toLowerCase();
                    const id = String(el.id || '').toLowerCase();
                    const text = (el.innerText || el.textContent || '').toLowerCase();
                    const name = `${className} ${id}`;
                    const zIndex = Number.parseInt(style.zIndex || '0', 10) || 0;
                    const fixedLayer = ['fixed', 'sticky'].includes(style.position) && zIndex >= 10;
                    const namedLayer = /(^|[-_\\s])(modal|popup|mask|overlay|captcha|verify|verification|cloudflare)([-_\\s]|$)/.test(name);
                    const modalRole = role === 'dialog' || role === 'alertdialog' || ariaModal === 'true';
                    const containsMarker = markers.some((marker) => text.includes(marker.toLowerCase()));
                    const largeEnough = rect.width >= Math.min(320, viewportWidth * 0.3) &&
                      rect.height >= Math.min(120, viewportHeight * 0.18);
                    return containsMarker && (modalRole || namedLayer || fixedLayer || largeEnough);
                  };

                  return Array.from(document.querySelectorAll('body *')).some((el) => {
                    if (!isVisible(el)) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return verificationLayer(el, style, rect);
                  });
                }
                """
            )
        )
    except Exception:
        return False


async def _body_text(page) -> str:
    try:
        return await page.locator("body").inner_text(timeout=1000)
    except Exception:
        return ""


async def _has_blocked_page_shape(page) -> bool:
    try:
        body = await _body_text(page)
        normalized = body.casefold()
        decisive_markers = (
            "verify you are human",
            "checking if the site connection is secure",
            "请验证您是真人",
            "正在检查您是否是真人",
            "验证码",
            "安全检查",
            "unusual traffic",
            "cloudflare",
        )
        return any(marker.casefold() in normalized for marker in decisive_markers)
    except Exception:
        return False


async def _locator_looks_disabled(locator) -> bool:
    try:
        disabled = await locator.get_attribute("disabled", timeout=500)
        aria_disabled = await locator.get_attribute("aria-disabled", timeout=500)
        class_name = await locator.get_attribute("class", timeout=500)
        return disabled is not None or aria_disabled == "true" or "disabled" in (class_name or "").casefold()
    except Exception:
        return False


async def _last_locator(page, selector: str):
    for candidate_selector in [part.strip() for part in selector.split("||") if part.strip()]:
        try:
            locators = page.locator(candidate_selector)
            count = await locators.count()
            if count:
                return locators.nth(count - 1)
        except Exception:
            continue
    return None


async def _best_text(page, selector: str) -> str:
    best = ""
    for candidate_selector in [part.strip() for part in selector.split("||") if part.strip()]:
        try:
            locators = page.locator(candidate_selector)
            count = await locators.count()
            for index in range(count):
                locator = locators.nth(index)
                try:
                    text = (await locator.inner_text(timeout=1000)).strip()
                except Exception:
                    continue
                if _answer_text_score(text) > _answer_text_score(best):
                    best = text
        except Exception:
            continue
    return best


def _answer_text_score(text: str) -> int:
    stripped = " ".join(text.split())
    if not stripped:
        return 0
    penalty_markers = (
        "搜索 元宝",
        "全部应用",
        "全部收藏",
        "安装电脑版",
        "下载元宝",
        "内容由AI生成",
    )
    penalty = sum(300 for marker in penalty_markers if marker in stripped)
    return max(len(stripped) - penalty, 0)


async def _expand_scrollable_page(page) -> None:
    try:
        await page.evaluate(
            """
            () => {
              const changed = [];
              const remember = (el, prop) => {
                changed.push([el, prop, el.style[prop] || ""]);
              };
              const set = (el, prop, value) => {
                remember(el, prop);
                el.style[prop] = value;
              };
              const all = Array.from(document.querySelectorAll("body *"));
              const scrollables = all
                .filter((el) => el.scrollHeight > el.clientHeight + 120 && el.clientWidth > 320)
                .sort((a, b) => (b.scrollHeight * b.clientWidth) - (a.scrollHeight * a.clientWidth))
                .slice(0, 8);

              set(document.documentElement, "height", "auto");
              set(document.documentElement, "overflow", "visible");
              set(document.body, "height", "auto");
              set(document.body, "overflow", "visible");

              for (const el of scrollables) {
                el.scrollTop = el.scrollHeight;
                set(el, "height", `${Math.min(el.scrollHeight, 30000)}px`);
                set(el, "maxHeight", "none");
                set(el, "overflow", "visible");
                set(el, "position", "relative");
                let parent = el.parentElement;
                let depth = 0;
                while (parent && parent !== document.body && depth < 8) {
                  if (parent.clientHeight < el.scrollHeight) {
                    set(parent, "height", "auto");
                    set(parent, "maxHeight", "none");
                    set(parent, "overflow", "visible");
                  }
                  parent = parent.parentElement;
                  depth += 1;
                }
              }
              window.scrollTo(0, document.body.scrollHeight);
            }
            """
        )
        await page.wait_for_timeout(500)
    except Exception:
        pass


async def _expand_platform_chat_scrollables(page, platform_id: str) -> None:
    try:
        await page.evaluate(
            """
            ({platformId}) => {
              const selectorMap = {
                yuanbao: [
                  '.agent-chat__list',
                  '[class*="agent-chat__list"]',
                  '.agent-dialogue__content',
                  '[class*="agent-dialogue__content"]',
                  '[class*="agent-chat"]',
                  'main'
                ],
                tongyi: [
                  '.markdown',
                  '[class*="markdown"]',
                  '[class*="response"]',
                  '[class*="conversation"]',
                  '[class*="chat"]',
                  'main'
                ]
              };
              const selectors = selectorMap[platformId] || ['main'];
              const set = (el, prop, value) => {
                try { el.style[prop] = value; } catch {}
              };
              const visible = (el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden' &&
                  rect.width > 200 && rect.height > 80;
              };
              set(document.documentElement, 'height', 'auto');
              set(document.documentElement, 'maxHeight', 'none');
              set(document.documentElement, 'overflow', 'visible');
              set(document.body, 'height', 'auto');
              set(document.body, 'maxHeight', 'none');
              set(document.body, 'overflow', 'visible');

              const roots = selectors.flatMap((selector) => Array.from(document.querySelectorAll(selector)))
                .filter(visible);
              const scrollables = Array.from(new Set([
                ...roots,
                ...Array.from(document.querySelectorAll('body *'))
                  .filter((el) => visible(el) && el.scrollHeight > el.clientHeight + 80 && el.clientWidth > 260)
              ]))
                .sort((a, b) => (b.scrollHeight * b.clientWidth) - (a.scrollHeight * a.clientWidth))
                .slice(0, 12);

              for (const el of scrollables) {
                const fullHeight = Math.min(Math.max(el.scrollHeight, el.clientHeight), 50000);
                el.scrollTop = 0;
                set(el, 'height', `${fullHeight}px`);
                set(el, 'maxHeight', 'none');
                set(el, 'overflow', 'visible');
                set(el, 'position', 'relative');
                let parent = el.parentElement;
                let depth = 0;
                while (parent && parent !== document.body && depth < 10) {
                  set(parent, 'height', 'auto');
                  set(parent, 'maxHeight', 'none');
                  set(parent, 'overflow', 'visible');
                  parent.scrollTop = 0;
                  parent = parent.parentElement;
                  depth += 1;
                }
              }
              window.scrollTo(0, 0);
            }
            """,
            {"platformId": platform_id},
        )
        await page.wait_for_timeout(700)
    except Exception:
        pass


async def _screenshot_tongyi(page, screenshot_path: Path) -> None:
    await _expand_platform_chat_scrollables(page, "tongyi")
    await page.screenshot(path=str(screenshot_path), full_page=True)


async def _screenshot_yuanbao(page, screenshot_path: Path) -> None:
    try:
        await _expand_platform_chat_scrollables(page, "yuanbao")
        await page.screenshot(path=str(screenshot_path), full_page=True)
        return
    except Exception:
        pass
    await page.screenshot(path=str(screenshot_path), full_page=True)


def _default_citation_triggers(platform_id: str) -> tuple[str, ...]:
    defaults = {
        "chatgpt": ("source", "sources", "来源"),
        "gemini": ("Sources", "引用", "参考", "链接"),
        "deepseek": ("个网页", "来源", "source"),
        "doubao": ("参考", "篇资料"),
        "yuanbao": ("找到了", "篇相关资料", "相关资料"),
        "tongyi": ("篇来源", "来源"),
        "kimi": ("引用",),
        "wenxin": ("参考", "个网页"),
    }
    return defaults.get(platform_id, ("Sources", "引用", "参考", "来源", "链接"))


async def _click_trigger_and_collect_citations(page, triggers: tuple[str, ...], platform_id: str) -> tuple[list[dict[str, str]], list[str]]:
    pre_clicked, playwright_debug = await _playwright_click_citation_candidate(page, triggers, platform_id)
    mouse_debug = [] if pre_clicked else await _mouse_click_citation_candidate(page, triggers, platform_id)
    payload = await page.evaluate(
        """
        async ({triggers, platformId}) => {
          const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
          const debug = [];
          const log = (message) => debug.push(String(message));
          const normalizedTriggers = triggers.map((item) => String(item || '').trim().toLowerCase()).filter(Boolean);
          log(`start triggers=${normalizedTriggers.join('|') || '(none)'}`);
          const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity) !== 0 && rect.width > 8 && rect.height > 8;
          };
          const textOf = (el) => [
            el.innerText || el.textContent || '',
            el.getAttribute('aria-label') || '',
            el.getAttribute('title') || ''
          ].join(' ').replace(/\\s+/g, ' ').trim();
          const matches = (el) => {
            const text = textOf(el).toLowerCase();
            return normalizedTriggers.some((trigger) => text.includes(trigger));
          };
          const clickRoot = (el) => el.closest('button, a, [role="button"], [tabindex], [onclick], [class*="btn" i], [class*="button" i]') || el;
          const clickableSelector = [
            'button',
            'a',
            '[role="button"]',
            '[tabindex]',
            '[onclick]',
            '[id="search-guide-tool"]',
            '[class*="message-action-button-third"]',
            '[class*="entry-btn"]',
            '[class*="source" i]',
            '[class*="citation" i]',
            '[class*="reference" i]',
            '[class*="ref" i]',
            '[class*="web" i]'
          ].join(',');
          const answerScopes = findAnswerScopes();
          const answerScope = answerScopes[answerScopes.length - 1] || document.body;
          const before = currentExternalLinks(document);
          const scopedCandidates = findCitationTriggers(answerScope);
          const pageCandidates = findCitationTriggers(document.body);
          log(`answerScopes=${answerScopes.length} beforeExternalLinks=${before.size}`);
          log(`candidates scoped=${scopedCandidates.length} page=${pageCandidates.length}`);
          const initialLayer = findCitationLayer();
          const initialTongyiRefCount = platformId === 'tongyi' ? countTongyiRefNodes(document) : 0;
          if (initialLayer) {
            log(`initialCitationLayer=${describeElement(initialLayer)}`);
          } else if (platformId === 'tongyi' && initialTongyiRefCount > 0) {
            log(`skipReclickBecauseRefUrlExists=${initialTongyiRefCount}`);
          } else {
            const verified = await clickVerifiedTrigger([...scopedCandidates, ...pageCandidates], before);
            log(`verified=${verified ? textOf(verified).slice(0, 80) : '(none)'}`);
          }
          const citationLayer = findCitationLayer();
          log(`citationLayer=${citationLayer ? describeElement(citationLayer) : '(none)'}`);
          const linkRoot = citationLayer || (platformId === 'tongyi' ? document : answerScope);
          if (!linkRoot) {
            log('linkRoot=(none)');
            return {items: [], debug};
          }
          const scroller = findCitationScroller(linkRoot);
          const collected = [];
          for (let round = 0; round < 6; round += 1) {
            const roundItems = collectItemsFromRoot(linkRoot, citationLayer, before);
            collected.push(...roundItems);
            log(`collectRound=${round} items=${roundItems.length} total=${dedupeItems(collected).length} scroll=${scroller ? `${Math.round(scroller.scrollTop)}/${Math.round(scroller.scrollHeight)}` : '(none)'}`);
            if (!scroller) break;
            const beforeTop = scroller.scrollTop;
            scroller.scrollTop = Math.min(scroller.scrollHeight, scroller.scrollTop + Math.max(scroller.clientHeight * 0.85, 320));
            scroller.dispatchEvent(new Event('scroll', {bubbles: true}));
            await sleep(800);
            if (Math.abs(scroller.scrollTop - beforeTop) < 2) break;
          }
          const items = dedupeItems(collected);
          return {items, debug};

          function collectItemsFromRoot(root, layer, beforeLinks) {
            if (platformId === 'tongyi') {
              const tongyiItems = extractTongyiCitationCards(root);
              if (tongyiItems[0]) log(`firstTongyiRef=${tongyiItems[0].url}`);
              log(`tongyiRefItems=${tongyiItems.length}`);
              return tongyiItems;
            }
            const linkElements = Array.from(root.querySelectorAll('a[href]'))
              .filter((a) => isExternalHref(a.href) && (visible(a) || visibleCitationCard(a)));
            const preferred = linkElements.filter((a) => layer || !beforeLinks.has(a.href) || hasCitationAncestor(a));
            const source = preferred.length ? preferred : linkElements.filter(hasCitationAncestor);
            const anchorItems = source.slice(0, 80).map((a) => {
              const text = textOf(a);
              const parentText = textOf(a.closest('[role="dialog"], [class*="modal" i], [class*="popup" i], [class*="drawer" i], li, article, section, div') || a);
              const site = inferSiteName(a, parentText);
              return {title: text || parentText || a.href, site_name: site, url: a.href};
            });
            const cardItems = platformId === 'doubao' ? extractDoubaoCitationCards(root) : (platformId === 'yuanbao' ? extractYuanbaoCitationCards(root) : []);
            if (anchorItems[0]) log(`firstAnchor=${anchorItems[0].url}`);
            if (cardItems[0]) log(`firstCard=${cardItems[0].url}`);
            return [...anchorItems, ...cardItems];
          }

          function findAnswerScopes() {
            const selectors = [
              '[data-message-author-role="assistant"]',
              '.agent-chat__conv--ai',
              '[class*="conv--ai"]',
              '#answer_text_id',
              '.ds-markdown',
              '.markdown',
              '[class*="markdown"]',
              'message-content',
              '.model-response-text',
              '[data-response-index]',
              '.md-stream',
              '[class*="answer"]',
              '[class*="response"]',
              '[class*="message-content"]'
            ];
            const seen = new Set();
            return selectors.flatMap((selector) => Array.from(document.querySelectorAll(selector)))
              .filter((el) => {
                if (seen.has(el) || !visible(el)) return false;
                seen.add(el);
                return textOf(el).length >= 20;
              })
              .sort((a, b) => {
                const ar = a.getBoundingClientRect();
                const br = b.getBoundingClientRect();
                return (ar.top - br.top) || (ar.left - br.left);
              });
          }

          async function clickVerifiedTrigger(items, beforeLinks) {
            const unique = [];
            const seen = new Set();
            for (const item of items) {
              if (!item?.el || seen.has(item.el)) continue;
              if (item.text && item.text.length > 20 && !item.force) continue;
              seen.add(item.el);
              unique.push(item);
            }
            for (const item of unique.slice(0, 10)) {
              try {
                log(`tryCandidate text=${String(item.text || '').slice(0, 80)} score=${Math.round(item.score || 0)}`);
                item.el.scrollIntoView({block: 'center', inline: 'nearest'});
                await sleep(200);
                await humanClick(item.el);
                await sleep(1200);
                if (!findCitationLayer()) {
                  await keyboardActivate(item.el);
                }
                await sleep(10000);
                const layer = findCitationLayer();
                const root = layer || (platformId === 'doubao' ? null : document);
                if (!root) {
                  log('afterClick root=(none)');
                  continue;
                }
                const afterLinks = currentExternalLinks(root);
                const newLinks = [...afterLinks].filter((href) => !beforeLinks.has(href));
                log(`afterClick layer=${layer ? describeElement(layer) : '(none)'} externalLinks=${afterLinks.size} newExternalLinks=${newLinks.length}`);
                if (newLinks[0]) log(`newExternalLink=${newLinks[0]}`);
                if (layer || newLinks.length) return item.el;
              } catch (error) {
                log(`candidateError=${error?.message || error}`);
              }
            }
            return null;
          }

          async function humanClick(el) {
            const rect = el.getBoundingClientRect();
            const x = Math.max(1, Math.min(window.innerWidth - 2, rect.left + rect.width / 2));
            const y = Math.max(1, Math.min(window.innerHeight - 2, rect.top + rect.height / 2));
            const target = document.elementFromPoint(x, y) || el;
            log(`clickPoint=${Math.round(x)},${Math.round(y)} target=${describeElement(target)}`);
            for (const type of ['pointerover', 'mouseover', 'pointermove', 'mousemove', 'pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
              const eventInit = {bubbles: true, cancelable: true, clientX: x, clientY: y, view: window};
              const event = type.startsWith('pointer')
                ? new PointerEvent(type, {...eventInit, pointerId: 1, pointerType: 'mouse', isPrimary: true})
                : new MouseEvent(type, eventInit);
              target.dispatchEvent(event);
            }
          }

          async function keyboardActivate(el) {
            try {
              el.focus({preventScroll: true});
              log('keyboardActivate=Enter/Space');
              for (const key of ['Enter', ' ']) {
                el.dispatchEvent(new KeyboardEvent('keydown', {key, bubbles: true, cancelable: true}));
                el.dispatchEvent(new KeyboardEvent('keyup', {key, bubbles: true, cancelable: true}));
                await sleep(300);
                if (findCitationLayer()) return;
              }
            } catch (error) {
              log(`keyboardActivateError=${error?.message || error}`);
            }
          }

          function findCitationTriggers(root) {
            const scoped = Array.from(root.querySelectorAll(clickableSelector)).filter(visible);
            const tinyTextMatches = findTinyTextMatches(root);
            const platformSpecific = [...tinyTextMatches, ...scoped.map((el) => ({el: clickRoot(el), text: textOf(el)}))]
              .filter((item) => platformTriggerMatch(item.el, item.text));
            const generic = scoped
              .map((el) => ({el: clickRoot(el), text: textOf(el)}))
              .filter((item) => matches(item.el) || normalizedTriggers.some((trigger) => item.text.toLowerCase().includes(trigger)));
            return sortTriggers([...platformSpecific, ...generic]);
          }

          function findTinyTextMatches(root) {
            return Array.from(root.querySelectorAll('button, a, span, div, p, li, [role="button"], [tabindex], [onclick]'))
              .filter(visible)
              .map((el) => ({el: clickRoot(el), text: textOf(el), raw: el}))
              .filter((item) => {
                const text = item.text || '';
                if (!text || text.length > 80) return false;
                return platformTriggerMatch(item.el, text) || normalizedTriggers.some((trigger) => text.toLowerCase().includes(trigger));
              })
              .map((item) => ({...item, force: true}));
          }

          function platformTriggerMatch(el, text) {
            const normalized = text.toLowerCase();
            if (platformId === 'yuanbao' && (el.id === 'search-guide-tool' || el.querySelector?.('#search-guide-tool'))) return true;
            if (platformId === 'deepseek' && /\\d+\\s*个\\s*网页/.test(text)) return true;
            if (platformId === 'doubao' && /参考\\s*\\d+\\s*篇\\s*资料/.test(text)) return true;
            if (platformId === 'tongyi' && /\\d+\\s*篇\\s*来源/.test(text)) return true;
            if (platformId === 'wenxin' && /参考\\s*\\d+\\s*个\\s*网页/.test(text)) return true;
            return normalizedTriggers.some((trigger) => normalized.includes(trigger));
          }

          function sortTriggers(items) {
            return items
              .map((item) => {
                const rect = item.el.getBoundingClientRect();
                const digitScore = /\\d/.test(item.text) ? 100 : 0;
                const lastAnswerScore = answerScope.contains(item.el) ? 200 : 0;
                const shortTextScore = Math.max(0, 120 - String(item.text || '').length);
                const smallAreaScore = Math.max(0, 180 - Math.min((rect.width * rect.height) / 1000, 180));
                const doubaoScore = platformId === 'doubao' && /参考\\s*\\d+\\s*篇\\s*资料/.test(item.text) ? 700 : 0;
                const forceScore = item.force ? 300 : 0;
                return {...item, score: doubaoScore + forceScore + lastAnswerScore + digitScore + shortTextScore + smallAreaScore + rect.top / Math.max(1, window.innerHeight)};
              })
              .sort((a, b) => b.score - a.score);
          }

          function findCitationLayer() {
            if (platformId === 'tongyi') {
              const tongyiLayer = findTongyiCitationLayer();
              if (tongyiLayer) return tongyiLayer;
            }
            if (platformId === 'yuanbao') {
              const yuanbaoLayer = document.querySelector('#chatReferenceList');
              if (yuanbaoLayer && visible(yuanbaoLayer)) return yuanbaoLayer;
            }
            if (platformId === 'tongyi') {
              const tongyiLayer = findRightCitationLayer(/参考来源|来源/, false);
              if (tongyiLayer) return tongyiLayer;
            }
            if (platformId === 'deepseek') {
              const deepseekLayer = findRightCitationLayer(/搜索结果|网页|来源|参考/);
              if (deepseekLayer) return deepseekLayer;
            }
            if (platformId === 'doubao') {
              const doubaoLayer = findDoubaoCitationLayer();
              if (doubaoLayer) return doubaoLayer;
            }
            const selectors = [
              '[role="dialog"]',
              '[class*="modal" i]',
              '[class*="popup" i]',
              '[class*="drawer" i]',
              '[class*="source" i]',
              '[class*="citation" i]',
              '[class*="reference" i]',
              '[class*="ref" i]',
              '[class*="web" i]'
            ];
            const layers = selectors.flatMap((selector) => Array.from(document.querySelectorAll(selector)))
              .filter((el) => visible(el) && el.querySelector('a[href]') && (matches(el) || textOf(el).length > 20))
              .sort((a, b) => {
                const ar = a.getBoundingClientRect();
                const br = b.getBoundingClientRect();
                return (br.width * br.height) - (ar.width * ar.height);
              });
            return layers[0] || null;
          }

          function findTongyiCitationLayer() {
            const refNodes = Array.from(document.querySelectorAll([
              '[data-click-extra*="ref_url"]',
              '[data-exposure-extra*="ref_url"]',
              '[data-log-params*="ref_url"]',
              '[data-log*="ref_url"]',
              '[data-extra*="ref_url"]',
              '[data-c="refer_panel"]',
              '[class*="source-item"]'
            ].join(','))).filter((el) => el && el.nodeType === 1);
            log(`tongyiRefNodeCount=${refNodes.length}`);
            if (refNodes.length) {
              const candidates = [];
              for (const node of refNodes.slice(0, 20)) {
                for (const selector of [
                  '[data-panel]',
                  '[data-panel-group-id]',
                  '[class*="deep-think-source"]',
                  '[class*="source"]',
                  '[class*="refer"]',
                  'aside',
                  'section',
                  'article',
                  'div'
                ]) {
                  const candidate = node.closest(selector);
                  if (candidate && candidate.nodeType === 1 && visible(candidate) && textOf(candidate).length > 20) {
                    candidates.push(candidate);
                  }
                }
              }
              const best = Array.from(new Set(candidates))
                .map((el) => {
                  const rect = el.getBoundingClientRect();
                  const text = textOf(el);
                  const refCount = countTongyiRefNodes(el);
                  const classScore = /deep-think-source|source|refer/i.test(String(el.className || '')) ? 500 : 0;
                  const panelScore = el.hasAttribute('data-panel') || el.hasAttribute('data-panel-group-id') ? 300 : 0;
                  const areaScore = Math.min(rect.width * rect.height / 1000, 500);
                  return {el, score: refCount * 800 + classScore + panelScore + areaScore + Math.min(text.length / 20, 200)};
                })
                .sort((a, b) => b.score - a.score)[0]?.el;
              if (best) {
                log(`tongyiPanelByRefUrl=${describeElement(best)} refs=${countTongyiRefNodes(best)}`);
                return best;
              }
            }
            return null;
          }

          function countTongyiRefNodes(root) {
            return Array.from(root.querySelectorAll?.([
              '[data-click-extra*="ref_url"]',
              '[data-exposure-extra*="ref_url"]',
              '[data-log-params*="ref_url"]',
              '[data-log*="ref_url"]',
              '[data-extra*="ref_url"]'
            ].join(',')) || []).length;
          }

          function findRightCitationLayer(titlePattern, requireLinks = true) {
            const candidates = Array.from(document.querySelectorAll('aside, section, article, div'))
              .filter((el) => {
                if (!visible(el)) return false;
                if (requireLinks && !el.querySelector('a[href]')) return false;
                const rect = el.getBoundingClientRect();
                const text = textOf(el);
                return rect.left >= window.innerWidth * 0.55 && rect.width >= 260 && rect.height >= 300 &&
                  text.length > 20 && titlePattern.test(text);
              })
              .map((el) => {
                const rect = el.getBoundingClientRect();
                const text = textOf(el);
                const titleScore = titlePattern.test(text.slice(0, 120)) ? 500 : 0;
                const rightScore = rect.left + rect.width;
                const areaScore = Math.min(rect.width * rect.height / 1000, 500);
                return {el, score: titleScore + rightScore + areaScore};
              })
              .sort((a, b) => b.score - a.score);
            return candidates[0]?.el || null;
          }

          function findDoubaoCitationLayer() {
            const candidates = Array.from(document.querySelectorAll('aside, section, article, div'))
              .filter((el) => {
                if (!visible(el)) return false;
                const rect = el.getBoundingClientRect();
                const text = textOf(el);
                return rect.left >= window.innerWidth * 0.55 && rect.width >= 260 && rect.height >= 300 &&
                  text.length > 20 && !/历史对话|新对话|AI 创作|云盘|更多/.test(text);
              })
              .map((el) => {
                const rect = el.getBoundingClientRect();
                const text = textOf(el);
                const titleScore = /参考资料|参考|资料/.test(text) ? 500 : 0;
                const rightScore = rect.left + rect.width;
                const areaScore = Math.min(rect.width * rect.height / 1000, 500);
                return {el, score: titleScore + rightScore + areaScore};
              })
              .sort((a, b) => b.score - a.score);
            return candidates[0]?.el || null;
          }

          function hasCitationAncestor(el) {
            const ancestor = el.closest('[role="dialog"], [class*="modal" i], [class*="popup" i], [class*="drawer" i], [class*="source" i], [class*="citation" i], [class*="reference" i], [class*="ref" i], [class*="web" i]');
            if (ancestor) return true;
            const text = textOf(el.closest('section, article, li, div') || el).toLowerCase();
            return normalizedTriggers.some((trigger) => text.includes(trigger));
          }

          function visibleCitationCard(el) {
            const card = el.closest('a, li, article, section, div');
            return Boolean(card && visible(card));
          }

          function extractCitationCards(root) {
            const cards = Array.from(root.querySelectorAll('a[href], li, article, section, div, [role="link"], [data-url], [data-href], [data-c="refer_panel"], [data-click-extra*="ref_url"]'))
              .filter((el) => visible(el))
              .map((el) => {
                const text = textOf(el);
                const url = platformId === 'tongyi' ? findTongyiRefUrl(el) : findUrlInElement(el, text);
                return {el, text, url};
              })
              .filter((item) => item.url && isExternalHref(item.url) && item.text.length >= 8)
              .map((item) => {
                const lines = item.text.split(/\\s{2,}|\\n/).map((line) => line.trim()).filter(Boolean);
                const site = inferSiteNameFromText(item.url, item.text);
                const title = lines.find((line) => line.length >= 6 && !line.includes(site) && !/^\\d+$/.test(line)) || item.text || item.url;
                return {title, site_name: site, url: item.url};
              });
            return dedupeItems(cards).slice(0, 80);
          }

          function extractTongyiCitationCards(root) {
            const selector = [
              '[data-click-extra*="ref_url"]',
              '[data-exposure-extra*="ref_url"]',
              '[data-log-params*="ref_url"]',
              '[data-log*="ref_url"]',
              '[data-extra*="ref_url"]'
            ].join(',');
            const nodes = Array.from(root.querySelectorAll?.(selector) || [])
              .filter((el) => el && el.nodeType === 1);
            const cards = nodes
              .map((el) => {
                const card = el.closest('[data-c="refer_panel"], [class*="source-item"], li, article, section, div') || el;
                const text = textOf(card) || textOf(el);
                const url = findTongyiRefUrl(el) || findTongyiRefUrl(card);
                return {text, url};
              })
              .filter((item) => item.url && isExternalHref(item.url) && item.text.length >= 6)
              .map((item) => {
                const site = inferSiteNameFromText(item.url, item.text);
                const lines = item.text.split(/\\s{2,}|\\n/).map((line) => line.trim()).filter(Boolean);
                const title = lines.find((line) => line.length >= 6 && !line.includes(site) && !/^\\d+$/.test(line)) || item.text || item.url;
                return {title, site_name: site, url: item.url};
              });
            return dedupeItems(cards).slice(0, 120);
          }

          function extractDoubaoCitationCards(root) {
            const cards = Array.from(root.querySelectorAll('div.w-full a[href], .w-full a[href]'))
              .filter((a) => isExternalHref(a.href) && visibleCitationCard(a))
              .map((a) => {
                const card = a.closest('div.w-full') || a;
                const text = textOf(card);
                const site = inferSiteName(a, text);
                const lines = text.split(/\\s{2,}|\\n/).map((line) => line.trim()).filter(Boolean);
                const title = lines.find((line) => line.length >= 6 && !line.includes(site) && !/^\\d+$/.test(line)) || textOf(a) || text || a.href;
                return {title, site_name: site, url: a.href};
              });
            return dedupeItems(cards).slice(0, 80);
          }

          function extractYuanbaoCitationCards(root) {
            const cards = Array.from(root.querySelectorAll('#chatReferenceList li, li[dt-ext6], li[class*="agent-dialogue-references"]'))
              .filter((li) => visible(li))
              .map((li) => {
                const text = textOf(li);
                const rawUrl = li.getAttribute('dt-ext6') || '';
                const url = extractUrl(rawUrl);
                return {text, url};
              })
              .filter((item) => item.url && isExternalHref(item.url) && item.text.length >= 8)
              .map((item) => {
                const site = inferSiteNameFromText(item.url, item.text);
                const lines = item.text.split(/\\s{2,}|\\n/).map((line) => line.trim()).filter(Boolean);
                const title = lines.find((line) => line.length >= 6 && !line.includes(site) && !/^\\d+$/.test(line)) || item.text || item.url;
                return {title, site_name: site, url: item.url};
              });
            return dedupeItems(cards).slice(0, 80);
          }

          function findUrlInElement(el, text) {
            const anchor = el.matches?.('a[href]') ? el : el.querySelector?.('a[href]');
            if (anchor?.href) return anchor.href;
            const attrNames = ['href', 'data-url', 'data-href', 'data-link', 'data-jump-url', 'data-target', 'data-value', 'to'];
            const stack = [el, ...Array.from(el.querySelectorAll?.('*') || [])].slice(0, 80);
            for (const node of stack) {
              const structuredUrl = extractStructuredUrl(node);
              if (structuredUrl) return structuredUrl;
              for (const attr of attrNames) {
                const value = node.getAttribute?.(attr);
                const url = extractUrl(value || '');
                if (url) return url;
              }
              for (const attr of Array.from(node.attributes || [])) {
                const url = extractUrl(attr.value || '');
                if (url) return url;
              }
            }
            return extractUrl(text || '');
          }

          function findTongyiRefUrl(el) {
            const stack = [el, ...Array.from(el.querySelectorAll?.('[data-click-extra], [data-exposure-extra], [data-log-params], [data-log], [data-extra]') || [])].slice(0, 80);
            for (const node of stack) {
              for (const attr of ['data-click-extra', 'data-exposure-extra', 'data-log-params', 'data-log', 'data-extra']) {
                const value = node.getAttribute?.(attr);
                if (!value) continue;
                const parsed = parseJsonish(value);
                const refUrl = findKeyInObject(parsed, 'ref_url');
                if (refUrl) return extractUrl(refUrl);
              }
            }
            return '';
          }

          function extractStructuredUrl(node) {
            const attrNames = ['data-click-extra', 'data-exposure-extra', 'data-extra', 'data-log', 'data-spm'];
            for (const attr of attrNames) {
              const value = node.getAttribute?.(attr);
              if (!value) continue;
              const parsed = parseJsonish(value);
              const fromObject = findUrlInObject(parsed);
              if (fromObject) return fromObject;
              const fromText = extractUrl(value);
              if (fromText) return fromText;
            }
            return '';
          }

          function parseJsonish(value) {
            const candidates = [
              value,
              safeDecode(value),
              value.replace(/\\\\\\//g, '/'),
              safeDecode(value).replace(/\\\\\\//g, '/')
            ];
            for (const candidate of candidates) {
              try {
                return JSON.parse(candidate);
              } catch {
                continue;
              }
            }
            return null;
          }

          function findUrlInObject(value) {
            if (!value) return '';
            if (typeof value === 'string') return extractUrl(value);
            if (Array.isArray(value)) {
              for (const item of value) {
                const url = findUrlInObject(item);
                if (url) return url;
              }
              return '';
            }
            if (typeof value === 'object') {
              for (const key of ['ref_url', 'url', 'href', 'link', 'target_url', 'jump_url']) {
                const url = findUrlInObject(value[key]);
                if (url) return url;
              }
              for (const item of Object.values(value)) {
                const url = findUrlInObject(item);
                if (url) return url;
              }
            }
            return '';
          }

          function findKeyInObject(value, keyName) {
            if (!value) return '';
            if (Array.isArray(value)) {
              for (const item of value) {
                const found = findKeyInObject(item, keyName);
                if (found) return found;
              }
              return '';
            }
            if (typeof value === 'object') {
              if (typeof value[keyName] === 'string') return value[keyName];
              for (const item of Object.values(value)) {
                const found = findKeyInObject(item, keyName);
                if (found) return found;
              }
            }
            return '';
          }

          function extractUrl(value) {
            const raw = String(value || '');
            const decoded = safeDecode(raw).replace(/\\\\\\//g, '/');
            const unescaped = raw.replace(/\\\\\\//g, '/');
            const match = decoded.match(/https?:\\/\\/[^\\s"'<>，。)）]+/i) || raw.match(/https?:\\/\\/[^\\s"'<>，。)）]+/i);
            const fallback = unescaped.match(/https?:\\/\\/[^\\s"'<>，。)）]+/i);
            return (match || fallback)?.[0] || '';
          }

          function safeDecode(value) {
            try {
              return decodeURIComponent(value);
            } catch {
              return value;
            }
          }

          function dedupeItems(items) {
            const seen = new Set();
            const output = [];
            for (const item of items) {
              if (!item?.url || seen.has(item.url)) continue;
              seen.add(item.url);
              output.push(item);
            }
            return output;
          }

          function findCitationScroller(root) {
            const base = root?.nodeType === 9
              ? [root.scrollingElement, root.documentElement, root.body]
              : [root];
            const descendants = typeof root?.querySelectorAll === 'function'
              ? Array.from(root.querySelectorAll('*'))
              : [];
            const nodes = [...base, ...descendants]
              .filter((el) => el && el.nodeType === 1 && typeof el.getBoundingClientRect === 'function')
              .filter((el) => {
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return rect.width >= 180 && rect.height >= 120 &&
                  el.scrollHeight > el.clientHeight + 20 &&
                  /(auto|scroll|overlay)/.test(`${style.overflowY} ${style.overflow}`);
              })
              .sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
            if (nodes[0]) return nodes[0];
            return root.scrollHeight > root.clientHeight + 20 ? root : null;
          }

          function currentExternalLinks(root) {
            return new Set(
              Array.from(root.querySelectorAll('a[href]'))
                .filter((a) => visible(a))
                .map((a) => a.href)
                .filter((href) => isExternalHref(href))
            );
          }

          function isExternalHref(href) {
            if (!/^https?:/i.test(href || '')) return false;
            try {
              const hostname = new URL(href).hostname.toLowerCase().replace(/^www\\./, '');
              return !internalDomains().some((domain) => hostname === domain || hostname.endsWith(`.${domain}`));
            } catch {
              return false;
            }
          }

          function internalDomains() {
            const current = location.hostname.toLowerCase().replace(/^www\\./, '');
            const domainsByPlatform = {
              chatgpt: ['chatgpt.com'],
              gemini: ['gemini.google.com'],
              deepseek: ['deepseek.com', 'chat.deepseek.com'],
              doubao: ['doubao.com'],
              yuanbao: ['yuanbao.tencent.com'],
              tongyi: ['tongyi.aliyun.com'],
              kimi: ['kimi.com'],
              wenxin: ['yiyan.baidu.com']
            };
            return Array.from(new Set([current, ...(domainsByPlatform[platformId] || [])].filter(Boolean)));
          }

          function describeElement(el) {
            if (!el) return '';
            const rect = el.getBoundingClientRect();
            const id = el.id ? `#${el.id}` : '';
            const className = String(el.className || '').split(/\\s+/).filter(Boolean).slice(0, 3).join('.');
            const name = `${el.tagName.toLowerCase()}${id}${className ? `.${className}` : ''}`;
            return `${name} ${Math.round(rect.width)}x${Math.round(rect.height)} @${Math.round(rect.left)},${Math.round(rect.top)}`;
          }

          function inferSiteName(anchor, text) {
            const explicit = anchor.getAttribute('data-site') || anchor.getAttribute('data-domain') || anchor.getAttribute('aria-label') || '';
            const merged = `${explicit} ${text || ''}`;
            const domainMatch = merged.match(/([a-z0-9-]+\\.)+[a-z]{2,}/i);
            if (domainMatch) return domainMatch[0].replace(/^www\\./i, '');
            try {
              return new URL(anchor.href).hostname.replace(/^www\\./i, '');
            } catch {
              return '';
            }
          }

          function inferSiteNameFromText(url, text) {
            const domainMatch = String(text || '').match(/([a-z0-9-]+\\.)+[a-z]{2,}/i);
            if (domainMatch) return domainMatch[0].replace(/^www\\./i, '');
            try {
              return new URL(url).hostname.replace(/^www\\./i, '');
            } catch {
              return '';
            }
          }
        }
        """,
        {"triggers": list(triggers), "platformId": platform_id},
    )
    if isinstance(payload, dict):
        items = payload.get("items")
        debug = payload.get("debug")
        item_list = items if isinstance(items, list) else []
        debug_lines = [*playwright_debug, *mouse_debug, *([str(item) for item in debug] if isinstance(debug, list) else [])]
        if platform_id == "wenxin":
            clicked_items, clicked_debug = await _collect_wenxin_citations_by_clicking_cards(page)
            item_list = [*item_list, *clicked_items]
            debug_lines.extend(clicked_debug)
        return item_list, debug_lines
    return [], [*playwright_debug, *mouse_debug, f"unexpected payload type: {type(payload).__name__}"]


async def _collect_wenxin_citations_by_clicking_cards(page) -> tuple[list[dict[str, str]], list[str]]:
    debug: list[str] = []
    try:
        candidates = await page.evaluate(
            """
            async () => {
              const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
              const visible = (el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity) !== 0 &&
                  rect.width >= 180 && rect.height >= 60 && rect.left >= window.innerWidth * 0.45;
              };
              const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
              const roots = Array.from(document.querySelectorAll('[class*="SourcesViewer"], [class*="webListContainer"]'))
                .filter((el) => {
                  const rect = el.getBoundingClientRect();
                  return rect.left >= window.innerWidth * 0.45 && rect.width >= 240;
                });
              const root = roots.sort((a, b) => b.getBoundingClientRect().width * b.getBoundingClientRect().height - a.getBoundingClientRect().width * a.getBoundingClientRect().height)[0] || document.body;
              const scroller = findScroller(root);
              const snapshots = [];
              for (let round = 0; round < 6; round += 1) {
                snapshots.push(...collectCards(root, round));
                if (!scroller) break;
                const beforeTop = scroller.scrollTop;
                scroller.scrollTop = Math.min(scroller.scrollHeight, scroller.scrollTop + Math.max(scroller.clientHeight * 0.85, 300));
                scroller.dispatchEvent(new Event('scroll', {bubbles: true}));
                await sleep(800);
                if (Math.abs(scroller.scrollTop - beforeTop) < 2) break;
              }
              const cards = snapshots.sort((a, b) => a.round - b.round || a.top - b.top || b.area - a.area);
              const selected = [];
              const selectedTexts = new Set();
              for (const item of cards) {
                if (selected.some((other) => other.el.contains(item.el) || item.el.contains(other.el))) continue;
                const key = item.text.slice(0, 120);
                if (selectedTexts.has(key)) continue;
                selectedTexts.add(key);
                selected.push(item);
                if (selected.length >= 40) break;
              }
              selected.forEach((item, index) => item.el.setAttribute('data-geomonitor-wenxin-source-index', String(index)));
              return selected.map((item, index) => {
                const rect = item.el.getBoundingClientRect();
                return {index, text: item.text, width: Math.round(rect.width), height: Math.round(rect.height), top: Math.round(rect.top), round: item.round};
              });

              function collectCards(root, round) {
                return Array.from(root.querySelectorAll('[class^="item__"], [class*=" item__"], [class*="item__"]'))
                  .filter(visible)
                  .map((el) => {
                    const rect = el.getBoundingClientRect();
                    const text = textOf(el);
                    return {el, text, area: rect.width * rect.height, top: rect.top, round};
                  })
                  .filter((item) => item.text.length >= 20 && /\\d{4}[-/]\\d{1,2}[-/]\\d{1,2}|搜狗|百度|搜狐|网易|新浪|腾讯|凤凰|今日头条|百家号|知乎|什么值得买|太平洋|中关村/.test(item.text));
              }

              function findScroller(root) {
                const nodes = [root, ...Array.from(root.querySelectorAll('*'))]
                  .filter((el) => {
                    const style = getComputedStyle(el);
                    return el.scrollHeight > el.clientHeight + 20 && /(auto|scroll|overlay)/.test(`${style.overflowY} ${style.overflow}`);
                  })
                  .sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
                return nodes[0] || (root.scrollHeight > root.clientHeight + 20 ? root : null);
              }
            }
            """
        )
    except Exception as exc:  # noqa: BLE001
        return [], [f"wenxinClickCardsDiscoveryError={exc}"]

    if not isinstance(candidates, list) or not candidates:
        return [], ["wenxinClickCards=(none)"]

    debug.append(f"wenxinClickCards candidates={len(candidates)}")
    items: list[dict[str, str]] = []
    original_url = page.url
    for candidate in candidates[:40]:
        if not isinstance(candidate, dict):
            continue
        index = candidate.get("index")
        text = str(candidate.get("text") or "")
        locator = page.locator(f'[data-geomonitor-wenxin-source-index="{index}"]').first
        title, site_name = _parse_wenxin_card_text(text)
        try:
            await locator.scroll_into_view_if_needed(timeout=1500)
            box = await locator.bounding_box(timeout=1500)
            if not box:
                debug.append(f"wenxinCardNoBox index={index}")
                continue
            debug.append(
                f"wenxinCardTry index={index} text={text[:60]} box={round(box['width'])}x{round(box['height'])}"
            )
            url = await _click_wenxin_card_for_url(page, box, original_url, index, debug)

            if _is_external_citation_url(url, "wenxin"):
                items.append({"title": title or url, "site_name": site_name or _site_name_from_url(url), "url": url})
                debug.append(f"wenxinCardUrl index={index} url={url}")
            else:
                if title or site_name:
                    items.append({"title": title or text[:120], "site_name": site_name, "url": ""})
                debug.append(f"wenxinCardNoUrl index={index} current={page.url}")
        except Exception as exc:  # noqa: BLE001
            debug.append(f"wenxinCardError index={index}: {exc}")
    return _dedupe_citation_items(items), debug


async def _click_wenxin_card_for_url(page, box: dict, original_url: str, index, debug: list[str]) -> str:
    points = [
        (0.50, 0.50),
        (0.50, 0.20),
        (0.35, 0.25),
        (0.72, 0.25),
        (0.50, 0.78),
    ]
    for point_index, (x_ratio, y_ratio) in enumerate(points):
        x = box["x"] + box["width"] * x_ratio
        y = box["y"] + box["height"] * y_ratio
        debug.append(f"wenxinCardClick index={index} point={point_index}:{round(x)},{round(y)}")
        url = await _click_and_capture_external_url(page, x, y, original_url, debug)
        if _is_external_citation_url(url, "wenxin"):
            return url
        await page.wait_for_timeout(350)
    return ""


async def _click_and_capture_external_url(page, x: float, y: float, original_url: str, debug: list[str]) -> str:
    try:
        async with page.context.expect_page(timeout=2500) as popup_info:
            await page.mouse.click(x, y)
        popup = await popup_info.value
        try:
            url = await _wait_for_external_popup_url(popup, "wenxin")
        finally:
            try:
                await popup.close()
            except Exception:
                pass
        if url:
            return url
        debug.append("wenxinPopupNoExternalUrl")
        return ""
    except Exception:
        await page.mouse.click(x, y)
        await page.wait_for_timeout(1000)
        if _is_external_citation_url(page.url, "wenxin"):
            url = page.url
            try:
                await page.goto(original_url, wait_until="domcontentloaded")
                await page.wait_for_timeout(1000)
            except Exception:
                pass
            return url
        if page.url != original_url:
            debug.append(f"wenxinSamePageNonExternalUrl={page.url}")
            try:
                await page.goto(original_url, wait_until="domcontentloaded")
                await page.wait_for_timeout(1000)
            except Exception:
                pass
        return ""


async def _wait_for_external_popup_url(popup, platform_id: str) -> str:
    try:
        await popup.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass
    deadline = asyncio.get_running_loop().time() + 6
    last_url = ""
    while asyncio.get_running_loop().time() < deadline:
        last_url = popup.url
        if _is_external_citation_url(last_url, platform_id):
            return last_url
        await asyncio.sleep(0.3)
    return last_url if _is_external_citation_url(last_url, platform_id) else ""


def _parse_wenxin_card_text(text: str) -> tuple[str, str]:
    normalized = " ".join(text.split()).strip()
    lines = [line.strip() for line in text.replace(" - ", "\n").splitlines() if line.strip()]
    if not lines:
        parts = normalized.split(" ")
        lines = [part for part in parts if part]
    title = lines[0] if lines else normalized
    site_name = ""
    for line in lines[1:5]:
        if re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", line):
            continue
        if 1 < len(line) <= 20 and not line.startswith(("http://", "https://")):
            site_name = line
            break
    return title[:500], site_name[:180]


def _is_external_citation_url(url: str, platform_id: str) -> bool:
    if not url.startswith(("http://", "https://")):
        return False
    try:
        hostname = (urlparse(url).hostname or "").lower().removeprefix("www.")
    except Exception:
        return False
    internal = {
        "chatgpt": {"chatgpt.com"},
        "gemini": {"gemini.google.com"},
        "deepseek": {"deepseek.com", "chat.deepseek.com"},
        "doubao": {"doubao.com"},
        "yuanbao": {"yuanbao.tencent.com"},
        "tongyi": {"tongyi.aliyun.com", "qianwen.com"},
        "kimi": {"kimi.com"},
        "wenxin": {"yiyan.baidu.com"},
    }.get(platform_id, set())
    return not any(hostname == domain or hostname.endswith(f".{domain}") for domain in internal)


def _dedupe_citation_items(items: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    output: list[dict[str, str]] = []
    empty_seen: set[tuple[str, str]] = set()
    for item in items:
        url = str(item.get("url") or "")
        if not url:
            key = (str(item.get("title") or ""), str(item.get("site_name") or ""))
            if not any(key) or key in empty_seen:
                continue
            empty_seen.add(key)
            output.append(item)
            continue
        if url in seen:
            continue
        seen.add(url)
        output.append(item)
    return output


async def _playwright_click_citation_candidate(page, triggers: tuple[str, ...], platform_id: str) -> tuple[bool, list[str]]:
    if platform_id not in {"tongyi", "doubao"}:
        return False, []
    if platform_id == "doubao":
        patterns = [r"参考\s*\d+\s*篇\s*资料", r"\d+\s*篇\s*资料", *[re.escape(trigger) for trigger in triggers if trigger.strip()]]
    else:
        patterns = [r"\d+\s*篇\s*来源", *[re.escape(trigger) for trigger in triggers if trigger.strip()]]
    errors: list[str] = []
    for pattern in patterns:
        try:
            count = await page.get_by_text(re.compile(pattern)).count()
            if count <= 0:
                errors.append(f"playwrightTextNoMatch={pattern}")
                continue
            clicked = False
            for index in range(count - 1, max(count - 13, -1), -1):
                candidate = page.get_by_text(re.compile(pattern)).nth(index)
                try:
                    if not await candidate.is_visible(timeout=500):
                        continue
                    await candidate.scroll_into_view_if_needed(timeout=1500)
                    await page.wait_for_timeout(200)
                    box = await candidate.bounding_box()
                    text = (await candidate.inner_text(timeout=1000)).replace("\n", " ").strip()
                    if not box:
                        await candidate.click(timeout=1500, force=True)
                        clicked = True
                        errors.append(f"playwrightClick text={text[:80]} pattern={pattern} mode=locator")
                    else:
                        x = box["x"] + box["width"] / 2
                        y = box["y"] + box["height"] / 2
                        await page.mouse.move(x, y)
                        await page.wait_for_timeout(150)
                        await page.mouse.click(x, y)
                        clicked = True
                        errors.append(
                            f"playwrightClick text={text[:80]} pattern={pattern} point={round(x)},{round(y)} "
                            f"box={round(box['width'])}x{round(box['height'])}"
                        )
                    await page.wait_for_timeout(10000)
                    return True, errors
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"playwrightClickCandidateError={pattern}:{exc}")
                    continue
            if not clicked:
                errors.append(f"playwrightTextInvisible={pattern}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"playwrightClickError={pattern}:{exc}")
    return False, errors[:12]


async def _mouse_click_citation_candidate(page, triggers: tuple[str, ...], platform_id: str) -> list[str]:
    try:
        candidate = await page.evaluate(
            """
            ({triggers, platformId}) => {
              const normalizedTriggers = triggers.map((item) => String(item || '').trim().toLowerCase()).filter(Boolean);
              const visible = (el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity) !== 0 &&
                  rect.width > 8 && rect.height > 8 && rect.bottom > 0 && rect.right > 0 &&
                  rect.top < window.innerHeight && rect.left < window.innerWidth;
              };
              const textOf = (el) => [
                el.innerText || el.textContent || '',
                el.getAttribute('aria-label') || '',
                el.getAttribute('title') || ''
              ].join(' ').replace(/\\s+/g, ' ').trim();
              const platformMatch = (el, text) => {
                const normalized = text.toLowerCase();
                if (platformId === 'yuanbao' && (el.id === 'search-guide-tool' || el.querySelector?.('#search-guide-tool'))) return true;
                if (platformId === 'deepseek' && /\\d+\\s*个\\s*网页/.test(text)) return true;
                if (platformId === 'doubao' && /参考\\s*\\d+\\s*篇\\s*资料/.test(text)) return true;
                if (platformId === 'tongyi' && /\\d+\\s*篇\\s*来源/.test(text)) return true;
                if (platformId === 'wenxin' && /参考\\s*\\d+\\s*个\\s*网页/.test(text)) return true;
                return normalizedTriggers.some((trigger) => normalized.includes(trigger));
              };
              const elements = Array.from(document.querySelectorAll('button, a, span, div, p, li, td, tr, [role="button"], [tabindex], [onclick], [id="search-guide-tool"], [class*="message-action-button-third"], [class*="entry-btn"]'));
              const candidates = elements
                .filter(visible)
                .map((el) => ({el, text: textOf(el)}))
                .filter((item) => item.text && item.text.length <= 80 && platformMatch(item.el, item.text))
                .map((item) => {
                  item.el.scrollIntoView({block: 'center', inline: 'nearest'});
                  const rect = item.el.getBoundingClientRect();
                  const area = rect.width * rect.height;
                  const digitScore = /\\d/.test(item.text) ? 100 : 0;
                  const shortTextScore = Math.max(0, 120 - item.text.length);
                  const smallAreaScore = Math.max(0, 800 - Math.min(area / 10, 800));
                  const exactDeepseekScore = platformId === 'deepseek' && /^\\s*(已阅读\\s*)?\\d+\\s*个\\s*网页\\s*$/.test(item.text) ? 1000 : 0;
                  const exactDoubaoScore = platformId === 'doubao' && /^\\s*参考\\s*\\d+\\s*篇\\s*资料\\s*$/.test(item.text) ? 1000 : 0;
                  const tooLargePenalty = area > 50000 ? 1000 : 0;
                  const yScore = rect.top / Math.max(1, window.innerHeight);
                  return {
                    text: item.text,
                    x: Math.max(1, Math.min(window.innerWidth - 2, rect.left + rect.width / 2)),
                    y: Math.max(1, Math.min(window.innerHeight - 2, rect.top + rect.height / 2)),
                    score: exactDeepseekScore + exactDoubaoScore + digitScore + shortTextScore + smallAreaScore + yScore - tooLargePenalty,
                    desc: `${item.el.tagName.toLowerCase()} ${Math.round(rect.width)}x${Math.round(rect.height)} @${Math.round(rect.left)},${Math.round(rect.top)}`
                  };
                })
                .sort((a, b) => b.score - a.score);
              return candidates[0] || null;
            }
            """,
            {"triggers": list(triggers), "platformId": platform_id},
        )
        if not isinstance(candidate, dict):
            return ["mouseClickCandidate=(none)"]
        x = float(candidate.get("x", 0))
        y = float(candidate.get("y", 0))
        text = str(candidate.get("text", ""))[:80]
        desc = str(candidate.get("desc", ""))
        await page.mouse.move(x, y)
        await page.wait_for_timeout(200)
        await page.mouse.click(x, y)
        await page.wait_for_timeout(10000)
        return [f"mouseClickCandidate text={text} point={round(x)},{round(y)} target={desc}"]
    except Exception as exc:  # noqa: BLE001
        return [f"mouseClickError={exc}"]


def _normalize_citations(items: list[dict[str, str]]) -> list[dict[str, str]]:
    citations: list[dict[str, str]] = []
    for item in items:
        url = str(item.get("url", "")).strip()
        if not url.startswith(("http://", "https://")):
            title = " ".join(str(item.get("title", "")).split()).strip()
            site_name = " ".join(str(item.get("site_name", "")).split()).strip()
            if title or site_name:
                citations.append({"title": title[:500], "site_name": site_name[:180], "url": ""})
            continue
        title = " ".join(str(item.get("title", "")).split()).strip() or url
        site_name = _site_name_from_url(url)
        citations.append({"title": title[:500], "site_name": site_name[:180], "url": url})
    return citations


def _filter_platform_citations(citations: list[dict[str, str]], platform_id: str, answer_url: str | None = None) -> list[dict[str, str]]:
    filtered: list[dict[str, str]] = []
    for citation in citations:
        url = str(citation.get("url") or "").strip()
        if not url:
            filtered.append(citation)
            continue
        if not _is_external_citation_url(url, platform_id):
            continue
        if answer_url and _same_url_without_fragment(url, answer_url):
            continue
        filtered.append(citation)
    return _dedupe_citation_items(filtered)


def _same_url_without_fragment(left: str, right: str) -> bool:
    try:
        left_parsed = urlparse(left)
        right_parsed = urlparse(right)
        return (
            left_parsed.scheme,
            left_parsed.netloc,
            left_parsed.path,
            left_parsed.query,
        ) == (
            right_parsed.scheme,
            right_parsed.netloc,
            right_parsed.path,
            right_parsed.query,
        )
    except Exception:
        return left == right


def _log_citation_debug(platform_id: str, lines: list[str]) -> None:
    if not lines:
        print(f"[citation-debug] {platform_id}: no debug lines returned", flush=True)
        return
    for line in lines:
        print(f"[citation-debug] {platform_id}: {line}", flush=True)


def _site_name_from_url(url: str) -> str:
    try:
        hostname = urlparse(url).hostname or url
    except Exception:
        return url
    return hostname.removeprefix("www.")


def _blocked_text_match(text: str) -> str | None:
    normalized = text.casefold()
    for marker in HUMAN_VERIFICATION_TEXTS:
        if marker.casefold() in normalized:
            return marker
    return None


def _is_blocked_exception(message: str) -> bool:
    normalized = message.casefold()
    return "blocked:" in normalized or "blocking popup" in normalized


def _is_profile_in_use_error(message: str) -> bool:
    normalized = message.casefold()
    return "processsingleton" in normalized or "profile is already in use" in normalized or "profile directory" in normalized


def _relative_run_path(path: Path) -> str:
    if len(path.parts) >= 2:
        return str(Path(path.parts[-2]) / path.parts[-1])
    return path.name
