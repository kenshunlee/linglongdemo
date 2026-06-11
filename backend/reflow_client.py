from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class ReflowConfig:
    enabled: bool
    base_url: str
    login_name: str
    password: str
    timeout_s: float = 20.0
    verify_ssl: bool = True


class ReflowClient:
    """Minimal client for SaiXing reflow APIs with token refresh and idempotency headers."""

    def __init__(self, config: ReflowConfig) -> None:
        self.config = config
        self._client = httpx.Client(timeout=config.timeout_s, verify=config.verify_ssl)
        self._access_token = ""
        self._refresh_token = ""
        self._token_ts = 0.0

    def close(self) -> None:
        self._client.close()

    def health(self) -> dict[str, Any]:
        return {
            "enabled": self.config.enabled,
            "configured": bool(self.config.base_url and self.config.login_name and self.config.password),
            "base_url": self.config.base_url,
            "login_name": self.config.login_name,
            "logged_in": bool(self._access_token),
            "token_age_s": int(time.time() - self._token_ts) if self._token_ts else None,
        }

    def _full_url(self, path: str) -> str:
        return f"{self.config.base_url.rstrip('/')}/{path.lstrip('/')}"

    def _auth_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        return headers

    def _check_enabled(self) -> None:
        if not self.config.enabled:
            raise RuntimeError("REFLOW_DISABLED")
        if not (self.config.base_url and self.config.login_name and self.config.password):
            raise RuntimeError("REFLOW_NOT_CONFIGURED")

    def login(self, force: bool = False) -> dict[str, Any]:
        self._check_enabled()
        if self._access_token and not force:
            return {"ok": True, "cached": True}

        payload = {
            "login_name": self.config.login_name,
            "password": self.config.password,
        }
        resp = self._client.post(self._full_url("/api/v1/auth/login"), json=payload)
        resp.raise_for_status()
        data = resp.json()

        tokens = data.get("tokens") or {}
        access_token = str(tokens.get("access_token") or "").strip()
        refresh_token = str(tokens.get("refresh_token") or "").strip()
        if not access_token:
            raise RuntimeError(f"LOGIN_NO_ACCESS_TOKEN: {data}")

        self._access_token = access_token
        self._refresh_token = refresh_token
        self._token_ts = time.time()
        return {"ok": True, "cached": False}

    def refresh(self) -> dict[str, Any]:
        self._check_enabled()
        if not self._refresh_token:
            return self.login(force=True)

        payload = {"refresh_token": self._refresh_token}
        resp = self._client.post(self._full_url("/api/v1/auth/refresh"), json=payload)
        if resp.status_code >= 400:
            return self.login(force=True)

        data = resp.json()
        access_token = str(data.get("access_token") or "").strip()
        refresh_token = str(data.get("refresh_token") or "").strip()
        if not access_token:
            return self.login(force=True)

        self._access_token = access_token
        self._refresh_token = refresh_token or self._refresh_token
        self._token_ts = time.time()
        return {"ok": True}

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        allow_retry_auth: bool = True,
    ) -> dict[str, Any]:
        self._check_enabled()
        if not self._access_token:
            self.login()

        headers = self._auth_headers()
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        resp = self._client.request(method, self._full_url(path), json=payload, headers=headers)
        if resp.status_code == 401 and allow_retry_auth:
            self.refresh()
            return self.request(
                method=method,
                path=path,
                payload=payload,
                idempotency_key=idempotency_key,
                allow_retry_auth=False,
            )

        resp.raise_for_status()
        data = resp.json()

        # Reflow endpoints use wrapper {code, message, data} where code==0 means success.
        code = data.get("code")
        if isinstance(code, int) and code != 0:
            raise RuntimeError(f"REFLOW_BUSINESS_ERROR[{code}]: {data.get('message')}")
        return data

    def me(self) -> dict[str, Any]:
        return self.request("GET", "/api/v1/auth/me")

    def create_session(self, payload: dict[str, Any], idempotency_key: str | None = None) -> dict[str, Any]:
        return self.request("POST", "/api/v1/reflow/sessions", payload=payload, idempotency_key=idempotency_key)

    def update_session(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("PATCH", f"/api/v1/reflow/sessions/{session_id}", payload=payload)

    def session_status(self, session_id: str) -> dict[str, Any]:
        return self.request("GET", f"/api/v1/reflow/sessions/{session_id}/status")

    def report_task(self, payload: dict[str, Any], idempotency_key: str | None = None) -> dict[str, Any]:
        return self.request("POST", "/api/v1/reflow/tasks/report", payload=payload, idempotency_key=idempotency_key)

    def batch_trajectory(self, payload: dict[str, Any], idempotency_key: str | None = None) -> dict[str, Any]:
        return self.request("POST", "/api/v1/reflow/trajectories/batch", payload=payload, idempotency_key=idempotency_key)

    def batch_embodied(self, payload: dict[str, Any], idempotency_key: str | None = None) -> dict[str, Any]:
        return self.request("POST", "/api/v1/reflow/embodied/batch", payload=payload, idempotency_key=idempotency_key)

    def batch_events(self, payload: dict[str, Any], idempotency_key: str | None = None) -> dict[str, Any]:
        return self.request("POST", "/api/v1/reflow/events/batch", payload=payload, idempotency_key=idempotency_key)
