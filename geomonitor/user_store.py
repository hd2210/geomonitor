from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class UserStore:
    def __init__(self, path: str | Path = "./data/app.sqlite3") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                create table if not exists users (
                  id integer primary key autoincrement,
                  phone text not null unique,
                  company_name text not null,
                  quota_total integer not null default 3,
                  created_at text not null,
                  last_login_at text not null
                );

                create table if not exists sessions (
                  token text primary key,
                  user_id integer not null,
                  created_at text not null,
                  foreign key(user_id) references users(id)
                );

                create table if not exists sms_codes (
                  phone text primary key,
                  company_name text,
                  code text not null,
                  created_at text not null
                );

                create table if not exists monitors (
                  id integer primary key autoincrement,
                  user_id integer not null,
                  run_id text unique,
                  brand_name text not null,
                  intention text not null,
                  status text not null,
                  created_at text not null,
                  completed_at text,
                  selected_platforms text not null,
                  questions text not null,
                  keywords text not null default '[]',
                  competitor_payload text,
                  progress_current integer not null default 0,
                  progress_total integer not null default 0,
                  progress_message text,
                  run_dir text,
                  error_message text,
                  notification_message text,
                  foreign key(user_id) references users(id)
                );

                create table if not exists browser_account_status (
                  platform_id text not null,
                  account_id text not null,
                  account_name text,
                  status text not null,
                  run_id text,
                  question_id text,
                  error_message text,
                  last_used_at text not null,
                  last_success_at text,
                  blocked_at text,
                  primary key(platform_id, account_id)
                );
                """
            )
            self._ensure_column(db, "users", "quota_total", "integer not null default 3")

    @staticmethod
    def _ensure_column(db: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in db.execute(f"pragma table_info({table})").fetchall()}
        if column not in columns:
            db.execute(f"alter table {table} add column {column} {definition}")

    def save_code(self, phone: str, company_name: str | None, code: str = "123456") -> None:
        with self._connect() as db:
            db.execute(
                """
                insert into sms_codes(phone, company_name, code, created_at)
                values(?, ?, ?, ?)
                on conflict(phone) do update set
                  company_name=excluded.company_name,
                  code=excluded.code,
                  created_at=excluded.created_at
                """,
                (phone, company_name or "", code, now_iso()),
            )

    def login(self, phone: str, company_name: str | None, code: str) -> tuple[str, dict[str, Any]]:
        with self._connect() as db:
            row = db.execute("select * from sms_codes where phone = ?", (phone,)).fetchone()
            if row is None or row["code"] != code:
                raise ValueError("验证码不正确。")
            user = db.execute("select * from users where phone = ?", (phone,)).fetchone()
            current = now_iso()
            final_company = (company_name or row["company_name"] or "").strip()
            if user is None:
                if not final_company:
                    raise ValueError("首次登录需要填写公司名称。")
                cursor = db.execute(
                    "insert into users(phone, company_name, created_at, last_login_at) values(?, ?, ?, ?)",
                    (phone, final_company, current, current),
                )
                user_id = int(cursor.lastrowid)
            else:
                user_id = int(user["id"])
                if final_company and final_company != user["company_name"]:
                    db.execute("update users set company_name = ? where id = ?", (final_company, user_id))
                db.execute("update users set last_login_at = ? where id = ?", (current, user_id))
            token = secrets.token_urlsafe(32)
            db.execute("insert into sessions(token, user_id, created_at) values(?, ?, ?)", (token, user_id, current))
        return token, self.get_user_by_id(user_id) or {}

    def user_for_token(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        with self._connect() as db:
            row = db.execute(
                """
                select users.* from sessions
                join users on users.id = sessions.user_id
                where sessions.token = ?
                """,
                (token,),
            ).fetchone()
            return _row_dict(row)

    def get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        with self._connect() as db:
            return _row_dict(db.execute("select * from users where id = ?", (user_id,)).fetchone())

    def delete_session(self, token: str) -> None:
        with self._connect() as db:
            db.execute("delete from sessions where token = ?", (token,))

    def monitor_count(self, user_id: int) -> int:
        with self._connect() as db:
            row = db.execute("select count(*) as count from monitors where user_id = ?", (user_id,)).fetchone()
            return int(row["count"] if row else 0)

    def quota_total(self, user_id: int) -> int:
        with self._connect() as db:
            row = db.execute("select quota_total from users where id = ?", (user_id,)).fetchone()
            return int(row["quota_total"] if row else 3)

    def remaining_quota(self, user_id: int) -> int:
        return max(self.quota_total(user_id) - self.monitor_count(user_id), 0)

    def update_user_quota(self, user_id: int, quota_total: int) -> None:
        if quota_total < 0:
            raise ValueError("可用监控总次数不能小于 0。")
        with self._connect() as db:
            cursor = db.execute("update users set quota_total = ? where id = ?", (quota_total, user_id))
            if cursor.rowcount == 0:
                raise ValueError("用户不存在。")

    def list_users_with_monitors(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            users = db.execute("select * from users order by id desc").fetchall()
            rows: list[dict[str, Any]] = []
            for user in users:
                data = _row_dict(user) or {}
                monitors = db.execute("select * from monitors where user_id = ? order by id desc", (data["id"],)).fetchall()
                monitor_rows = [_monitor_dict(row) for row in monitors]
                data["monitor_count"] = len(monitor_rows)
                data["remaining_quota"] = max(int(data.get("quota_total") or 3) - len(monitor_rows), 0)
                data["monitors"] = monitor_rows
                rows.append(data)
            return rows

    def create_monitor(
        self,
        user_id: int,
        brand_name: str,
        intention: str,
        selected_platforms: list[str],
        questions: list[dict[str, str]],
    ) -> dict[str, Any]:
        if self.remaining_quota(user_id) <= 0:
            raise ValueError("当前手机号可用监控次数不足。")
        current = now_iso()
        with self._connect() as db:
            cursor = db.execute(
                """
                insert into monitors(
                  user_id, brand_name, intention, status, created_at,
                  selected_platforms, questions, progress_message
                )
                values(?, ?, ?, 'queued', ?, ?, ?, '等待开始')
                """,
                (user_id, brand_name, intention, current, json.dumps(selected_platforms, ensure_ascii=False), json.dumps(questions, ensure_ascii=False)),
            )
            monitor_id = int(cursor.lastrowid)
        return self.get_monitor(monitor_id, user_id=user_id) or {}

    def update_monitor(self, monitor_id: int, **fields: Any) -> None:
        if not fields:
            return
        allowed = {
            "run_id",
            "status",
            "completed_at",
            "keywords",
            "competitor_payload",
            "progress_current",
            "progress_total",
            "progress_message",
            "run_dir",
            "error_message",
            "notification_message",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        columns = ", ".join(f"{key} = ?" for key in updates)
        values = [json.dumps(value, ensure_ascii=False) if key in {"keywords", "competitor_payload"} and not isinstance(value, str) else value for key, value in updates.items()]
        values.append(monitor_id)
        with self._connect() as db:
            db.execute(f"update monitors set {columns} where id = ?", values)

    def list_monitors(self, user_id: int) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("select * from monitors where user_id = ? order by id desc", (user_id,)).fetchall()
            return [_monitor_dict(row) for row in rows]

    def get_monitor(self, monitor_id: int, user_id: int | None = None) -> dict[str, Any] | None:
        with self._connect() as db:
            if user_id is None:
                row = db.execute("select * from monitors where id = ?", (monitor_id,)).fetchone()
            else:
                row = db.execute("select * from monitors where id = ? and user_id = ?", (monitor_id, user_id)).fetchone()
            return _monitor_dict(row)

    def update_browser_account_status(
        self,
        platform_id: str,
        account_id: str,
        account_name: str | None,
        status: str,
        run_id: str,
        question_id: str,
        error_message: str | None,
    ) -> None:
        current = now_iso()
        blocked_at = current if status in {"blocked", "login_required"} else None
        with self._connect() as db:
            existing = db.execute(
                """
                select last_success_at, blocked_at from browser_account_status
                where platform_id = ? and account_id = ?
                """,
                (platform_id, account_id),
            ).fetchone()
            last_success_at = current if status in {"success", "partial_success"} else (existing["last_success_at"] if existing else None)
            final_blocked_at = blocked_at if blocked_at is not None else None
            db.execute(
                """
                insert into browser_account_status(
                  platform_id, account_id, account_name, status, run_id, question_id,
                  error_message, last_used_at, last_success_at, blocked_at
                )
                values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(platform_id, account_id) do update set
                  account_name=excluded.account_name,
                  status=excluded.status,
                  run_id=excluded.run_id,
                  question_id=excluded.question_id,
                  error_message=excluded.error_message,
                  last_used_at=excluded.last_used_at,
                  last_success_at=excluded.last_success_at,
                  blocked_at=excluded.blocked_at
                """,
                (
                    platform_id,
                    account_id,
                    account_name,
                    status,
                    run_id,
                    question_id,
                    error_message,
                    current,
                    last_success_at,
                    final_blocked_at,
                ),
            )

    def list_browser_account_statuses(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """
                select * from browser_account_status
                order by platform_id asc, account_id asc
                """
            ).fetchall()
            return [_row_dict(row) or {} for row in rows]

    def clear_browser_account_status(self, platform_id: str, account_id: str) -> None:
        with self._connect() as db:
            db.execute(
                """
                update browser_account_status
                set status = 'ready', error_message = null, blocked_at = null
                where platform_id = ? and account_id = ?
                """,
                (platform_id, account_id),
            )


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _monitor_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    for key, fallback in (("selected_platforms", []), ("questions", []), ("keywords", [])):
        try:
            data[key] = json.loads(data.get(key) or "null") or fallback
        except json.JSONDecodeError:
            data[key] = fallback
    try:
        data["competitor_payload"] = json.loads(data.get("competitor_payload") or "null")
    except json.JSONDecodeError:
        data["competitor_payload"] = None
    return data
