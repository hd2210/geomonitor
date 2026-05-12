from __future__ import annotations

import csv
import json
import mimetypes
import os
import subprocess
import sys
import threading
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .answer_storage import read_raw_answers
from .config_loader import ConfigError, parse_config
from .env_loader import load_env_file
from .llm_client import AstraFlowLLMClient
from .monitor_service import MonitorJobManager
from .platform_templates import browser_platform_defaults
from .user_store import UserStore


WEB_DIR = Path(__file__).resolve().parent / "web"
SESSION_COOKIE = "geomonitor_session"
ADMIN_COOKIE = "geomonitor_admin"


class RunJobState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.return_code: int | None = None
        self.lines: list[str] = []

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "running": self.running,
                "return_code": self.return_code,
                "lines": self.lines[-300:],
            }

    def start(self, config_path: Path) -> None:
        with self.lock:
            if self.running:
                raise ValueError("A monitoring run is already in progress.")
            self.running = True
            self.return_code = None
            self.lines = ["Starting monitoring run..."]
        thread = threading.Thread(target=self._worker, args=(config_path,), daemon=True)
        thread.start()

    def _worker(self, config_path: Path) -> None:
        command = [sys.executable, "-m", "geomonitor.cli", "run", "--config", str(config_path)]
        process = subprocess.Popen(
            command,
            cwd=Path.cwd(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            with self.lock:
                self.lines.append(line.rstrip())
        return_code = process.wait()
        with self.lock:
            self.return_code = return_code
            self.running = False
            self.lines.append(f"Run finished with exit code {return_code}.")


class LoginJobState(RunJobState):
    def start(self, config_path: Path, platform_id: str) -> None:  # type: ignore[override]
        with self.lock:
            if self.running:
                raise ValueError("A login preparation browser is already open.")
            self.running = True
            self.return_code = None
            self.lines = [f"Opening login browser for {platform_id}..."]
        thread = threading.Thread(target=self._worker, args=(config_path, platform_id), daemon=True)
        thread.start()

    def _worker(self, config_path: Path, platform_id: str) -> None:  # type: ignore[override]
        command = [sys.executable, "-m", "geomonitor.cli", "login", "--config", str(config_path), "--platform-id", platform_id]
        process = subprocess.Popen(
            command,
            cwd=Path.cwd(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            with self.lock:
                self.lines.append(line.rstrip())
        return_code = process.wait()
        with self.lock:
            self.return_code = return_code
            self.running = False
            self.lines.append(f"Login preparation finished with exit code {return_code}.")


def serve_dashboard(host: str, port: int, runs_dir: str | Path, config_path: str | Path) -> None:
    load_env_file()
    store = UserStore()
    monitor_jobs = MonitorJobManager(store, Path(config_path))
    run_state = RunJobState()
    login_state = LoginJobState()
    server = ThreadingHTTPServer(
        (host, port),
        lambda *args: DashboardHandler(
            *args,
            runs_dir=Path(runs_dir),
            config_path=Path(config_path),
            run_state=run_state,
            login_state=login_state,
            store=store,
            monitor_jobs=monitor_jobs,
        ),
    )
    print(f"Dashboard running at http://{host}:{port}")
    print(f"Reading runs from {Path(runs_dir).resolve()}")
    print(f"Editing config at {Path(config_path).resolve()}")
    server.serve_forever()


class DashboardHandler(BaseHTTPRequestHandler):
    def __init__(
        self,
        *args,
        runs_dir: Path,
        config_path: Path,
        run_state: RunJobState,
        login_state: LoginJobState,
        store: UserStore,
        monitor_jobs: MonitorJobManager,
    ) -> None:
        self.runs_dir = runs_dir
        self.config_path = config_path
        self.run_state = run_state
        self.login_state = login_state
        self.store = store
        self.monitor_jobs = monitor_jobs
        super().__init__(*args)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            if path == "/api/auth/me":
                self._json({"user": self._current_user()})
                return
            if path == "/api/admin/me":
                self._json({"authenticated": self._is_admin()})
                return
            if path == "/api/user/platforms":
                self._json(self._user_platforms())
                return
            if path == "/api/monitors":
                user = self._require_user()
                self._json(self.store.list_monitors(int(user["id"])))
                return
            if path == "/api/monitor":
                user = self._require_user()
                monitor_id = int(_single_query_value(parsed.query, "id"))
                self._json(self._monitor_payload(monitor_id, int(user["id"])))
                return
            if path == "/api/admin/config":
                self._require_admin()
                self._json(self._config_payload())
                return
            if path == "/api/runs":
                self._require_admin()
                self._json(self._list_runs())
                return
            if path == "/api/run":
                self._require_admin()
                run_id = _single_query_value(parsed.query, "id")
                self._json(self._run_payload(run_id))
                return
            if path == "/api/config":
                self._require_admin()
                self._json(self._config_payload())
                return
            if path == "/api/run-status":
                self._require_admin()
                self._json(self.run_state.snapshot())
                return
            if path == "/api/login-status":
                self._require_admin()
                self._json(self.login_state.snapshot())
                return
            if path.startswith("/runs/"):
                self._serve_run_asset(path)
                return
            self._serve_static(path)
        except FileNotFoundError:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
        except PermissionError as exc:
            self._json_error(HTTPStatus.UNAUTHORIZED, str(exc))
        except ValueError as exc:
            self._json_error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:  # noqa: BLE001
            self._json_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            if path == "/api/auth/send-code":
                payload = self._read_json_body()
                phone = str(payload.get("phone", "")).strip()
                company_name = str(payload.get("company_name", "")).strip()
                if not phone:
                    raise ValueError("手机号不能为空。")
                self.store.save_code(phone, company_name, "123456")
                self._json({"status": "sent", "message": "本地验证码为 123456"})
                return
            if path == "/api/auth/login":
                payload = self._read_json_body()
                token, user = self.store.login(
                    str(payload.get("phone", "")).strip(),
                    str(payload.get("company_name", "")).strip(),
                    str(payload.get("code", "")).strip(),
                )
                self._json({"status": "success", "user": user}, cookies=[_cookie(SESSION_COOKIE, token)])
                return
            if path == "/api/auth/logout":
                token = self._cookies().get(SESSION_COOKIE)
                if token:
                    self.store.delete_session(token)
                self._json({"status": "success"}, cookies=[_cookie(SESSION_COOKIE, "", max_age=0)])
                return
            if path == "/api/admin/login":
                payload = self._read_json_body()
                password = str(payload.get("password", ""))
                if password != os.environ.get("ADMIN_PASSWORD", "yunzhigeo"):
                    raise ValueError("管理密码不正确。")
                self._json({"status": "success"}, cookies=[_cookie(ADMIN_COOKIE, "1")])
                return
            if path == "/api/admin/logout":
                self._json({"status": "success"}, cookies=[_cookie(ADMIN_COOKIE, "", max_age=0)])
                return
            if path == "/api/admin/config":
                self._require_admin()
                payload = self._read_json_body()
                self._save_config(payload)
                self._json({"status": "success", "config_path": str(self.config_path)})
                return
            if path == "/api/monitor/generate-questions":
                user = self._require_user()
                payload = self._read_json_body()
                brand_name = _limited_text(payload.get("brand_name"), "目标品牌名", 20)
                intention = _limited_text(payload.get("intention"), "消费者意图", 50)
                if self.store.monitor_count(int(user["id"])) >= 3:
                    raise ValueError("每个手机号最多可创建 3 次监测。")
                questions = AstraFlowLLMClient().generate_questions(brand_name, intention)
                self._json(
                    {
                        "questions": [
                            {"question_id": f"Q{index + 1:03d}", "question": question}
                            for index, question in enumerate(questions)
                        ],
                        "platforms": self._user_platforms()["platforms"],
                        "remaining_quota": max(3 - self.store.monitor_count(int(user["id"])), 0),
                    }
                )
                return
            if path == "/api/monitor/start":
                user = self._require_user()
                payload = self._read_json_body()
                brand_name = _limited_text(payload.get("brand_name"), "目标品牌名", 20)
                intention = _limited_text(payload.get("intention"), "消费者意图", 50)
                questions = _validate_questions(payload.get("questions"))
                selected_platforms = _validate_selected_platforms(payload.get("selected_platforms"), self._user_platforms()["platforms"])
                monitor = self.store.create_monitor(int(user["id"]), brand_name, intention, selected_platforms, questions)
                self.monitor_jobs.start(int(monitor["id"]))
                self._json({"status": "started", "monitor": monitor})
                return
            if path == "/api/monitor/retry":
                user = self._require_user()
                payload = self._read_json_body()
                monitor_id = int(payload.get("monitor_id") or 0)
                if monitor_id <= 0:
                    raise ValueError("monitor_id is required.")
                monitor = self.store.get_monitor(monitor_id, user_id=int(user["id"]))
                if not monitor:
                    raise FileNotFoundError("monitor not found")
                self.monitor_jobs.retry_failed(monitor_id)
                self._json({"status": "started", "monitor": monitor})
                return
            if path == "/api/config":
                self._require_admin()
                payload = self._read_json_body()
                self._save_config(payload)
                self._json({"status": "success", "config_path": str(self.config_path)})
                return
            if path == "/api/run-now":
                self._require_admin()
                parse_config(_read_json_file(self.config_path))
                self.run_state.start(self.config_path)
                self._json({"status": "started"})
                return
            if path == "/api/login-prepare":
                self._require_admin()
                payload = self._read_json_body()
                platform_id = str(payload.get("platform_id", "")).strip()
                if not platform_id:
                    raise ValueError("platform_id is required.")
                parse_config(_read_json_file(self.config_path))
                self.login_state.start(self.config_path, platform_id)
                self._json({"status": "started"})
                return
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
        except FileNotFoundError:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
        except PermissionError as exc:
            self._json_error(HTTPStatus.UNAUTHORIZED, str(exc))
        except (ValueError, ConfigError) as exc:
            self._json_error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:  # noqa: BLE001
            self._json_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_HEAD(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            static_path = WEB_DIR / ("index.html" if path in {"/", ""} else path.lstrip("/"))
            if not static_path.exists():
                raise FileNotFoundError
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mimetypes.guess_type(static_path.name)[0] or "application/octet-stream")
            self.end_headers()
        except FileNotFoundError:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return

    def _cookies(self) -> dict[str, str]:
        raw = self.headers.get("Cookie", "")
        cookies: dict[str, str] = {}
        for part in raw.split(";"):
            if "=" in part:
                key, value = part.split("=", 1)
                cookies[key.strip()] = value.strip()
        return cookies

    def _current_user(self) -> dict | None:
        return self.store.user_for_token(self._cookies().get(SESSION_COOKIE))

    def _require_user(self) -> dict:
        user = self._current_user()
        if not user:
            raise PermissionError("请先登录。")
        return user

    def _is_admin(self) -> bool:
        return self._cookies().get(ADMIN_COOKIE) == "1"

    def _require_admin(self) -> None:
        if not self._is_admin():
            raise PermissionError("需要管理密码。")

    def _user_platforms(self) -> dict:
        config = _read_json_file(self.config_path)
        parsed = parse_config(config)
        platforms = parsed.ai_platforms
        return {
            "run_mode": parsed.run_mode,
            "platforms": [
                {
                    "platform_id": platform.platform_id,
                    "platform_name": platform.platform_name,
                    "method": platform.method,
                }
                for platform in platforms
            ],
        }

    def _monitor_payload(self, monitor_id: int, user_id: int) -> dict:
        monitor = self.store.get_monitor(monitor_id, user_id=user_id)
        if not monitor:
            raise FileNotFoundError("monitor not found")
        payload = {"monitor": monitor, "run": None}
        if monitor.get("run_id"):
            try:
                payload["run"] = self._run_payload(str(monitor["run_id"]))
            except Exception:
                payload["run"] = None
        return payload

    def _list_runs(self) -> list[dict]:
        if not self.runs_dir.exists():
            return []
        runs: list[dict] = []
        for run_dir in sorted((p for p in self.runs_dir.iterdir() if p.is_dir()), reverse=True):
            raw_path = run_dir / "raw_answers.jsonl"
            if not raw_path.exists():
                continue
            answers = read_raw_answers(raw_path)
            success_count = sum(1 for answer in answers if answer.status in {"success", "partial_success"})
            failed_count = len(answers) - success_count
            platforms = sorted({answer.platform_id for answer in answers})
            questions = sorted({answer.question_id for answer in answers})
            runs.append(
                {
                    "run_id": run_dir.name,
                    "answer_count": len(answers),
                    "success_count": success_count,
                    "failed_count": failed_count,
                    "platforms": platforms,
                    "question_count": len(questions),
                }
            )
        return runs

    def _run_payload(self, run_id: str) -> dict:
        run_dir = _safe_child(self.runs_dir, run_id)
        answers = [asdict(answer) for answer in read_raw_answers(run_dir / "raw_answers.jsonl")]
        analyses = _read_jsonl(run_dir / "keyword_analysis.jsonl")
        platform_summary = _read_csv(run_dir / "platform_summary.csv")
        global_summary = _read_csv(run_dir / "global_summary.csv")
        report = (run_dir / "report.md").read_text(encoding="utf-8") if (run_dir / "report.md").exists() else ""
        return {
            "run_id": run_id,
            "answers": answers,
            "analyses": analyses,
            "platform_summary": platform_summary,
            "global_summary": global_summary,
            "report": report,
        }

    def _config_payload(self) -> dict:
        config = _read_json_file(self.config_path)
        return {
            "config_path": str(self.config_path),
            "run_mode": config.get("run_mode", "browser"),
            "questions": config.get("questions", []),
            "target_keywords": _normalize_keywords(config.get("target_keywords", [])),
            "browser_platforms": config.get("browser_platforms") or browser_platform_defaults(),
            "api_platforms": config.get("api_platforms") or [p for p in config.get("ai_platforms", []) if isinstance(p, dict) and p.get("method") == "api"],
            "schedule": config.get("schedule"),
            "runner": config.get("runner", {}),
        }

    def _save_config(self, payload: dict) -> None:
        if not isinstance(payload, dict):
            raise ValueError("Config payload must be an object.")
        questions = _validate_questions(payload.get("questions"))
        keywords = _validate_keywords(payload.get("target_keywords"))
        run_mode = str(payload.get("run_mode", "browser")).strip()
        if run_mode not in {"browser", "api"}:
            raise ValueError("run_mode must be browser or api.")
        browser_platforms = _validate_browser_platforms(payload.get("browser_platforms"))
        api_platforms = _validate_api_platforms(payload.get("api_platforms"))
        runner = _validate_runner(payload.get("runner", {}))
        config = _read_json_file(self.config_path)
        config["run_mode"] = run_mode
        config["questions"] = questions
        config["target_keywords"] = keywords
        config["browser_platforms"] = browser_platforms
        config["api_platforms"] = api_platforms
        config["runner"] = {**config.get("runner", {}), **runner}
        config.pop("ai_platforms", None)
        parse_config(config)
        self.config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            raise ValueError("Request body is empty.")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _serve_static(self, path: str) -> None:
        static_path = WEB_DIR / ("index.html" if path in {"/", "", "/admin"} else path.lstrip("/"))
        static_path = static_path.resolve()
        if WEB_DIR.resolve() not in static_path.parents and static_path != WEB_DIR.resolve():
            raise FileNotFoundError
        self._send_file(static_path)

    def _serve_run_asset(self, path: str) -> None:
        relative = path.removeprefix("/runs/")
        asset_path = _safe_child(self.runs_dir, relative)
        self._send_file(asset_path)

    def _send_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            raise FileNotFoundError
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        payload = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, payload, cookies: list[str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for cookie in cookies or []:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _json_error(self, status: HTTPStatus, message: str) -> None:
        body = json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _single_query_value(query: str, name: str) -> str:
    values = parse_qs(query).get(name)
    if not values or not values[0]:
        raise ValueError(f"Missing query parameter: {name}")
    return values[0]


def _cookie(name: str, value: str, max_age: int | None = None) -> str:
    parts = [f"{name}={value}", "Path=/", "SameSite=Lax"]
    if max_age is not None:
        parts.append(f"Max-Age={max_age}")
    return "; ".join(parts)


def _limited_text(value, label: str, max_length: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label}不能为空。")
    if len(text) > max_length:
        raise ValueError(f"{label}不能超过 {max_length} 个字。")
    return text


def _validate_selected_platforms(value, available: list[dict]) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("请选择至少一个 AI 平台。")
    allowed = {item["platform_id"] for item in available}
    selected: list[str] = []
    for item in value:
        platform_id = str(item).strip()
        if platform_id in allowed and platform_id not in selected:
            selected.append(platform_id)
    if not selected:
        raise ValueError("至少选择一个 AI 平台。")
    return selected


def _safe_child(parent: Path, relative: str) -> Path:
    parent = parent.resolve()
    child = (parent / relative).resolve()
    if parent != child and parent not in child.parents:
        raise ValueError("Invalid path")
    return child


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_json_file(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Config root must be an object.")
    return data


def _normalize_keywords(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    normalized: list[dict] = []
    for item in value:
        if isinstance(item, str):
            normalized.append({"keyword": item, "aliases": []})
        elif isinstance(item, dict):
            aliases = item.get("aliases", [])
            normalized.append(
                {
                    "keyword": item.get("keyword", ""),
                    "aliases": aliases if isinstance(aliases, list) else [],
                }
            )
    return normalized


def _validate_questions(value) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError("questions must be a list.")
    seen: set[str] = set()
    questions: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Each question must be an object.")
        question_id = str(item.get("question_id", "")).strip()
        question = str(item.get("question", "")).strip()
        if not question_id or not question:
            raise ValueError("Question ID and text are required.")
        if question_id in seen:
            raise ValueError(f"Duplicate question_id: {question_id}")
        seen.add(question_id)
        questions.append({"question_id": question_id, "question": question})
    if not questions:
        raise ValueError("At least one question is required.")
    return questions


def _validate_keywords(value) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError("target_keywords must be a list.")
    seen: set[str] = set()
    keywords: list[dict] = []
    for item in value:
        if isinstance(item, str):
            keyword = item.strip()
            aliases: list[str] = []
        elif isinstance(item, dict):
            keyword = str(item.get("keyword", "")).strip()
            raw_aliases = item.get("aliases", [])
            if not isinstance(raw_aliases, list):
                raise ValueError("Keyword aliases must be a list.")
            aliases = [str(alias).strip() for alias in raw_aliases if str(alias).strip()]
        else:
            raise ValueError("Each keyword must be a string or object.")
        if not keyword:
            raise ValueError("Keyword cannot be empty.")
        key = keyword.casefold()
        if key in seen:
            raise ValueError(f"Duplicate keyword: {keyword}")
        seen.add(key)
        keywords.append({"keyword": keyword, "aliases": aliases})
    if not keywords:
        raise ValueError("At least one keyword is required.")
    return keywords


def _validate_api_platforms(value) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError("ai_platforms must be a list.")
    seen: set[str] = set()
    platforms: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Each platform must be an object.")
        platform_id = str(item.get("platform_id", "")).strip()
        platform_name = str(item.get("platform_name", "")).strip()
        model = str(item.get("model", "")).strip()
        if not platform_id or not platform_name or not model:
            raise ValueError("Platform ID, name, and model are required.")
        if platform_id in seen:
            raise ValueError(f"Duplicate platform_id: {platform_id}")
        seen.add(platform_id)
        platform = {
            "platform_id": platform_id,
            "platform_name": platform_name,
            "method": "api",
            "enabled": bool(item.get("enabled", True)),
            "model": model,
            "web_search": bool(item.get("web_search", True)),
        }
        api_base_url = str(item.get("api_base_url", "")).strip()
        web_search_vendor = str(item.get("web_search_vendor", "")).strip()
        if api_base_url:
            platform["api_base_url"] = api_base_url
        if web_search_vendor:
            platform["web_search_vendor"] = web_search_vendor
        platforms.append(platform)
    return platforms


def _validate_browser_platforms(value) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError("browser_platforms must be a list.")
    seen: set[str] = set()
    platforms: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Each browser platform must be an object.")
        platform_id = str(item.get("platform_id", "")).strip()
        platform_name = str(item.get("platform_name", "")).strip()
        url = str(item.get("url", "")).strip()
        if not platform_id or not platform_name or not url:
            raise ValueError("Browser platform ID, name, and URL are required.")
        if platform_id in seen:
            raise ValueError(f"Duplicate platform_id: {platform_id}")
        seen.add(platform_id)
        platform: dict[str, object] = {
            "platform_id": platform_id,
            "platform_name": platform_name,
            "url": url,
            "method": "browser",
            "enabled": bool(item.get("enabled", False)),
        }
        new_chat_url = str(item.get("new_chat_url", "")).strip()
        if new_chat_url:
            platform["new_chat_url"] = new_chat_url
        selectors = item.get("selectors")
        if isinstance(selectors, dict):
            platform["selectors"] = {
                key: str(value).strip()
                for key, value in selectors.items()
                if value is not None and str(value).strip()
            }
        platforms.append(platform)
    return platforms


def _validate_runner(value) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("runner must be an object.")
    runner: dict[str, int] = {}
    for key, default in (("browser_concurrency", 2), ("api_concurrency", 5)):
        raw = value.get(key, default)
        try:
            number = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"runner.{key} must be a number.") from exc
        if number <= 0:
            raise ValueError(f"runner.{key} must be positive.")
        runner[key] = min(number, 20)
    return runner
