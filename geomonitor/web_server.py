from __future__ import annotations

import csv
import json
import mimetypes
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


WEB_DIR = Path(__file__).resolve().parent / "web"


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


def serve_dashboard(host: str, port: int, runs_dir: str | Path, config_path: str | Path) -> None:
    run_state = RunJobState()
    server = ThreadingHTTPServer(
        (host, port),
        lambda *args: DashboardHandler(
            *args,
            runs_dir=Path(runs_dir),
            config_path=Path(config_path),
            run_state=run_state,
        ),
    )
    print(f"Dashboard running at http://{host}:{port}")
    print(f"Reading runs from {Path(runs_dir).resolve()}")
    print(f"Editing config at {Path(config_path).resolve()}")
    server.serve_forever()


class DashboardHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, runs_dir: Path, config_path: Path, run_state: RunJobState) -> None:
        self.runs_dir = runs_dir
        self.config_path = config_path
        self.run_state = run_state
        super().__init__(*args)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            if path == "/api/runs":
                self._json(self._list_runs())
                return
            if path == "/api/run":
                run_id = _single_query_value(parsed.query, "id")
                self._json(self._run_payload(run_id))
                return
            if path == "/api/config":
                self._json(self._config_payload())
                return
            if path == "/api/run-status":
                self._json(self.run_state.snapshot())
                return
            if path.startswith("/runs/"):
                self._serve_run_asset(path)
                return
            self._serve_static(path)
        except FileNotFoundError:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
        except ValueError as exc:
            self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:  # noqa: BLE001
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            if path == "/api/config":
                payload = self._read_json_body()
                self._save_config(payload)
                self._json({"status": "success", "config_path": str(self.config_path)})
                return
            if path == "/api/run-now":
                parse_config(_read_json_file(self.config_path))
                self.run_state.start(self.config_path)
                self._json({"status": "started"})
                return
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
        except FileNotFoundError:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
        except (ValueError, ConfigError) as exc:
            self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:  # noqa: BLE001
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

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
            "questions": config.get("questions", []),
            "target_keywords": _normalize_keywords(config.get("target_keywords", [])),
            "ai_platforms": config.get("ai_platforms", []),
            "schedule": config.get("schedule"),
            "runner": config.get("runner", {}),
        }

    def _save_config(self, payload: dict) -> None:
        if not isinstance(payload, dict):
            raise ValueError("Config payload must be an object.")
        questions = _validate_questions(payload.get("questions"))
        keywords = _validate_keywords(payload.get("target_keywords"))
        platforms = _validate_platforms(payload.get("ai_platforms"))
        config = _read_json_file(self.config_path)
        config["questions"] = questions
        config["target_keywords"] = keywords
        config["ai_platforms"] = platforms
        parse_config(config)
        self.config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            raise ValueError("Request body is empty.")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _serve_static(self, path: str) -> None:
        static_path = WEB_DIR / ("index.html" if path in {"/", ""} else path.lstrip("/"))
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

    def _json(self, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _single_query_value(query: str, name: str) -> str:
    values = parse_qs(query).get(name)
    if not values or not values[0]:
        raise ValueError(f"Missing query parameter: {name}")
    return values[0]


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


def _validate_platforms(value) -> list[dict]:
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
    if not platforms:
        raise ValueError("At least one model/platform is required.")
    return platforms
