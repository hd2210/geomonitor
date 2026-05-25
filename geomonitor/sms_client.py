from __future__ import annotations

import json
import os
import ssl
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_SMS_BASE_URL = "https://cloud.ts-martech.com/api/sms"
DEFAULT_NOTICE_TEMPLATE_ID = "UTN2605181K4Q0D"
DEFAULT_NOTICE_TEMPLATE_PARAM = "GEO监测页面"


class SMSClient:
    def __init__(self, base_url: str | None = None, timeout: int = 20) -> None:
        self.base_url = (base_url or os.environ.get("SMS_API_BASE_URL") or DEFAULT_SMS_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.notice_template_id = os.environ.get("SMS_NOTICE_TEMPLATE_ID", DEFAULT_NOTICE_TEMPLATE_ID)
        self.notice_template_param = os.environ.get("SMS_NOTICE_TEMPLATE_PARAM", DEFAULT_NOTICE_TEMPLATE_PARAM)
        self.ssl_context = _ssl_context()

    def send_code(self, mobile: str) -> dict[str, Any]:
        return self._post("/send", {"mobile": mobile})

    def verify_code(self, mobile: str, sms_code: str) -> dict[str, Any]:
        return self._post("/verify", {"mobile": mobile, "smsCode": sms_code})

    def send_notice(self, mobile: str, template_params: list[str] | None = None) -> dict[str, Any]:
        return self._post(
            "/notice",
            {
                "mobile": mobile,
                "templateId": self.notice_template_id,
                "templateParams": template_params or [self.notice_template_param],
            },
        )

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout, context=self.ssl_context) as response:
                response_body = response.read().decode("utf-8", errors="replace")
                if response.status != 200:
                    raise RuntimeError(f"短信接口返回 HTTP {response.status}: {response_body}")
                payload = _decode_json(response_body)
                code = payload.get("code")
                if code is not None and str(code) != "200":
                    raise RuntimeError(f"短信接口返回失败：{payload}")
                return payload
        except HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"短信接口返回 HTTP {exc.code}: {error_body}") from exc
        except URLError as exc:
            raise RuntimeError(f"短信接口请求失败：{exc.reason}") from exc


def _decode_json(value: str) -> dict[str, Any]:
    if not value.strip():
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {"raw": value}
    return payload if isinstance(payload, dict) else {"data": payload}


def _ssl_context() -> ssl.SSLContext:
    ca_bundle = os.environ.get("SMS_CA_BUNDLE")
    if ca_bundle:
        return ssl.create_default_context(cafile=ca_bundle)

    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()
