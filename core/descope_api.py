import os
import re
from typing import Any, Dict, List, Optional

import requests


class DescopeAPI:
    def __init__(self):
        self.project_id = os.environ.get("DESCOPE_PROJECT_ID")
        # Management key env var name provided by user context
        self.mgmt_key = os.environ.get("DESCOPE_MANAGEMENT_KEY") or os.environ.get("DESCOPE_MGMT_KEY")
        self.inbound_app_id = os.environ.get("DESCOPE_MCP_INBOUND_APP_ID")
        self.base = os.environ.get("DESCOPE_BASE_URL", "https://api.descope.com")

        if not self.project_id or not self.mgmt_key:
            raise ValueError("Missing DESCOPE_PROJECT_ID or DESCOPE_MANAGEMENT_KEY")

        self._bearer = f"{self.project_id}:{self.mgmt_key}"

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._bearer}",
            "Content-Type": "application/json",
        }

    def list_roles(self, timeout: int = 15) -> List[Dict[str, Any]]:
        r = requests.get(f"{self.base}/v1/mgmt/role/all", headers=self._headers(), timeout=timeout)
        r.raise_for_status()
        body = r.json() or {}
        return body.get("roles", [])

    def load_app(self, timeout: int = 15) -> Dict[str, Any]:
        if not self.inbound_app_id:
            raise ValueError("Missing DESCOPE_MCP_INBOUND_APP_ID")
        params = {"id": self.inbound_app_id}
        r = requests.get(
            f"{self.base}/v1/mgmt/thirdparty/app/load",
            headers=self._headers(),
            params=params,
            timeout=timeout,
        )
        r.raise_for_status()
        return r.json() or {}

    def patch_app_scopes(self, scopes: List[Dict[str, Any]], timeout: int = 15) -> Dict[str, Any]:
        if not self.inbound_app_id:
            raise ValueError("Missing DESCOPE_MCP_INBOUND_APP_ID")
        payload = {"id": self.inbound_app_id, "permissionsScopes": scopes}
        r = requests.post(f"{self.base}/v1/mgmt/thirdparty/app/patch", headers=self._headers(), json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def ensure_scopes(self, scopes_to_add: List[Dict[str, Any]], timeout: int = 15) -> Dict[str, Any]:
        """
        Load app, append/update provided scopes (by name), and patch.
        Each incoming scope uses role NAMES in values, not IDs.
        """
        if not self.inbound_app_id:
            raise ValueError("Missing DESCOPE_MCP_INBOUND_APP_ID")
        app = self.load_app(timeout=timeout)
        existing = list((app.get("permissionsScopes") or []))
        by_name: Dict[str, Dict[str, Any]] = { (s.get("name") or ""): s for s in existing if s.get("name") }
        for s in (scopes_to_add or []):
            name = s.get("name")
            if not name:
                continue
            if name in by_name:
                # Update existing with latest details
                by_name[name].update({
                    "description": s.get("description", by_name[name].get("description")),
                    "optional": s.get("optional", by_name[name].get("optional", False)),
                    "values": s.get("values", by_name[name].get("values", [])),
                })
            else:
                by_name[name] = {
                    "name": name,
                    "description": s.get("description") or "",
                    "optional": bool(s.get("optional", False)),
                    "values": list(s.get("values") or []),
                }
        merged = list(by_name.values())
        return self.patch_app_scopes(merged, timeout=timeout)

    # list_outbound_apps removed — use Descope SDK directly in endpoint layer

    def get_latest_outbound_token(
        self,
        app_id: str,
        user_id: str,
        tenant_id: Optional[str] = None,
        with_refresh: bool = True,
        force_refresh: bool = False,
        timeout: int = 15,
    ) -> Dict[str, Any]:
        """
        Fetch the latest outbound app token for a user via management API.
        Mirrors docs sample: POST /v1/mgmt/outbound/app/user/token/latest
        """
        url = f"{self.base}/v1/mgmt/outbound/app/user/token/latest"
        payload: Dict[str, Any] = {
            "appId": app_id,
            "userId": user_id,
            "options": {
                "withRefreshToken": bool(with_refresh),
                "forceRefresh": bool(force_refresh),
            },
        }
        if tenant_id:
            payload["tenantId"] = tenant_id
        r = requests.post(url, headers=self._headers(), json=payload, timeout=timeout)
        r.raise_for_status()        
        return r.json() or {}

    @staticmethod
    def sanitize_component(value: str) -> str:
        if not value:
            return ""
        # Keep only letters, numbers and '.'
        return re.sub(r"[^A-Za-z0-9.]", "", str(value))

    @classmethod
    def build_scope_name(cls, user_id: str, mcp_name: str, tool_name: str) -> str:
        return ".".join([
            cls.sanitize_component(user_id or ""),
            cls.sanitize_component(mcp_name or ""),
            cls.sanitize_component(tool_name or ""),
        ])

    # -------- DCR: Create inbound app per MCP --------
    def create_inbound_app(self, name: str, description: str, permissions_scopes: List[Dict[str, Any]], approved_callback_url: str = "http://localhost:3000/auth/inbound/callback", timeout: int = 15) -> Dict[str, Any]:
        # Allow both 3000 and 3001 locally so frontend ports can vary
        callback_urls = [approved_callback_url]
        if approved_callback_url.endswith(":3000/auth/inbound/callback"):
            callback_urls.append("http://localhost:3001/auth/inbound/callback")
        payload = {
            "name": name,
            "description": description or name,
            "approvedCallbackUrls": callback_urls,
            "permissionsScopes": permissions_scopes or [],
        }
        r = requests.post(f"{self.base}/v1/mgmt/thirdparty/app/create", headers=self._headers(), json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json() or {}

    # -------- Per-app helpers (when using per-MCP inbound apps) --------
    def load_app_by_id(self, app_id: str, timeout: int = 15) -> Dict[str, Any]:
        params = {"id": app_id}
        r = requests.get(
            f"{self.base}/v1/mgmt/thirdparty/app/load",
            headers=self._headers(),
            params=params,
            timeout=timeout,
        )
        r.raise_for_status()
        return r.json() or {}

    def patch_app_scopes_by_id(self, app_id: str, scopes: List[Dict[str, Any]], timeout: int = 15) -> Dict[str, Any]:
        payload = {"id": app_id, "permissionsScopes": scopes}
        r = requests.post(f"{self.base}/v1/mgmt/thirdparty/app/patch", headers=self._headers(), json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json() or {}

    def ensure_scopes_for_app(self, app_id: str, scopes_to_add: List[Dict[str, Any]], timeout: int = 15) -> Dict[str, Any]:
        app = self.load_app_by_id(app_id, timeout=timeout)
        existing = list((app.get("permissionsScopes") or []))
        by_name: Dict[str, Dict[str, Any]] = { (s.get("name") or ""): s for s in existing if s.get("name") }
        for s in (scopes_to_add or []):
            name = s.get("name")
            if not name:
                continue
            if name in by_name:
                by_name[name].update({
                    "description": s.get("description", by_name[name].get("description")),
                    "optional": s.get("optional", by_name[name].get("optional", False)),
                    "values": s.get("values", by_name[name].get("values", [])),
                })
            else:
                by_name[name] = {
                    "name": name,
                    "description": s.get("description") or "",
                    "optional": bool(s.get("optional", False)),
                    "values": list(s.get("values") or []),
                }
        merged = list(by_name.values())
        return self.patch_app_scopes_by_id(app_id, merged, timeout=timeout)
