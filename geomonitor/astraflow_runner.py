from __future__ import annotations

import asyncio
import json
import os
import random
import ssl
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from .env_loader import load_env_file
from .models import AIPlatform, AnswerRecord, Question, RunnerConfig


DEFAULT_ASTRAFLOW_CHAT_URL = "https://api.modelverse.cn/v1/chat/completions"


class AstraFlowRunner:
    def __init__(self, runner_config: RunnerConfig) -> None:
        self.config = runner_config
        load_env_file()

    async def run_question(
        self,
        run_id: str,
        platform: AIPlatform,
        question: Question,
        raw_response_path: Path,
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
            payload = await asyncio.to_thread(self._call_api, platform, question.question)
            raw_response_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            base.raw_response_path = _relative_run_path(raw_response_path)
            answer_text = _extract_answer_text(payload)
            if not answer_text.strip():
                base.status = "empty_answer"
                base.error_message = "AstraFlow response did not contain assistant text."
                return base
            base.answer_text = answer_text
            base.status = "success"
            return base
        except TimeoutError as exc:
            base.status = "timeout"
            base.error_message = str(exc)
            return base
        except Exception as exc:  # noqa: BLE001
            base.status = "failed"
            base.error_message = str(exc)
            return base

    async def random_delay(self) -> None:
        delay = random.uniform(self.config.min_delay_seconds, self.config.max_delay_seconds)
        if delay > 0:
            await asyncio.sleep(delay)

    def _call_api(self, platform: AIPlatform, question: str) -> dict[str, Any]:
        api_key = os.environ.get("ASTRAFLOW_API_KEY") or os.environ.get("MODELVERSE_API_KEY")
        if not api_key:
            raise RuntimeError("Missing API key. Set ASTRAFLOW_API_KEY in .env.")
        if not platform.model:
            raise RuntimeError(f"API platform {platform.platform_id} requires model.")

        url = platform.api_base_url or os.environ.get("ASTRAFLOW_API_BASE_URL") or DEFAULT_ASTRAFLOW_CHAT_URL
        body: dict[str, Any] = {
            "model": platform.model,
            "messages": [{"role": "user", "content": question}],
            "temperature": 0.2,
        }
        if platform.web_search:
            body["web_search"] = {"enable": True}
            if platform.web_search_vendor:
                body["web_search"]["vendor"] = platform.web_search_vendor

        request = urllib.request.Request(
            url=url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        context = _ssl_context()
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds, context=context) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"AstraFlow API HTTP {exc.code}: {detail}") from exc
        except TimeoutError as exc:
            raise TimeoutError(f"AstraFlow API timeout after {self.config.timeout_seconds} seconds") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(_format_url_error(exc)) from exc


def _extract_answer_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts: list[str] = []
                for item in content:
                    if isinstance(item, dict):
                        text = item.get("text") or item.get("content")
                        if isinstance(text, str):
                            parts.append(text)
                return "\n".join(parts)
        text = choices[0].get("text") if isinstance(choices[0], dict) else None
        if isinstance(text, str):
            return text
    output_text = payload.get("output_text")
    return output_text if isinstance(output_text, str) else ""


def _ssl_context() -> ssl.SSLContext:
    verify_ssl = os.environ.get("ASTRAFLOW_VERIFY_SSL", "true").strip().casefold()
    if verify_ssl in {"0", "false", "no"}:
        return ssl._create_unverified_context()

    ca_bundle = os.environ.get("ASTRAFLOW_CA_BUNDLE")
    if ca_bundle:
        return ssl.create_default_context(cafile=ca_bundle)

    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _format_url_error(exc: urllib.error.URLError) -> str:
    reason = exc.reason
    if isinstance(reason, ssl.SSLError):
        return (
            f"AstraFlow API SSL verification failed: {reason}. "
            "Install requirements.txt, or set ASTRAFLOW_CA_BUNDLE in .env if you are behind an HTTPS proxy."
        )
    return f"AstraFlow API connection failed: {reason}"


def _relative_run_path(path: Path) -> str:
    if len(path.parts) >= 2:
        return str(Path(path.parts[-2]) / path.parts[-1])
    return path.name
