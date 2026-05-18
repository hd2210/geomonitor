from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import (
    AIPlatform,
    MonitorConfig,
    PlatformSelectors,
    Question,
    RunnerConfig,
    ScheduleConfig,
    TargetKeyword,
)
from .platform_templates import browser_platform_defaults, merge_browser_template


class ConfigError(ValueError):
    pass


def load_config(path: str | Path) -> MonitorConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"Config file does not exist: {config_path}")

    raw_text = config_path.read_text(encoding="utf-8")
    if config_path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml
        except ImportError as exc:
            raise ConfigError("YAML config requires PyYAML. Install requirements.txt.") from exc
        data = yaml.safe_load(raw_text)
    else:
        data = json.loads(raw_text)

    if not isinstance(data, dict):
        raise ConfigError("Config root must be an object.")

    return parse_config(data)


def parse_config(data: dict[str, Any]) -> MonitorConfig:
    questions = tuple(_parse_questions(data.get("questions")))
    keywords = tuple(_parse_keywords(data.get("target_keywords")))
    run_mode = data.get("run_mode") or _infer_legacy_run_mode(data)
    if run_mode not in {"browser", "api"}:
        raise ConfigError("run_mode must be either browser or api.")
    browser_platforms = tuple(_parse_platforms(_browser_platform_source(data), forced_method="browser"))
    api_platforms = tuple(_parse_platforms(_api_platform_source(data), forced_method="api"))
    platforms = tuple(p for p in (browser_platforms if run_mode == "browser" else api_platforms) if p.enabled)
    schedule = _parse_schedule(data.get("schedule"))
    output_dir = _required_str(data, "output_dir")
    runner = _parse_runner(data.get("runner", {}))

    if not questions:
        raise ConfigError("questions must contain at least one item.")
    if not keywords:
        raise ConfigError("target_keywords must contain at least one item.")
    if not platforms:
        raise ConfigError(f"{run_mode} mode must contain at least one enabled platform.")

    return MonitorConfig(
        questions=questions,
        target_keywords=keywords,
        ai_platforms=platforms,
        run_mode=run_mode,
        browser_platforms=browser_platforms,
        api_platforms=api_platforms,
        schedule=schedule,
        output_dir=output_dir,
        runner=runner,
    )


def _browser_platform_source(data: dict[str, Any]) -> list[Any]:
    if "browser_platforms" in data:
        return data.get("browser_platforms")
    legacy = [item for item in data.get("ai_platforms", []) if isinstance(item, dict) and item.get("method", "browser") == "browser"]
    return legacy or browser_platform_defaults()


def _api_platform_source(data: dict[str, Any]) -> list[Any]:
    if "api_platforms" in data:
        return data.get("api_platforms")
    return [item for item in data.get("ai_platforms", []) if isinstance(item, dict) and item.get("method") == "api"]


def _infer_legacy_run_mode(data: dict[str, Any]) -> str:
    if "browser_platforms" in data or "api_platforms" in data:
        return "browser"
    legacy = data.get("ai_platforms")
    if isinstance(legacy, list) and legacy and all(isinstance(item, dict) and item.get("method") == "api" for item in legacy):
        return "api"
    return "browser"


def _parse_questions(value: Any) -> list[Question]:
    if not isinstance(value, list):
        raise ConfigError("questions must be a list.")
    seen: set[str] = set()
    questions: list[Question] = []
    for item in value:
        if not isinstance(item, dict):
            raise ConfigError("Each question must be an object.")
        question_id = _required_str(item, "question_id")
        question = _required_str(item, "question")
        if question_id in seen:
            raise ConfigError(f"Duplicate question_id: {question_id}")
        seen.add(question_id)
        questions.append(Question(question_id=question_id, question=question))
    return questions


def _parse_keywords(value: Any) -> list[TargetKeyword]:
    if not isinstance(value, list):
        raise ConfigError("target_keywords must be a list.")
    keywords: list[TargetKeyword] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, str):
            keyword = item.strip()
            aliases: tuple[str, ...] = ()
        elif isinstance(item, dict):
            keyword = _required_str(item, "keyword").strip()
            raw_aliases = item.get("aliases", [])
            if not isinstance(raw_aliases, list) or not all(isinstance(a, str) for a in raw_aliases):
                raise ConfigError("keyword aliases must be a list of strings.")
            aliases = tuple(a.strip() for a in raw_aliases if a.strip())
        else:
            raise ConfigError("Each target keyword must be a string or object.")
        if not keyword:
            raise ConfigError("Keyword cannot be empty.")
        key = keyword.casefold()
        if key in seen:
            raise ConfigError(f"Duplicate keyword: {keyword}")
        seen.add(key)
        keywords.append(TargetKeyword(keyword=keyword, aliases=aliases))
    return keywords


