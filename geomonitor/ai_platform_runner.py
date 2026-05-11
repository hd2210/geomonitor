from __future__ import annotations

import asyncio
import random
from datetime import datetime
from pathlib import Path

from .models import AIPlatform, AnswerRecord, Question, RunnerConfig


COMMON_BLOCKED_TEXTS = (
    "verify you are human",
    "checking if the site connection is secure",
    "please verify you are human",
    "cloudflare",
    "请验证您是真人",
    "正在检查您是否是真人",
    "请稍候",
    "unusual traffic",
)


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
    ) -> AnswerRecord:
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        base = AnswerRecord(
            run_id=run_id,
            timestamp=timestamp,
            platform_id=platform.platform_id,
            platform_name=platform.platform_name,
            question_id=question.question_id,
            question=question.question,
        )

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
                context, page, should_close_context = await self._open_context_and_page(playwright, profile_dir)
                page.set_default_timeout(self.config.timeout_seconds * 1000)
                try:
                    await page.goto(platform.url, wait_until="domcontentloaded")
                    status = await self._detect_blockers(page, platform)
                    if status:
                        base.status = status[0]
                        base.error_message = status[1]
                        await self._save_html(page, html_path, base)
                        return base

                    await self._submit_question(page, platform, question.question)
                    answer_text = await self._wait_and_extract_answer(page, platform)
                    await self._save_html(page, html_path, base)

                    if not answer_text.strip():
                        base.status = "empty_answer"
                        base.error_message = "Answer container was found but extracted text was empty."
                        return base

                    base.answer_text = answer_text
                    base.raw_html_path = _relative_run_path(html_path)
                    try:
                        await self._screenshot_answer(page, platform, screenshot_path)
                        base.screenshot_path = _relative_run_path(screenshot_path)
                        base.status = "success"
                    except Exception as exc:  # noqa: BLE001
                        base.status = "partial_success"
                        base.screenshot_error = str(exc)
                    return base
                except (PlaywrightTimeoutError, TimeoutError) as exc:
                    base.status = "timeout"
                    base.error_message = f"Response timeout after {self.config.timeout_seconds} seconds: {exc}"
                    await self._try_save_html(page, html_path, base)
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

    async def _open_context_and_page(self, playwright, profile_dir: Path):
        if self.config.browser_cdp_url:
            browser = await playwright.chromium.connect_over_cdp(self.config.browser_cdp_url)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = await context.new_page()
            await page.set_viewport_size({"width": self.config.viewport_width, "height": self.config.viewport_height})
            return context, page, False

        launch_options = self._launch_options(profile_dir)
        try:
            context = await playwright.chromium.launch_persistent_context(**launch_options)
        except Exception as exc:
            fallback = self._local_chrome_fallback(profile_dir)
            if fallback is None or self.config.browser_executable_path or self.config.browser_channel:
                raise
            print(f"Playwright bundled Chromium unavailable, retrying with local Chrome: {exc}")
            context = await playwright.chromium.launch_persistent_context(**fallback)
        page = context.pages[0] if context.pages else await context.new_page()
        return context, page, True

    def _local_chrome_fallback(self, profile_dir: Path) -> dict | None:
        chrome_path = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        if not chrome_path.exists():
            return None
        launch_options = self._launch_options(profile_dir)
        launch_options["executable_path"] = str(chrome_path)
        return launch_options

    async def _detect_blockers(self, page, platform: AIPlatform) -> tuple[str, str] | None:
        selectors = platform.selectors
        if selectors.blocked_indicator and await _is_visible(page, selectors.blocked_indicator, timeout=1500):
            return "blocked", f"Blocked indicator detected: {selectors.blocked_indicator}"
        body_text = await _body_text(page)
        matched_text = _blocked_text_match(body_text)
        if matched_text:
            return "blocked", f"Blocked page text detected: {matched_text}"
        if selectors.login_indicator and await _is_visible(page, selectors.login_indicator, timeout=1500):
            return "login_required", f"Login indicator detected: {selectors.login_indicator}"
        return None

    async def _submit_question(self, page, platform: AIPlatform, question: str) -> None:
        selectors = platform.selectors
        input_locator = await _first_visible_locator(page, selectors.input, self.config.timeout_seconds * 1000)
        await input_locator.click()
        try:
            await input_locator.fill(question)
        except Exception:
            await page.keyboard.press("Meta+A")
            await page.keyboard.type(question)

        if selectors.submit:
            submit_locator = await _first_visible_locator(page, selectors.submit, 3000)
        else:
            submit_locator = None

        if submit_locator:
            try:
                await submit_locator.click()
            except Exception:
                await input_locator.press("Enter")
        else:
            await input_locator.press("Enter")

    async def _wait_and_extract_answer(self, page, platform: AIPlatform) -> str:
        selectors = platform.selectors
        deadline = asyncio.get_running_loop().time() + self.config.timeout_seconds
        last_text = ""
        stable_rounds = 0

        await page.locator(selectors.answer_container).last.wait_for(state="visible")
        while asyncio.get_running_loop().time() < deadline:
            if selectors.blocked_indicator and await _is_visible(page, selectors.blocked_indicator, timeout=300):
                raise RuntimeError(f"Blocked indicator detected during response: {selectors.blocked_indicator}")
            body_text = await _body_text(page)
            matched_text = _blocked_text_match(body_text)
            if matched_text:
                raise RuntimeError(f"Blocked page text detected during response: {matched_text}")

            text = await self._extract_text(page, platform)
            if text.strip() and text == last_text:
                stable_rounds += 1
            else:
                stable_rounds = 0
                last_text = text

            if selectors.done_indicator and await _is_visible(page, selectors.done_indicator, timeout=300):
                return text
            if selectors.stop_generating and not await _is_visible(page, selectors.stop_generating, timeout=300) and text.strip():
                stable_rounds += 1
            if stable_rounds >= 3 and text.strip():
                return text
            await asyncio.sleep(2)

        raise TimeoutError(f"Response timeout after {self.config.timeout_seconds} seconds")

    async def _extract_text(self, page, platform: AIPlatform) -> str:
        selectors = platform.selectors
        if selectors.answer_item:
            items = page.locator(selectors.answer_item)
            count = await items.count()
            if count:
                return (await items.nth(count - 1).inner_text()).strip()
        return (await page.locator(selectors.answer_container).last.inner_text()).strip()

    async def _screenshot_answer(self, page, platform: AIPlatform, screenshot_path: Path) -> None:
        locator = page.locator(platform.selectors.answer_item or platform.selectors.answer_container).last
        await locator.screenshot(path=str(screenshot_path))

    async def _save_html(self, page, html_path: Path, record: AnswerRecord) -> None:
        html_path.write_text(await page.content(), encoding="utf-8")
        record.raw_html_path = _relative_run_path(html_path)

    async def _try_save_html(self, page, html_path: Path, record: AnswerRecord) -> None:
        try:
            await self._save_html(page, html_path, record)
        except Exception:
            pass


async def _is_visible(page, selector: str, timeout: int) -> bool:
    return await _first_visible_locator(page, selector, timeout) is not None


async def _first_visible_locator(page, selector: str, timeout: int):
    try:
        deadline = asyncio.get_running_loop().time() + timeout / 1000
        while asyncio.get_running_loop().time() < deadline:
            locators = page.locator(selector)
            count = await locators.count()
            for index in range(count):
                candidate = locators.nth(index)
                try:
                    if await candidate.is_visible(timeout=250):
                        return candidate
                except Exception:
                    continue
            await asyncio.sleep(0.25)
        return None
    except Exception:
        return None


async def _body_text(page) -> str:
    try:
        return await page.locator("body").inner_text(timeout=1000)
    except Exception:
        return ""


def _blocked_text_match(text: str) -> str | None:
    normalized = text.casefold()
    for marker in COMMON_BLOCKED_TEXTS:
        if marker.casefold() in normalized:
            return marker
    return None


def _relative_run_path(path: Path) -> str:
    if len(path.parts) >= 2:
        return str(Path(path.parts[-2]) / path.parts[-1])
    return path.name
