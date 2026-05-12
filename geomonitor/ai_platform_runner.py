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
    "验证码",
    "安全检查",
    "安全审核",
    "内容安全",
    "安全验证",
    "风险审核",
    "风险提示",
    "违反相关",
    "风控",
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
                    await _close_blocking_popups(page)

                    try:
                        await self._open_new_conversation(page, platform)
                    except Exception as exc:  # noqa: BLE001
                        base.status = "blocked" if _is_blocked_exception(str(exc)) else "failed"
                        base.error_message = f"Failed to open a new conversation: {exc}"
                        await self._save_html(page, html_path, base)
                        await self._pause_for_blocked_debug(page, base.status, base.error_message)
                        return base

                    status = await self._detect_blockers(page, platform)
                    if status:
                        base.status = status[0]
                        base.error_message = status[1]
                        await self._save_html(page, html_path, base)
                        await self._pause_for_blocked_debug(page, base.status, base.error_message)
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
        context = await self._launch_persistent_context_with_profile_retry(playwright, launch_options)
        page = context.pages[0] if context.pages else await context.new_page()
        return context, page, True

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

    async def prepare_login(self, platform: AIPlatform) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError("Playwright is not installed. Run: pip install -r requirements.txt") from exc

        profile_dir = Path(self.config.browser_profile_dir) / platform.platform_id
        profile_dir.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as playwright:
            context, page, should_close_context = await self._open_context_and_page(playwright, profile_dir)
            try:
                await page.goto(platform.url, wait_until="domcontentloaded")
                print(f"Login browser opened for {platform.platform_name}. Sign in, then close the browser window.")
                while context.pages:
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
        if input_locator is None:
            return "blocked", f"Input selector was not visible after trying to open a new conversation: {platform.selectors.input}"

        popup_closed, popup_error = await _close_blocking_popups(page)
        if not popup_closed:
            return "blocked", popup_error or "Blocking popup could not be closed."
        return None

    async def _open_new_conversation(self, page, platform: AIPlatform) -> None:
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
        if input_locator is None:
            raise RuntimeError(f"Input selector was not visible: {selectors.input}")
        await input_locator.click()
        try:
            await input_locator.fill(question)
        except Exception:
            await page.keyboard.press("Meta+A")
            await page.keyboard.type(question)
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
        await _expand_scrollable_page(page)
        await page.screenshot(path=str(screenshot_path), full_page=True)

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
    if platform.platform_id == "yuanbao" and await _try_click_yuanbao_new_chat_icon(page, platform):
        return True

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


