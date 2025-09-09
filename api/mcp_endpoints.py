from typing import Dict, Any, List
import asyncio
import json
import httpx
import traceback
from e2b_code_interpreter import AsyncSandbox

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
                    tools_list = data.get("tools") or []
                    user_id = auth_data.get("user_id")
                    mcp_name = payload.get("name") or payload.get("mcpCommand") or title
                    tool_roles = {}
                    descope_scopes = {}
                    if tools_list and user_id:
                        dapi = DescopeAPI()
                        scopes_payload = []
                        for t in tools_list:
                            tname = (t or {}).get("name") or "tool"
                            desc = (t or {}).get("description") or f"Scope for {tname}"
                            scope_name = DescopeAPI.build_scope_name(user_id, mcp_name, tname)
                            descope_scopes[tname] = scope_name
                            tool_roles[tname] = "premium"
                            scopes_payload.append({
                                "name": scope_name,
                                "description": desc,
                                "optional": False,
                                "values": ["premium"],
                            })
                        try:
                            await asyncio.get_event_loop().run_in_executor(None, lambda: dapi.ensure_scopes(scopes_payload))
                        except Exception as de:
                            print(f"Warning: failed to patch Descope scopes: {de}")
                    meta = config.get("metadata") or {}
                    if tool_roles:
                        meta["tool_roles"] = tool_roles
                    if descope_scopes:
                        meta["descope_scopes"] = descope_scopes
                    config["metadata"] = meta
                except Exception as e:
                    print(f"Warning: Descope scope setup error: {e}")

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
            scope_names = (meta.get('descope_scopes') or {})
            tool_roles = dict(meta.get('tool_roles') or {})

            dapi = DescopeAPI()
            scopes_payload = []
            for t in tools:
                tname = (t or {}).get('name')
                if not tname:
                    continue
                desired_role_name = (role_map or {}).get(tname)
                if not desired_role_name:
                    continue
                scope_name = scope_names.get(tname)
                if not scope_name:
                    scope_name = DescopeAPI.build_scope_name(user_id, mcp.get('name') or '', tname)
                    scope_names[tname] = scope_name
                tool_roles[tname] = desired_role_name
                scopes_payload.append({
                    "name": scope_name,
                    "description": (t or {}).get('description') or f"Scope for {tname}",
                    "optional": False,
                    "values": [desired_role_name],
                })

            if scopes_payload:
                try:
                    await asyncio.get_event_loop().run_in_executor(None, lambda: dapi.ensure_scopes(scopes_payload))
                except Exception as de:
                    return {"success": False, "message": f"Failed to patch Descope scopes: {de}"}

            updated = await self.db_manager.update_mcp_metadata_by_id(mcp_id, {"tool_roles": tool_roles, "descope_scopes": scope_names})
            if not updated:
                return {"success": False, "message": "Failed to update MCP metadata"}
            return {"success": True, "mcp": updated}
        except Exception as e:
            print(f"Error updating tool roles for MCP {mcp_id}: {e}")
            return {"success": False, "message": f"Error updating tool roles: {str(e)}"}

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
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    f"{sandbox_url}/toggle-mcp",
                    json={
                        "mcp_id": mcp_id,
                        "enabled": enabled,
                        "config": (mcp_config if enabled else None),
                        "slug": (mcp_row.get("name") or None),
                        "tool_roles": ((base_cfg.get("metadata") or {}).get("tool_roles") or None),
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
