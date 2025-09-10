from typing import Dict, Any, List
import asyncio
import json
import httpx
import traceback
from e2b_code_interpreter import AsyncSandbox
import os
from descope import DescopeClient
import jwt

from core.sandbox_manager import SandboxManager
from core.session_manager import SessionManager
from core.database import DatabaseManager
from core.descope_api import DescopeAPI


class MCPEndpoints:
    def __init__(self, sandbox_manager: SandboxManager, session_manager: SessionManager, db_manager: DatabaseManager):
        self.sandbox_manager = sandbox_manager
        self.session_manager = session_manager
        self.db_manager = db_manager

    async def start_mcp_validation(self, payload: Dict[str, Any], auth_data: Dict[str, Any]) -> Dict[str, Any]:
        task_row = await self.db_manager.create_task("mcp_validation", payload)
        if not task_row:
            return {"success": False, "message": "Failed to create task"}
        task_id = task_row["id"]

        async def runner():
            sandbox = None
            sandbox_url = None
            try:
                await self.db_manager.update_task_progress(task_id, "creating sandbox", status="running")
                form_env = {kv.get("key"): kv.get("value") for kv in payload.get("envVars", []) if kv.get("key")}
                envs = {**self.sandbox_manager.sandbox_envs, **form_env}

                if self.sandbox_manager.local_testing:
                    PATH_ROOT = "/tmp"
                    sandbox_url = self.sandbox_manager.local_chatbot_url
                    await self.db_manager.update_task_progress(task_id, "running local startup commands in /tmp")

                    startup_cmds = [str(c) for c in (payload.get("startupCommands", []) or []) if str(c).strip()]
                    rendered_cmds = [c.replace("{path}", PATH_ROOT) for c in startup_cmds]
                    if rendered_cmds:
                        seq = json.dumps(rendered_cmds)
                        print(f"[local-testing] startup sequence: {seq}")
                        await self.db_manager.update_task_progress(task_id, f"startup sequence: {seq}")
                    else:
                        await self.db_manager.update_task_progress(task_id, "no startup commands provided")

                    async def run_local(cmd: str):
                        import asyncio as _asyncio
                        proc = await _asyncio.create_subprocess_shell(
                            f"bash -lc 'set -euxo pipefail; mkdir -p {PATH_ROOT}; cd {PATH_ROOT}; echo [startup] {cmd}; pwd; ls -la; {cmd}'",
                            stdout=_asyncio.subprocess.PIPE,
                            stderr=_asyncio.subprocess.STDOUT,
                        )
                        out, _ = await proc.communicate()
                        return proc.returncode, (out.decode(errors='ignore') if out else '')

                    for raw_cmd in startup_cmds:
                        if not raw_cmd or not str(raw_cmd).strip():
                            continue
                        cmd_use = str(raw_cmd).replace("{path}", PATH_ROOT)
                        print(f"[local-testing] running: {cmd_use}")
                        await self.db_manager.update_task_progress(task_id, f"running: {cmd_use}")
                        rc, output = await run_local(cmd_use)
                        print(f"[local-testing] completed: {cmd_use} rc={rc}")
                        await self.db_manager.update_task_progress(task_id, f"completed: {cmd_use} rc={rc}")
                        if rc != 0:
                            await self.db_manager.complete_task(task_id, "failed", {"error": f"startup command failed: {cmd_use}", "rc": rc, "output": output})
                            return
                else:
                    sandbox = await AsyncSandbox.create(self.sandbox_manager.template_id, envs=envs, timeout=3000, api_key=envs.get("E2B_API_KEY"))
                    await self.db_manager.update_task_progress(task_id, "running startup commands in /tmp")
                    PATH_ROOT = "/tmp"
                    await sandbox.commands.run("mkdir -p /tmp", background=False)
                    for cmd in payload.get("startupCommands", []) or []:
                        if not cmd or not str(cmd).strip():
                            continue
                        cmd_use = str(cmd).replace("{path}", PATH_ROOT)
                        await sandbox.commands.run(f"cd {PATH_ROOT} && {cmd_use}", background=False)
                    await sandbox.commands.run("cd /app && nohup python main.py > /tmp/app.log 2>&1 &", background=True)
                    host = sandbox.get_host(3000)
                    sandbox_url = f"https://{host}"
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        while True:
                            try:
                                r = await client.get(f"{sandbox_url}/health")
                                if r.status_code == 200:
                                    break
                            except httpx.RequestError:
                                pass
                            await asyncio.sleep(3)

                await self.db_manager.update_task_progress(task_id, f"sandbox ready: {sandbox_url}")

                mcp_env_names = payload.get("mcpEnvNames") or []
                PATH_ROOT = "/tmp"
                all_env: dict = {}
                mcp_env: dict = {}
                general_env: dict = {}
                for pair in (payload.get("envVars", []) or []):
                    key = pair.get("key")
                    if not key:
                        continue
                    raw_val = pair.get("value")
                    if raw_val is None:
                        val = ""
                    else:
                        val = raw_val
                    if isinstance(val, str):
                        val = val.replace("{path}", PATH_ROOT)
                    all_env[key] = val
                    if key in mcp_env_names:
                        mcp_env[key] = val
                    else:
                        general_env[key] = val

                introspect_req = {
                    "name": payload.get("name"),
                    "config": {
                        "command": (payload.get("mcpCommand") or "").replace("{path}", PATH_ROOT),
                        "args": [(a or "").replace("{path}", PATH_ROOT) for a in (payload.get("mcpArgs") or [])],
                        "env": all_env or {},
                    },
                }

                await self.db_manager.update_task_progress(task_id, "introspecting MCP tools")
                async with httpx.AsyncClient(timeout=120.0) as client:
                    resp = await client.post(f"{sandbox_url}/mcp/introspect", json=introspect_req)
                    if resp.status_code != 200:
                        await self.db_manager.complete_task(task_id, "failed", {"error": f"introspect failed: {resp.status_code}", "body": await resp.aread()})
                        return
                    data = resp.json()

                env_with_values = all_env if all_env else {}
                config = {
                    "command": (payload.get("mcpCommand") or "").replace("{path}", PATH_ROOT),
                    "args": [(a or "").replace("{path}", PATH_ROOT) for a in (payload.get("mcpArgs") or [])],
                    "env": env_with_values,
                    "tools": data.get("tools") or [],
                    "mcp_env_names": mcp_env_names,
                    "metadata": {
                        "visibility": ("private" if payload.get("isPrivate") else "public"),
                        "general_env_names": [k for k in env_with_values.keys() if k not in set(mcp_env_names)],
                    },
                }
                title = payload.get("name") or (data.get("server_info", {}) or {}).get("name") or payload.get("mcpCommand")
                description = data.get("description") or f"MCP: {title}"

                try:
                    # Create a dedicated inbound OAuth app (DCR) per MCP and store its credentials
                    tools_list = data.get("tools") or []
                    meta = config.get("metadata") or {}
                    inbound_existing = meta.get("inbound_app")
                    if not inbound_existing:
                        dapi = DescopeAPI()
                        # Build permissions scopes: always include full_access, plus one per tool
                        scopes_payload = [{
                            "name": "full_access",
                            "description": "full access",
                            "optional": True,
                            "values": ["premium"],
                        }]
                        for t in tools_list:
                            tname = (t or {}).get("name")
                            if not tname:
                                continue
                            desc = (t or {}).get("description") or f"Scope for {tname}"
                            scopes_payload.append({
                                "name": tname,
                                "description": desc,
                                "optional": True,
                                "values": ["premium"],
                            })
                        app_name = payload.get("name") or payload.get("mcpCommand") or title
                        app_desc = description or app_name
                        created = await asyncio.get_event_loop().run_in_executor(
                            None, lambda: dapi.create_inbound_app(app_name, app_desc, scopes_payload)
                        )
                        inbound_meta = {
                            "id": (created or {}).get("id"),
                            "clientId": (created or {}).get("clientId"),
                            "cleartext": (created or {}).get("cleartext"),
                        }
                        meta["inbound_app"] = inbound_meta
                        config["metadata"] = meta
                except Exception as e:
                    print(f"Warning: DCR inbound app creation failed: {e}")

                saved = await self.db_manager.create_mcp(
                    name=payload.get("name") or payload.get("mcpCommand"),
                    title=title,
                    description=description,
                    config=config,
                    user_id=auth_data.get("user_id"),
                )

                await self.db_manager.complete_task(task_id, "succeeded", {
                    "sandbox_url": sandbox_url,
                    "tools": data.get("tools"),
                    "server_info": data.get("server_info"),
                    "saved_mcp": saved,
                })
            except Exception as e:
                await self.db_manager.complete_task(task_id, "failed", {"error": str(e), "trace": traceback.format_exc()})
            finally:
                try:
                    if sandbox and not self.sandbox_manager.local_testing:
                        await sandbox.kill()
                except Exception:
                    pass

        asyncio.create_task(runner())
        return {"success": True, "task_id": task_id}

    async def list_outbound_apps(self, auth_data: Dict[str, Any]) -> Dict[str, Any]:
        """Return list of outbound apps using Descope Python SDK (management key required)."""
        try:
            project_id = os.environ.get("DESCOPE_PROJECT_ID")
            mgmt_key = os.environ.get("DESCOPE_MANAGEMENT_KEY") or os.environ.get("DESCOPE_MGMT_KEY")
            if not project_id or not mgmt_key:
                return {"success": False, "message": "Descope management not configured"}

            def _load_all():
                client = DescopeClient(project_id=project_id, management_key=mgmt_key)
                return client.mgmt.outbound_application.load_all_applications()

            body = await asyncio.get_event_loop().run_in_executor(None, _load_all)
            apps = (body or {}).get("apps") or []
            return {"success": True, "apps": apps, "raw": body}
        except Exception as e:
            return {"success": False, "message": f"Failed to list outbound apps: {e}"}

    async def latest_outbound_token(self, app_id: str, tenant_id: str, auth_data: Dict[str, Any]) -> Dict[str, Any]:
        """Return the latest outbound token for the current user and app."""
        try:
            user_id = auth_data.get("user_id")
            if not user_id:
                return {"success": False, "message": "Missing user_id"}
            dapi = DescopeAPI()
            body = await asyncio.get_event_loop().run_in_executor(
                None, lambda: dapi.get_latest_outbound_token(app_id, user_id, tenant_id=tenant_id or None)
            )
            token = (body or {}).get("token") or {}
            return {"success": True, "token": token, "raw": body}
        except Exception as e:
            return {"success": False, "message": f"Failed to fetch latest token: {e}"}

    async def is_outbound_connected(self, app_id: str, tenant_id: str, auth_data: Dict[str, Any]) -> Dict[str, Any]:
        """Return boolean indicating whether user has a stored token for the app.

        Does NOT return tokens or sensitive data, only a boolean flag.
        """
        try:
            user_id = auth_data.get("user_id")
            if not user_id:
                return {"success": False, "connected": False, "message": "Missing user_id"}
            dapi = DescopeAPI()
            body = await asyncio.get_event_loop().run_in_executor(
                None, lambda: dapi.get_latest_outbound_token(app_id, user_id, tenant_id=tenant_id or None)
            )
            token = (body or {}).get("token") or {}
            connected = bool(token.get("accessToken"))
            return {"success": True, "connected": connected}
        except Exception:
            # Any error treated as not connected (no token stored)
            return {"success": True, "connected": False}

    async def list_descope_roles(self, auth_data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            dapi = DescopeAPI()
            roles = await asyncio.get_event_loop().run_in_executor(None, dapi.list_roles)
            return {"success": True, "roles": roles}
        except Exception as e:
            return {"success": False, "message": f"Failed to list roles: {e}"}

    async def update_mcp_tool_roles(self, mcp_id: str, role_map: Dict[str, str], auth_data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            user_id = auth_data['user_id']
            mcp = await self.db_manager.get_mcp_by_id(mcp_id)
            if not mcp:
                return {"success": False, "message": "MCP not found"}
            if (mcp.get('user_id') or '') != user_id:
                return {"success": False, "message": "Not authorized"}

            cfg = (mcp.get('config') or {})
            meta = (cfg.get('metadata') or {})
            tools = (cfg.get('tools') or [])
            inbound = (meta.get('inbound_app') or {})
            inbound_app_id = inbound.get('id')
            if not inbound_app_id:
                return {"success": False, "message": "Inbound app not found for MCP"}

            # Build scopes (per tool) based on desired role mapping
            scopes_payload = []
            tool_roles = dict(meta.get('tool_roles') or {})
            for t in tools:
                tname = (t or {}).get('name')
                if not tname:
                    continue
                desired_role = (role_map or {}).get(tname)
                if not desired_role:
                    continue
                desc = (t or {}).get('description') or f"Scope for {tname}"
                # values rule: premium -> ["premium"]; free -> ["free","premium"]
                values = ["premium"] if desired_role == "premium" else ["free", "premium"]
                tool_roles[tname] = desired_role
                scopes_payload.append({
                    "name": tname,
                    "description": desc,
                    "optional": True,
                    "values": values,
                })

            if scopes_payload:
                dapi = DescopeAPI()
                try:
                    await asyncio.get_event_loop().run_in_executor(
                        None, lambda: dapi.ensure_scopes_for_app(inbound_app_id, scopes_payload)
                    )
                except Exception as de:
                    return {"success": False, "message": f"Failed to patch Descope scopes: {de}"}

            updated = await self.db_manager.update_mcp_metadata_by_id(mcp_id, {"tool_roles": tool_roles})
            if not updated:
                return {"success": False, "message": "Failed to update MCP metadata"}
            return {"success": True, "mcp": updated}
        except Exception as e:
            print(f"Error updating tool roles for MCP {mcp_id}: {e}")
            return {"success": False, "message": f"Error updating tool roles: {str(e)}"}

    async def toggle_gmail_integration(self, chat_id: str, app_id: str, tenant_id: str, enabled: bool, auth_data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            user_id = auth_data.get("user_id")
            if not user_id:
                return {"success": False, "message": "Missing user_id"}

            # Find sandbox for chat
            sandbox_info = await self.sandbox_manager.get_sandbox_info(chat_id)
            if not sandbox_info or not sandbox_info.url:
                return {"success": False, "message": "No active sandbox found"}

            sandbox_url = sandbox_info.url

            # If disabling, just forward to chatbot
            if not enabled:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(
                        f"{sandbox_url}/integrations/gmail/toggle",
                        json={"enabled": False},
                        headers={"Content-Type": "application/json"},
                    )
                return {"success": resp.status_code == 200, "status": resp.status_code}

            # Enabled case: fetch latest token and build credentials payload
            dapi = DescopeAPI()
            body = await asyncio.get_event_loop().run_in_executor(
                None, lambda: dapi.get_latest_outbound_token(app_id, user_id, tenant_id=tenant_id or None)
            )
            token = (body or {}).get("token") or {}
            access_token = token.get("accessToken")
            refresh_token = token.get("refreshToken") or token.get("refresh_token")
            scopes = token.get("scopes") or []

            if not access_token:
                return {"success": False, "message": "No access token available for user/app"}

            client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
            client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
            if not client_id or not client_secret:
                return {"success": False, "message": "Missing GOOGLE_OAUTH_CLIENT_ID/SECRET"}

            creds_payload = {
                "token": access_token,
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
                "token_uri": "https://oauth2.googleapis.com/token",
                "scopes": scopes or ["https://mail.google.com/"],
            }

            # Forward to chatbot to register tools
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    f"{sandbox_url}/integrations/gmail/toggle",
                    json={"enabled": True, "credentials": creds_payload},
                    headers={"Content-Type": "application/json"},
                )
            if resp.status_code != 200:
                try:
                    return {"success": False, "message": f"Chatbot rejected: {resp.status_code}", "body": resp.text}
                except Exception:
                    return {"success": False, "message": f"Chatbot rejected: {resp.status_code}"}

            return {"success": True}
        except Exception as e:
            print(f"Error toggling Gmail integration: {e}")
            return {"success": False, "message": f"Error toggling Gmail integration: {str(e)}"}

    async def get_task_status(self, task_id: str) -> Dict[str, Any]:
        task = await self.db_manager.get_task(task_id)
        if not task:
            return {"success": False, "message": "task not found"}
        return {"success": True, "task": task}

    async def get_mcps(self) -> Dict[str, Any]:
        try:
            mcps = await self.db_manager.get_all_mcps()
            return {"success": True, "mcps": mcps}
        except Exception as e:
            print(f"Error getting MCPs: {e}")
            return {"success": False, "message": f"Error getting MCPs: {str(e)}"}

    async def get_inbound_config(self, mcp_id: str) -> Dict[str, Any]:
        """Return per-MCP inbound OAuth config (clientId and scopes to request)."""
        try:
            mcp = await self.db_manager.get_mcp_by_id(mcp_id)
            if not mcp:
                return {"success": False, "message": "MCP not found"}
            cfg = (mcp.get("config") or {})
            meta = (cfg.get("metadata") or {})
            inbound = (meta.get("inbound_app") or {})
            client_id = inbound.get("clientId")
            # Build scopes: include full_access plus tool names when available
            tool_names = [ (t or {}).get("name") for t in (cfg.get("tools") or []) if (t or {}).get("name") ]
            scopes = ["full_access"] + (tool_names or [])
            return {"success": True, "clientId": client_id, "scopes": scopes}
        except Exception as e:
            return {"success": False, "message": f"Failed to get inbound config: {e}"}

    async def set_user_membership_role(self, role: str, auth_data: Dict[str, Any]) -> Dict[str, Any]:
        """Set the current user's role to one of ['free', 'premium'] using Descope management API."""
        try:
            role_norm = (role or '').strip().lower()
            if role_norm not in ('free', 'premium'):
                return {"success": False, "message": "Invalid role"}

            user_id = auth_data.get('user_id')
            if not user_id:
                return {"success": False, "message": "Missing user id"}

            # Use Descope Python SDK with management key
            import os
            from descope import DescopeClient
            project_id = os.environ.get('DESCOPE_PROJECT_ID')
            mgmt_key = os.environ.get('DESCOPE_MANAGEMENT_KEY')
            if not project_id or not mgmt_key:
                return {"success": False, "message": "Descope management not configured"}

            client = DescopeClient(project_id=project_id, management_key=mgmt_key)
            # Set single role for demo simplicity
            client.mgmt.user.set_roles(login_id=user_id, role_names=[role_norm])
            return {"success": True, "role": role_norm}
        except Exception as e:
            print(f"Error setting membership role: {e}")
            return {"success": False, "message": f"Failed to set role: {str(e)}"}

    async def update_mcp_general_env(self, mcp_id: str, env_updates: Dict[str, Any], auth_data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            user_id = auth_data['user_id']
            mcp = await self.db_manager.get_mcp_by_id(mcp_id)
            if not mcp:
                return {"success": False, "message": "MCP not found"}
            if (mcp.get('user_id') or '') != user_id:
                return {"success": False, "message": "Not authorized"}

            cfg = (mcp.get('config') or {})
            meta = (cfg.get('metadata') or {})
            general_names = set((meta.get('general_env_names') or []))
            if not general_names:
                return {"success": True, "mcp": mcp}

            updates = {k: (v if v is not None else "") for k, v in (env_updates or {}).items() if k in general_names}
            base_env = dict((cfg.get('env') or {}))
            base_env.update(updates)

            updated = await self.db_manager.update_mcp_env_by_id(mcp_id, base_env)
            if not updated:
                return {"success": False, "message": "Failed to update MCP env"}
            return {"success": True, "mcp": updated}
        except Exception as e:
            print(f"Error updating MCP env {mcp_id}: {e}")
            return {"success": False, "message": f"Error updating MCP env: {str(e)}"}

    async def update_mcp_visibility(self, mcp_id: str, visibility: str, auth_data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            user_id = auth_data['user_id']
            mcp = await self.db_manager.get_mcp_by_id(mcp_id)
            if not mcp:
                return {"success": False, "message": "MCP not found"}
            if (mcp.get('user_id') or '') != user_id:
                return {"success": False, "message": "Not authorized"}
            new_vis = (visibility or '').lower()
            updated = await self.db_manager.update_mcp_visibility_by_id(mcp_id, new_vis)
            if not updated:
                return {"success": False, "message": "Failed to update visibility"}
            return {"success": True, "mcp": updated}
        except Exception as e:
            print(f"Error updating MCP visibility {mcp_id}: {e}")
            return {"success": False, "message": f"Error updating MCP visibility: {str(e)}"}

    async def get_client_mcp(self, mcp_id: str, auth_data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            user_id = auth_data['user_id']
            row = await self.db_manager.get_client_mcp(user_id, mcp_id)
            return {"success": True, "client_mcp": row}
        except Exception as e:
            print(f"Error getting client MCP for {mcp_id}: {e}")
            return {"success": False, "message": f"Error getting client MCP: {str(e)}"}

    async def save_client_mcp(self, mcp_id: str, env_variables: Dict[str, Any], auth_data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            user_id = auth_data['user_id']
            row = await self.db_manager.upsert_client_mcp(user_id, mcp_id, env_variables or {})
            if not row:
                return {"success": False, "message": "Failed to save client MCP"}
            return {"success": True, "client_mcp": row}
        except Exception as e:
            print(f"Error saving client MCP for {mcp_id}: {e}")
            return {"success": False, "message": f"Error saving client MCP: {str(e)}"}

    async def create_mcp(self, name: str, command: str, args: List[str], title: str = None, description: str = None, env: Dict[str, str] = None, auth_data: Dict[str, Any] = None) -> Dict[str, Any]:
        try:
            if not name or not command:
                return {"success": False, "message": "Name and command are required"}
            if not args or len(args) == 0:
                return {"success": False, "message": "At least one argument is required"}
            existing = await self.db_manager.get_mcp_by_name(name)
            if existing:
                return {"success": False, "message": f"MCP with name '{name}' already exists"}

            final_title = title if title and title.strip() else name
            final_description = description if description and description.strip() else f"MCP tool: {name}"
            final_env = env if env else None

            config = {"command": command, "args": args, "env": final_env}
            user_id = auth_data['user_id'] if auth_data else None
            mcp = await self.db_manager.create_mcp(name, final_title, final_description, config, user_id)
            if mcp:
                return {"success": True, "mcp": mcp, "message": "MCP created successfully"}
            else:
                return {"success": False, "message": "Failed to create MCP"}
        except Exception as e:
            print(f"Error creating MCP: {e}")
            return {"success": False, "message": f"Error creating MCP: {str(e)}"}

    async def delete_mcp(self, mcp_id: str) -> Dict[str, Any]:
        return {"success": False, "message": "MCP deletion is not supported"}

    async def toggle_mcp_for_chat(self, chat_id: str, mcp_id: str, enabled: bool, auth_data: Dict[str, Any] = None) -> Dict[str, Any]:
        try:
            user_id = auth_data['user_id'] if auth_data else None
            sandbox_info = await self.sandbox_manager.get_sandbox_info(chat_id)
            print(f"Toggle MCP {mcp_id} enabled={enabled} for chat {chat_id} user {user_id}, sandbox: {sandbox_info}")
            if not sandbox_info or not sandbox_info.url:
                return {"success": False, "message": "No active sandbox found"}

            # Build MCP config from DB (similar to start_mcp_validation)
            mcp_row = await self.db_manager.get_mcp_by_id(mcp_id)
            if not mcp_row:
                return {"success": False, "message": "MCP not found"}
            base_cfg = (mcp_row.get("config") or {})
                        
            PATH_ROOT = "/tmp"
            cmd = (base_cfg.get("command") or "").replace("{path}", PATH_ROOT)
            args = [str(a or "").replace("{path}", PATH_ROOT) for a in (base_cfg.get("args") or [])]
            env_base = dict(base_cfg.get("env") or {})

            # Merge client-specific env overrides
            client_row = None
            if user_id:
                client_row = await self.db_manager.get_client_mcp(user_id, mcp_id)
            client_env = dict((client_row or {}).get("mcp_env_variables") or {})
            final_env = {**env_base, **client_env} if (env_base or client_env) else None

            mcp_config = {
                "command": cmd,
                "args": args,
                **({"env": final_env} if final_env is not None else {}),
            }

            sandbox_url = sandbox_info.url
            # Build allowed_tools based on inbound access token scopes
            allowed_tools: List[str] = []
            try:
                client_row = await self.db_manager.get_client_mcp(user_id, mcp_id) if user_id else None
                token = (client_row or {}).get("access_token")
                tool_names = [ (t or {}).get("name") for t in (base_cfg.get("tools") or []) if (t or {}).get("name") ]
                if token and tool_names:
                    claims = jwt.decode(token, options={"verify_signature": False})
                    scopes = set((claims.get("scope") or "").split())
                    if "full_access" in scopes:
                        allowed_tools = tool_names
                    else:
                        allowed_tools = [t for t in tool_names if t in scopes]
                    print(f"Allowed tools for MCP {mcp_id} user {user_id}: {allowed_tools}")
            except Exception:
                # On any error, fall back to empty (no extra filter) and let chatbot register all
                allowed_tools = []

            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    f"{sandbox_url}/toggle-mcp",
                    json={
                        "mcp_id": mcp_id,
                        "enabled": enabled,
                        "config": (mcp_config if enabled else None),
                        "slug": (mcp_row.get("name") or None),
                        **({"allowed_tools": allowed_tools} if allowed_tools else {}),
                    },
                    headers={"Content-Type": "application/json"},
                )
                if response.status_code != 200:
                    body = None
                    try:
                        body = response.text
                    except Exception:
                        pass
                    return {"success": False, "message": f"Sandbox toggle failed: {response.status_code}", "body": body}

            # Update session enabled MCPs as a mapping {mcp_id: mcp_id}
            session_data = await self.db_manager.load_chat_session(chat_id, user_id)
            if session_data:
                current = session_data.enabled_mcps if session_data.enabled_mcps is not None else {}
                # Normalize to dict mapping
                if isinstance(current, list):
                    # Previous format; start fresh mapping
                    enabled_mcps_map = {}
                elif isinstance(current, dict):
                    enabled_mcps_map = dict(current)
                else:
                    enabled_mcps_map = {}

                if enabled:
                    enabled_mcps_map[mcp_id] = mcp_id
                else:
                    enabled_mcps_map.pop(mcp_id, None)

                await self.db_manager.save_chat_session(chat_id, user_id, enabled_mcps_map)

            await self.sandbox_manager.update_activity(chat_id)
            self.session_manager.update_activity(chat_id)
            await self.db_manager.update_session_activity(chat_id, user_id)

            return {"success": True, "message": f"MCP '{mcp_id}' {'enabled' if enabled else 'disabled'} successfully"}

        except Exception as e:
            print(f"Error toggling MCP: {e}")
            return {"success": False, "message": f"Error toggling MCP: {str(e)}"}