def _parse_platforms(value: Any, forced_method: str | None = None) -> list[AIPlatform]:
    if not isinstance(value, list):
        raise ConfigError("platforms must be a list.")
    platforms: list[AIPlatform] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ConfigError("Each AI platform must be an object.")
        if forced_method == "browser":
            item = merge_browser_template(item)
        platform_id = _required_str(item, "platform_id")
        if platform_id in seen:
            raise ConfigError(f"Duplicate platform_id: {platform_id}")
        seen.add(platform_id)
        method = forced_method or item.get("method", "browser")
        if method not in {"browser", "api"}:
            raise ConfigError(f"Unsupported platform method for {platform_id}: {method}")
        selectors = _parse_selectors(item.get("selectors", {}))
        enabled = bool(item.get("enabled", True))
        url = item.get("url")
        if method == "browser":
            if not isinstance(url, str) or not url.strip():
                raise ConfigError(f"Browser platform {platform_id} requires url.")
            model = None
        else:
            model = _required_str(item, "model")
            if url is not None and not isinstance(url, str):
                raise ConfigError(f"Platform url for {platform_id} must be a string when provided.")
        platforms.append(
            AIPlatform(
                platform_id=platform_id,
                platform_name=_required_str(item, "platform_name"),
                url=url.strip() if isinstance(url, str) and url.strip() else None,
                method=method,
                enabled=enabled,
                new_chat_url=_optional_str(item, "new_chat_url"),
                selectors=selectors,
                model=model,
                api_base_url=_optional_str(item, "api_base_url"),
                web_search=bool(item.get("web_search", True)),
                web_search_vendor=_optional_str(item, "web_search_vendor"),
                citation_triggers=_parse_citation_triggers(item.get("citation_triggers")),
            )
        )
    return platforms


def _parse_citation_triggers(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raw = value.splitlines()
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        raw = []
        for item in value:
            raw.extend(item.splitlines())
    else:
        raise ConfigError("citation_triggers must be a string or list of strings.")
    seen: set[str] = set()
    triggers: list[str] = []
    for item in raw:
        trigger = item.strip()
        key = trigger.casefold()
        if trigger and key not in seen:
            seen.add(key)
            triggers.append(trigger)
    return tuple(triggers)


def _parse_selectors(value: Any) -> PlatformSelectors:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ConfigError("selectors must be an object.")
    allowed = PlatformSelectors.__dataclass_fields__.keys()
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise ConfigError(f"Unknown selector fields: {', '.join(unknown)}")
    return PlatformSelectors(**{k: v for k, v in value.items() if v is not None})


def _parse_runner(value: Any) -> RunnerConfig:
    if not isinstance(value, dict):
        raise ConfigError("runner must be an object.")
    runner = RunnerConfig(**{k: v for k, v in value.items() if k in RunnerConfig.__dataclass_fields__})
    if runner.timeout_seconds <= 0:
        raise ConfigError("runner.timeout_seconds must be positive.")
    if runner.min_delay_seconds < 0 or runner.max_delay_seconds < runner.min_delay_seconds:
        raise ConfigError("runner delay range is invalid.")
    if runner.pause_on_blocked_seconds < 0:
        raise ConfigError("runner.pause_on_blocked_seconds cannot be negative.")
    if runner.profile_lock_wait_seconds < 0:
        raise ConfigError("runner.profile_lock_wait_seconds cannot be negative.")
    if runner.browser_concurrency <= 0:
        raise ConfigError("runner.browser_concurrency must be positive.")
    if runner.api_concurrency <= 0:
        raise ConfigError("runner.api_concurrency must be positive.")
    if runner.question_count <= 0:
        raise ConfigError("runner.question_count must be positive.")
    if runner.question_count > 50:
        raise ConfigError("runner.question_count cannot exceed 50.")
    return runner


def _parse_schedule(value: Any) -> ScheduleConfig | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ConfigError("schedule must be an object.")
    schedule_type = _required_str(value, "type")
    if schedule_type == "interval":
        seconds = value.get("every_seconds")
        if not isinstance(seconds, int) or seconds <= 0:
            raise ConfigError("interval schedule requires positive every_seconds.")
        return ScheduleConfig(type="interval", every_seconds=seconds)
    if schedule_type == "daily":
        return ScheduleConfig(type="daily", time=_required_str(value, "time"))
    if schedule_type == "weekly":
        days = value.get("days")
        if not isinstance(days, list) or not all(isinstance(d, str) for d in days) or not days:
            raise ConfigError("weekly schedule requires non-empty days.")
        return ScheduleConfig(type="weekly", time=_required_str(value, "time"), days=tuple(days))
    if schedule_type == "cron":
        return ScheduleConfig(type="cron", cron=_required_str(value, "cron"))
    raise ConfigError(f"Unsupported schedule type: {schedule_type}")


def _required_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"Missing required string field: {key}")
    return value.strip()


def _optional_str(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"Field must be a non-empty string when provided: {key}")
    return value.strip()
