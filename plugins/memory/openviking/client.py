"""OpenViking REST client helpers."""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path

_TIMEOUT = 30.0


def _get_httpx():
    """Lazy import httpx."""
    try:
        import httpx
        return httpx
    except ImportError:
        return None


class _VikingClient:
    """Thin HTTP client for the OpenViking REST API."""

    def __init__(self, endpoint: str, api_key: str = "",
                 account: str = "", user: str = "", agent: str = ""):
        self._endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self._account = account or os.environ.get("OPENVIKING_ACCOUNT", "default")
        self._user = user or os.environ.get("OPENVIKING_USER", "default")
        self._agent = agent or os.environ.get("OPENVIKING_AGENT", "hermes")
        self._httpx = _get_httpx()
        if self._httpx is None:
            raise ImportError("httpx is required for OpenViking: pip install httpx")

    def _headers(self) -> dict:
        # Always send tenant headers when account/user are configured.
        # OpenViking 0.3.x requires X-OpenViking-Account and X-OpenViking-User
        # for ROOT API key requests to tenant-scoped APIs — omitting them
        # causes INVALID_ARGUMENT errors even when account="default".
        # User-level keys can omit them (server derives tenancy from the key),
        # but ROOT keys must always include them explicitly.
        h = {
            "Content-Type": "application/json",
            "X-OpenViking-Agent": self._agent,
        }
        if self._account:
            h["X-OpenViking-Account"] = self._account
        if self._user:
            h["X-OpenViking-User"] = self._user
        if self._api_key:
            h["X-API-Key"] = self._api_key
            h["Authorization"] = "Bearer " + self._api_key
        return h

    def _url(self, path: str) -> str:
        return f"{self._endpoint}{path}"

    def _multipart_headers(self) -> dict:
        headers = self._headers()
        headers.pop("Content-Type", None)
        return headers

    def _parse_response(self, resp) -> dict:
        try:
            data = resp.json()
        except Exception:
            data = None

        if resp.status_code >= 400:
            if isinstance(data, dict):
                error = data.get("error")
                if isinstance(error, dict):
                    code = error.get("code", "HTTP_ERROR")
                    message = error.get("message", resp.text)
                    raise RuntimeError(f"{code}: {message}")
                if data.get("status") == "error":
                    raise RuntimeError(str(data))
            resp.raise_for_status()

        if isinstance(data, dict) and data.get("status") == "error":
            error = data.get("error")
            if isinstance(error, dict):
                code = error.get("code", "OPENVIKING_ERROR")
                message = error.get("message", "")
                raise RuntimeError(f"{code}: {message}")
            raise RuntimeError(str(data))

        if data is None:
            return {}
        return data

    def get(self, path: str, **kwargs) -> dict:
        resp = self._httpx.get(
            self._url(path), headers=self._headers(), timeout=_TIMEOUT, **kwargs
        )
        return self._parse_response(resp)

    def post(self, path: str, payload: dict = None, **kwargs) -> dict:
        resp = self._httpx.post(
            self._url(path), json=payload or {}, headers=self._headers(),
            timeout=_TIMEOUT, **kwargs
        )
        return self._parse_response(resp)

    def put(self, path: str, payload: dict = None, **kwargs) -> dict:
        resp = self._httpx.put(
            self._url(path), json=payload or {}, headers=self._headers(),
            timeout=_TIMEOUT, **kwargs
        )
        return self._parse_response(resp)

    def delete(self, path: str, payload: dict = None, **kwargs) -> dict:
        resp = self._httpx.request(
            "DELETE", self._url(path), json=payload or {}, headers=self._headers(),
            timeout=_TIMEOUT, **kwargs
        )
        return self._parse_response(resp)

    def get_text(self, path: str, **kwargs) -> str:
        resp = self._httpx.get(
            self._url(path), headers=self._headers(), timeout=_TIMEOUT, **kwargs
        )
        if resp.status_code >= 400:
            self._parse_response(resp)
        return resp.text

    def upload_temp_file(self, file_path: Path) -> str:
        mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        with file_path.open("rb") as f:
            resp = self._httpx.post(
                self._url("/api/v1/resources/temp_upload"),
                files={"file": (file_path.name, f, mime_type)},
                headers=self._multipart_headers(),
                timeout=_TIMEOUT,
            )
        data = self._parse_response(resp)
        result = data.get("result", {})
        temp_file_id = result.get("temp_file_id", "")
        if not temp_file_id:
            raise RuntimeError("OpenViking temp upload did not return temp_file_id")
        return temp_file_id

    def health(self) -> bool:
        try:
            resp = self._httpx.get(
                self._url("/health"), headers=self._headers(), timeout=3.0
            )
            return resp.status_code == 200
        except Exception:
            return False