async def _try_click_yuanbao_new_chat_icon(page, platform: AIPlatform) -> bool:
    try:
        clicked = await page.evaluate(
            """
            () => {
              const visible = (el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden' && rect.width >= 20 &&
                  rect.height >= 20 && rect.left >= 45 && rect.left <= 180 && rect.top >= 70 && rect.top <= 190;
              };
              const elements = Array.from(document.querySelectorAll('button, a, [role="button"], [class*="btn"], [class*="icon"]'))
                .filter((el) => visible(el) && el.querySelector('svg'));
              const scored = elements
                .map((el) => {
                  const rect = el.getBoundingClientRect();
                  const text = `${el.textContent || ''} ${el.getAttribute('aria-label') || ''} ${el.getAttribute('title') || ''}`;
                  const plusHint = /新建|新对话|new|create|\\+/.test(text.toLowerCase()) ? 100 : 0;
                  const xScore = Math.max(0, 70 - Math.abs((rect.left + rect.width / 2) - 112));
                  return {el, score: plusHint + xScore};
                })
                .sort((a, b) => b.score - a.score);
              const target = scored[0]?.el;
              if (!target) return false;
              target.click();
              return true;
            }
            """
        )
        if not clicked:
            return False
        await page.wait_for_timeout(1000)
        return await _first_visible_locator(page, platform.selectors.input, timeout=2500) is not None
    except Exception:
        return False


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
                  const viewportWidth = window.innerWidth || document.documentElement.clientWidth;
                  const viewportHeight = window.innerHeight || document.documentElement.clientHeight;
                  const centerX = viewportWidth / 2;
                  const centerY = viewportHeight / 2;

                  const isVisible = (el) => {
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) {
                      return false;
                    }
                    const rect = el.getBoundingClientRect();
                    return rect.width >= 160 && rect.height >= 100 && rect.bottom > 0 && rect.right > 0 &&
                      rect.left < viewportWidth && rect.top < viewportHeight;
                  };

                  const coversCenter = (rect) =>
                    rect.left <= centerX && rect.right >= centerX && rect.top <= centerY && rect.bottom >= centerY;

                  const likelyDialog = (el, style, rect) => {
                    const role = (el.getAttribute('role') || '').toLowerCase();
                    const ariaModal = (el.getAttribute('aria-modal') || '').toLowerCase();
                    const className = String(el.className || '').toLowerCase();
                    const id = String(el.id || '').toLowerCase();
                    const name = `${className} ${id}`;
                    const zIndex = Number.parseInt(style.zIndex || '0', 10) || 0;
                    const fixedLayer = ['fixed', 'sticky'].includes(style.position) && zIndex >= 10;
                    const namedLayer = /(^|[-_\\s])(modal|popup|mask|overlay|drawer)([-_\\s]|$)/.test(name);
                    const modalRole = role === 'dialog' || role === 'alertdialog' || ariaModal === 'true';
                    const largeEnough = rect.width >= Math.min(360, viewportWidth * 0.35) &&
                      rect.height >= Math.min(180, viewportHeight * 0.25);
                    const coversEnough = rect.width * rect.height >= viewportWidth * viewportHeight * 0.18;
                    return (modalRole || namedLayer || fixedLayer) && (largeEnough || coversEnough) && coversCenter(rect);
                  };

                  return Array.from(document.querySelectorAll('body *')).some((el) => {
                    if (!isVisible(el)) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return likelyDialog(el, style, rect);
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
            "风控",
            "unusual traffic",
            "cloudflare",
        )
        if any(marker.casefold() in normalized for marker in decisive_markers):
            return True
        input_locator = await _first_visible_locator(page, "textarea || [contenteditable='true'] || .ql-editor", 500)
        return input_locator is None and len(normalized) < 1200
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


async def _screenshot_yuanbao(page, screenshot_path: Path) -> None:
    try:
        await page.evaluate(
            """
            () => {
              const selectors = [
                '.agent-chat__list',
                '[class*="agent-chat__list"]',
                '.agent-dialogue__content',
                '[class*="agent-dialogue__content"]',
                'main'
              ];
              const roots = selectors.flatMap((selector) => Array.from(document.querySelectorAll(selector)));
              const target = roots
                .filter((el) => el.scrollHeight > el.clientHeight + 80)
                .sort((a, b) => (b.scrollHeight * b.clientWidth) - (a.scrollHeight * a.clientWidth))[0];
              if (!target) return;
              target.scrollTop = target.scrollHeight;
            }
            """
        )
        await page.wait_for_timeout(300)
        locator = await _first_visible_locator(
            page,
            ".agent-dialogue__content || [class*='agent-dialogue__content'] || main || body",
            2000,
        )
        if locator is not None:
            await locator.screenshot(path=str(screenshot_path))
            return
    except Exception:
        pass
    await page.screenshot(path=str(screenshot_path), full_page=True)


def _blocked_text_match(text: str) -> str | None:
    normalized = text.casefold()
    for marker in COMMON_BLOCKED_TEXTS:
        if marker.casefold() in normalized:
            return marker
    return None


def _is_blocked_exception(message: str) -> bool:
    normalized = message.casefold()
    return "blocked:" in normalized or "blocking popup" in normalized or "input selector was not visible" in normalized


def _is_profile_in_use_error(message: str) -> bool:
    normalized = message.casefold()
    return "processsingleton" in normalized or "profile is already in use" in normalized or "profile directory" in normalized


def _relative_run_path(path: Path) -> str:
    if len(path.parts) >= 2:
        return str(Path(path.parts[-2]) / path.parts[-1])
    return path.name
