from fastapi import HTTPException
from typing import Dict, Any, AsyncIterator, List
import asyncio
import json
import httpx
import traceback
import re
from e2b_code_interpreter import AsyncSandbox

from core.sandbox_manager import SandboxManager
from core.session_manager import SessionManager
from core.database import DatabaseManager
from models.schemas import (
    CreateSandboxRequest,
    APIResponse,
    SandboxResponse,
    ChatMessage
)
import os
from dotenv import load_dotenv
load_dotenv()
from core.descope_api import DescopeAPI

class OrchestratorEndpoints:
    def __init__(self, sandbox_manager: SandboxManager, session_manager: SessionManager, db_manager: DatabaseManager):
        self.sandbox_manager = sandbox_manager
        self.session_manager = session_manager
        self.db_manager = db_manager

    # ------------------- MCP Validation Task -------------------
    async def start_mcp_validation(self, payload: Dict[str, Any], auth_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Start async task to:
         1) spin E2B instance with env + startup cmds
         2) get URL
         3) hit chatbot /mcp/introspect with MCP config
         4) on success, save MCP to DB
        Returns a task_id immediately.
        """
        # Persist input and create task
        task_row = await self.db_manager.create_task("mcp_validation", payload)
        if not task_row:
            return {"success": False, "message": "Failed to create task"}
        task_id = task_row["id"]

        async def runner():
            sandbox = None
            sandbox_url = None
            try:
                await self.db_manager.update_task_progress(task_id, "creating sandbox", status="running")
                # Merge envs: base + user-provided
                form_env = {kv.get("key"): kv.get("value") for kv in payload.get("envVars", []) if kv.get("key")}
                envs = {**self.sandbox_manager.sandbox_envs, **form_env}

                if self.sandbox_manager.local_testing:
                    # Local testing mode: run commands locally and use local chatbot URL
                    PATH_ROOT = "/tmp"
                    sandbox_url = self.sandbox_manager.local_chatbot_url
                    await self.db_manager.update_task_progress(task_id, "running local startup commands in /tmp")

                    # Prepare and log startup commands (sequential)
                    startup_cmds = [str(c) for c in (payload.get("startupCommands", []) or []) if str(c).strip()]
                    rendered_cmds = [c.replace("{path}", PATH_ROOT) for c in startup_cmds]
                    if rendered_cmds:
                        # Log to DB and print to terminal for quick debugging
                        seq = json.dumps(rendered_cmds)
                        print(f"[local-testing] startup sequence: {seq}")
                        await self.db_manager.update_task_progress(task_id, f"startup sequence: {seq}")
                    else:
                        await self.db_manager.update_task_progress(task_id, "no startup commands provided")

                    async def run_local(cmd: str):
                        import asyncio as _asyncio
                        # Use bash -lc for consistent PATH and better debug, print cwd and ls
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
                            # Surface detailed output to help diagnose e.g. ENOENT (os error 2)
                            await self.db_manager.complete_task(task_id, "failed", {"error": f"startup command failed: {cmd_use}", "rc": rc, "output": output})
                            return
                else:
                    # Create sandbox
                    sandbox = await AsyncSandbox.create(self.sandbox_manager.template_id, envs=envs, timeout=3000, api_key=envs.get("E2B_API_KEY"))
                    await self.db_manager.update_task_progress(task_id, "running startup commands in /tmp")
                    # Run startup commands under /tmp and support {path}
                    PATH_ROOT = "/tmp"
                    await sandbox.commands.run("mkdir -p /tmp", background=False)
                    for cmd in payload.get("startupCommands", []) or []:
                        if not cmd or not str(cmd).strip():
                            continue
                        cmd_use = str(cmd).replace("{path}", PATH_ROOT)
                        await sandbox.commands.run(f"cd {PATH_ROOT} && {cmd_use}", background=False)
                        
                    # Start server if not already running                    
                    await sandbox.commands.run("cd /app && nohup python main.py > /tmp/app.log 2>&1 &", background=True)
                    host = sandbox.get_host(3000)
                    sandbox_url = f"https://{host}"

                    # Wait for health
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

                # Build introspection config (replace {path} with /tmp)
                mcp_env_names = payload.get("mcpEnvNames") or []
                PATH_ROOT = "/tmp"
                # Collect ALL envs (both MCP-required and general), differentiating for later use
                all_env: dict = {}
                mcp_env: dict = {}
                general_env: dict = {}
                for pair in (payload.get("envVars", []) or []):
                    key = pair.get("key")
                    if not key:
                        continue
                    raw_val = pair.get("value")
                    # If empty, persist as empty string as requested
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
                        # Provide ALL envs to the MCP for initialization
                        "env": all_env or {},
                    }
                }

                await self.db_manager.update_task_progress(task_id, "introspecting MCP tools")
                async with httpx.AsyncClient(timeout=120.0) as client:
                    resp = await client.post(f"{sandbox_url}/mcp/introspect", json=introspect_req)
                    if resp.status_code != 200:
                        await self.db_manager.complete_task(task_id, "failed", {"error": f"introspect failed: {resp.status_code}", "body": await resp.aread()})
                        return
                    data = resp.json()

                # Save to registry with env keys and values
                env_with_values = all_env if all_env else {}
                config = {
                    "command": (payload.get("mcpCommand") or "").replace("{path}", PATH_ROOT),
                    "args": [(a or "").replace("{path}", PATH_ROOT) for a in (payload.get("mcpArgs") or [])],
                    "env": env_with_values,
                    # Persist full tool specs returned by introspection
                    "tools": data.get("tools") or [],
                    # Differentiate env classes for future UI/logic while keeping all values
                    "mcp_env_names": mcp_env_names,
                    "metadata": {
                        "visibility": ("private" if payload.get("isPrivate") else "public"),
                        "general_env_names": [k for k in env_with_values.keys() if k not in set(mcp_env_names)]
                    }
                }
                print("Tool introspection result:", data)
                title = payload.get("name") or (data.get("server_info", {}) or {}).get("name") or payload.get("mcpCommand")
                description = data.get("description") or f"MCP: {title}"

                # Prepare Descope scopes for tools: default role 'premium' (use role NAMES)
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
                                "values": ["premium"],  # role names, not IDs
                            })
                        # Load current app scopes, append/merge, and patch
                        try:
                            await asyncio.get_event_loop().run_in_executor(None, lambda: dapi.ensure_scopes(scopes_payload))
                        except Exception as de:
                            print(f"Warning: failed to patch Descope scopes: {de}")
                    # Store role selections and scope names in metadata for UI
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

        # fire-and-forget background task
        asyncio.create_task(runner())

        return {"success": True, "task_id": task_id}

    async def list_descope_roles(self, auth_data: Dict[str, Any]) -> Dict[str, Any]:
        """List Descope roles (management API)."""
        try:
            dapi = DescopeAPI()
            roles = await asyncio.get_event_loop().run_in_executor(None, dapi.list_roles)
            return {"success": True, "roles": roles}
        except Exception as e:
            return {"success": False, "message": f"Failed to list roles: {e}"}

    async def update_mcp_tool_roles(self, mcp_id: str, role_map: Dict[str, str], auth_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Update role assignment per tool for an MCP and patch Descope scopes accordingly.
        role_map is { tool_name: role_name }.
        Owner-only.
        """
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
                # Compute scope name if missing
                scope_name = scope_names.get(tname)
                if not scope_name:
                    scope_name = DescopeAPI.build_scope_name(user_id, mcp.get('name') or '', tname)
                    scope_names[tname] = scope_name
                # Always update local mapping
                tool_roles[tname] = desired_role_name
                scopes_payload.append({
                    "name": scope_name,
                    "description": (t or {}).get('description') or f"Scope for {tname}",
                    "optional": False,
                    "values": [desired_role_name],  # role names, not IDs
                })

            # Patch in Descope
            if scopes_payload:
                try:
                    await asyncio.get_event_loop().run_in_executor(None, lambda: dapi.ensure_scopes(scopes_payload))
                except Exception as de:
                    return {"success": False, "message": f"Failed to patch Descope scopes: {de}"}

            # Persist metadata updates
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

    async def create_sandbox(self, request: CreateSandboxRequest, auth_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new sandbox for a chat session"""
        user_id = auth_data['user_id']
        
        try:            
            # Create session
            session_created = self.session_manager.create_session(request.chat_id, user_id)
            if not session_created:
                raise HTTPException(status_code=409, detail="Session conflict")

            # Create sandbox
            sandbox_response = await self.sandbox_manager.create_sandbox(request.chat_id, user_id)

            if sandbox_response.status == "error":
                return {
                    "success": False,
                    "message": "Failed to create sandbox"
                }

            # Save initial session data to database with auth token
            await self.db_manager.save_chat_session(
                request.chat_id,
                user_id,
                request.enabled_mcps
            )

            # Store sandbox URL in database
            if sandbox_response.url:
                await self.db_manager.update_sandbox_url(
                    request.chat_id,
                    user_id,
                    sandbox_response.url
                )

            # Save initial chat messages if any
            for message in request.chat_history:
                from models.schemas import ChatMessage
                if isinstance(message, dict):
                    msg = ChatMessage(**message)
                else:
                    msg = message
                await self.db_manager.save_chat_message(request.chat_id, user_id, msg)

            return {
                "success": True,
                "sandbox": sandbox_response.dict(),
                "message": "Sandbox created successfully"
            }

        except Exception as e:
            print(f"Error creating sandbox: {e}")
            return {
                "success": False,
                "message": f"Error creating sandbox: {str(e)}"
            }

    async def get_sandbox_info(self, chat_id: str, auth_data: Dict[str, Any]) -> Dict[str, Any]:
        """Get sandbox information for a chat"""
        try:
            user_id = auth_data['user_id']

            # Get sandbox info
            sandbox_info = await self.sandbox_manager.get_sandbox_info(chat_id)
            if not sandbox_info:
                return {
                    "success": False,
                    "message": "No active sandbox found"
                }

            # Update activity
            await self.sandbox_manager.update_activity(chat_id)
            self.session_manager.update_activity(chat_id)
            await self.db_manager.update_session_activity(chat_id, user_id)

            return {
                "success": True,
                "sandbox": sandbox_info.dict()
            }

        except Exception as e:
            print(f"Error getting sandbox info: {e}")
            return {
                "success": False,
                "message": f"Error getting sandbox info: {str(e)}"
            }

    async def terminate_sandbox(self, chat_id: str, auth_data: Dict[str, Any]) -> APIResponse:
        """Manually terminate a sandbox"""
        try:
            user_id = auth_data['user_id']

            # Terminate sandbox
            terminated = await self.sandbox_manager.terminate_sandbox(chat_id)

            # Remove session
            self.session_manager.remove_session(chat_id)

            return APIResponse(
                success=terminated,
                message="Sandbox terminated successfully" if terminated else "Failed to terminate sandbox"
            )

        except Exception as e:
            print(f"Error terminating sandbox: {e}")
            return APIResponse(
                success=False,
                message=f"Error terminating sandbox: {str(e)}"
            )

    async def load_session_data(self, chat_id: str, auth_data: Dict[str, Any]) -> Dict[str, Any]:
        """Load session data from database for sandbox initialization"""
        try:
            user_id = auth_data['user_id']
            session_data = await self.db_manager.load_chat_session(chat_id, user_id)
            
            if not session_data:
                return {
                    "success": False,
                    "message": "No session data found"
                }
            
            return {
                "success": True,
                "data": {
                    "chat_history": [msg.model_dump() for msg in session_data.chat_history],
                    "enabled_mcps": session_data.enabled_mcps
                }
            }
            
        except Exception as e:
            print(f"Error loading session data: {e}")
            return {
                "success": False,
                "message": f"Error loading session data: {str(e)}"
            }

    async def save_session_data(self, chat_id: str, auth_data: Dict[str, Any], chat_history: list, enabled_mcps: list) -> APIResponse:
        """Save session data to database"""
        try:
            user_id = auth_data['user_id']
            # Save MCP configuration to session
            result = await self.db_manager.save_chat_session(chat_id, user_id, enabled_mcps)
            
            # Save chat messages separately
            from models.schemas import ChatMessage
            for msg in chat_history:
                if isinstance(msg, dict):
                    message = ChatMessage(**msg)
                else:
                    message = msg
                await self.db_manager.save_chat_message(chat_id, user_id, message)
            
            return APIResponse(
                success=result is not None,
                message="Session data saved successfully" if result else "Failed to save session data"
            )
            
        except Exception as e:
            print(f"Error saving session data: {e}")
            return APIResponse(
                success=False,
                message=f"Error saving session data: {str(e)}"
            )

    async def chat_stream(self, chat_id: str, message: str, auth_data: Dict[str, Any]) -> AsyncIterator[str]:
        """Handle streaming chat with sandbox"""
        try:
            user_id = auth_data['user_id']

            # Get sandbox info
            sandbox_info = await self.sandbox_manager.get_sandbox_info(chat_id)
            if not sandbox_info or not sandbox_info.url:
                yield f"data: {json.dumps({'error': 'No active sandbox found'})}\n\n"
                return

            # Save user message to database
            user_message = ChatMessage(role="user", content=message)
            await self.db_manager.save_chat_message(chat_id, user_id, user_message)

            # Update activity
            await self.sandbox_manager.update_activity(chat_id)
            self.session_manager.update_activity(chat_id)
            await self.db_manager.update_session_activity(chat_id, user_id)

            # Stream request to sandbox
            sandbox_url = sandbox_info.url
            print(f"Streaming chat to sandbox at {sandbox_url} for chat {chat_id}")
            async with httpx.AsyncClient(timeout=300.0) as client:
                async with client.stream(
                    "POST",
                    f"{sandbox_url}/chat/stream",
                    json={"message": message, "session_id": chat_id},
                    headers={"Content-Type": "application/json"}
                ) as response:
                    
                    if response.status_code != 200:
                        yield f"data: {json.dumps({'error': f'Sandbox error: {response.status_code}'})}\n\n"
                        return

                    assistant_content = ""
                    
                    # Stream response from sandbox - only modify download links
                    async for chunk in response.aiter_text():
                        if chunk.strip():
                            processed_chunk = ""
                            for line in chunk.split('\n'):
                                if line.startswith('data: '):
                                    try:
                                        data = json.loads(line[6:])
                                        
                                        # Accumulate content for database saving
                                        if data.get('type') == 'content':
                                            assistant_content += data.get('content', '')
                                            
                                        # Check for create_download_link in tool results
                                        elif data.get('type') == 'tool' and data.get('state') == 'output-available':
                                            tool_output = data.get('output', {})
                                            tool_result = tool_output.get('result', '')
                                            print(tool_result)
                                            
                                            if 'create_download_link(' in str(tool_result):
                                                filepath_match = re.search(r'create_download_link\(([^)]+)\)', str(tool_result))
                                                if filepath_match:
                                                    filepath = filepath_match.group(1).strip('"\'')
                                                    try:
                                                        if filepath.startswith('/tmp/nirmal/'):
                                                            filename = filepath.replace('/tmp/nirmal/', '')
                                                            actual_url = f"{sandbox_url}/images/{filename}"
                                                        else:
                                                            sandbox_id = sandbox_url.split('-')[1].split('.')[0]
                                                            sandbox = await AsyncSandbox.connect(sandbox_id, api_key=os.environ.get("E2B_API_KEY"))
                                                            actual_url = sandbox.download_url(path=filepath)

                                                        # Replace the result with the actual URL
                                                        data['output']['result'] = actual_url

                                                    except Exception as e:
                                                        print(f"Error creating download link: {e}")
                                                        data['output']['result'] = f"Error creating download link: {str(e)}"

                                            elif 'get_live_url(' in str(tool_result):
                                                # Extract the live URL directly from the tool result
                                                print(f"Debug: Processing get_live_url result: {tool_result}")
                                                url_match = re.search(r'get_live_url\(([^)]+)\)', str(tool_result))
                                                if url_match:
                                                    live_url = url_match.group(1).strip('"\'')
                                                    print(f"Debug: Extracted live URL: {live_url}")
                                                    print(f"Debug: URL length: {len(live_url)}")
                                                    # Replace the result with just the URL directly
                                                    data['output']['result'] = live_url
                                                    print(f"Debug: Updated data result to: {data['output']['result']}")
                                                else:
                                                    print(f"Debug: Failed to match get_live_url pattern in: {tool_result}")
                                        
                                        # Forward the (possibly modified) JSON message
                                        processed_chunk += f"data: {json.dumps(data)}\n"
                                        
                                    except Exception as e:
                                        print(f"Error processing chunk: {e}")
                                        processed_chunk += line + "\n"
                                else:
                                    processed_chunk += line + "\n"
                            yield processed_chunk if processed_chunk else chunk

                    # Save complete assistant response to database
                    if assistant_content.strip():
                        assistant_message = ChatMessage(role="assistant", content=assistant_content.strip())
                        await self.db_manager.save_chat_message(chat_id, user_id, assistant_message)

            yield f"data: {json.dumps({'done': True})}\n\n"

        except Exception as e:
            print(traceback.format_exc())
            print(f"Error in chat stream: {e}")
            yield f"data: {json.dumps({'error': f'Chat error: {str(e)}'})}\n\n"

    async def get_user_chat_sessions(self, auth_data: Dict[str, Any]) -> Dict[str, Any]:
        """Get all chat sessions for a user"""
        try:
            user_id = auth_data['user_id']
            sessions = await self.db_manager.get_user_chat_sessions(user_id)
            return {
                "success": True,
                "sessions": sessions
            }
        except Exception as e:
            print(f"Error getting user chat sessions: {e}")
            return {
                "success": False,
                "message": f"Error getting chat sessions: {str(e)}"
            }

    async def connect_chat(self, chat_id: str, auth_data: Dict[str, Any]) -> Dict[str, Any]:
        """Connect to a chat - handles both new and existing chats"""
        try:
            user_id = auth_data['user_id']
            # Load existing session data if it exists
            session_data = await self.db_manager.load_chat_session(chat_id, user_id)
            print(f"Connecting to chat {chat_id} for user {user_id}")
            existing_sandbox_url = None
            chat_history = []
            enabled_mcps = []
            
            if session_data:
                # Existing chat - get stored data
                chat_history = [msg.model_dump() for msg in session_data.chat_history]
                enabled_mcps = session_data.enabled_mcps
                # Get sandbox URL from database
                existing_sandbox_url = session_data.sandbox_url
                print(f"Loaded existing chat session for {chat_id} with sandbox URL: {existing_sandbox_url}")
            else:
                # New chat - create session in database
                await self.db_manager.save_chat_session(chat_id, user_id, enabled_mcps)
            
            # Try to use existing sandbox URL first
            sandbox_url = existing_sandbox_url
            sandbox_active = False
            
            if existing_sandbox_url:
                # Test if existing sandbox is still alive
                try:
                    import httpx
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        response = await client.get(f"{existing_sandbox_url}/health")
                        if response.status_code == 200:
                            sandbox_active = True
                            print(f"Reusing existing sandbox for chat {chat_id}: {existing_sandbox_url}")
                            # Update in-memory sandbox manager with existing URL
                            from models.schemas import SandboxResponse, SandboxStatus
                            from datetime import datetime, timezone, timedelta
                            sandbox_response = SandboxResponse(
                                sandbox_id=f"reused-{chat_id}",
                                chat_id=chat_id,
                                status=SandboxStatus.ACTIVE,
                                url=existing_sandbox_url,
                                created_at=datetime.now(timezone.utc).isoformat()
                            )
                            # Add to sandbox manager's in-memory tracking
                            # Extract sandbox ID from URL pattern https://{port}-{id}.e2b.app
                            sandbox_id = existing_sandbox_url.split('-')[1].split('.')[0]
                            sandbox = await AsyncSandbox.connect(sandbox_id)
                            sandbox_info = {
                                "sandbox": sandbox,  # No actual sandbox object for reused ones
                                "chat_id": chat_id,
                                "user_id": user_id,
                                "url": existing_sandbox_url,
                                "status": SandboxStatus.ACTIVE,
                                "created_at": datetime.now(timezone.utc),
                                "expires_at": datetime.now(timezone.utc) + timedelta(seconds=600),  # 10 min
                                "last_activity": datetime.now(timezone.utc)
                            }
                            self.sandbox_manager.active_sandboxes[chat_id] = sandbox_info
                        else:
                            print(f"Existing sandbox health check failed (status {response.status_code}) for chat {chat_id}, will create new one")
                except Exception as e:
                    print(f"Existing sandbox not responding for chat {chat_id}: {e}, will create new one")
            
            # Create new sandbox if needed
            if not sandbox_active:
                # Start async sandbox creation - don't wait for completion
                import asyncio
                asyncio.create_task(self._create_sandbox_async(chat_id, user_id, chat_history, enabled_mcps))                
                return {
                    "success": True,
                    "chat_id": chat_id,
                    "sandbox_url": None,
                    "chat_history": chat_history,
                    "enabled_mcps": enabled_mcps,
                    "sandbox_status": "creating",
                    "message": "Sandbox is being created, please wait..."
                }
            
            # Initialize sandbox with chat history
            if chat_history:
                # Send existing history to sandbox for initialization
                try:
                    import httpx
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        await client.post(
                            f"{sandbox_url}/initialize",
                            json={
                                "chat_history": chat_history,
                                "enabled_mcps": enabled_mcps,
                                "session_id": chat_id
                            }
                        )
                except Exception as e:
                    print(f"Warning: Failed to initialize sandbox with history: {e}")
            
            return {
                "success": True,
                "chat_id": chat_id,
                "sandbox_url": sandbox_url,
                "chat_history": chat_history,
                "enabled_mcps": enabled_mcps,
                "sandbox_status": "ready",
                "message": "Connected to chat successfully"
            }
            
        except Exception as e:
            print(f"Error connecting to chat {chat_id}: {e}")
            return {
                "success": False,
                "message": f"Error connecting to chat: {str(e)}"
            }

    async def _create_sandbox_async(self, chat_id: str, user_id: str, chat_history: list, enabled_mcps: list):
        """Create sandbox asynchronously"""
        try:
            sandbox_response = await self.sandbox_manager.create_sandbox(chat_id, user_id)
            if sandbox_response.status != "error":
                sandbox_url = sandbox_response.url
                # Update sandbox URL in database
                await self.db_manager.update_sandbox_url(chat_id, user_id, sandbox_url)
                print(f"Created new sandbox for chat {chat_id}: {sandbox_url}")
                
                # Initialize sandbox with chat history
                if chat_history:
                    try:
                        import httpx
                        async with httpx.AsyncClient(timeout=30.0) as client:
                            await client.post(
                                f"{sandbox_url}/initialize",
                                json={
                                    "chat_history": chat_history,
                                    "enabled_mcps": enabled_mcps,
                                    "session_id": chat_id
                                }
                            )
                    except Exception as e:
                        print(f"Warning: Failed to initialize sandbox with history: {e}")
            else:
                print(f"Failed to create sandbox for chat {chat_id}")
        except Exception as e:
            print(f"Error in async sandbox creation for chat {chat_id}: {e}")

    async def check_sandbox_status(self, chat_id: str, auth_data: Dict[str, Any]) -> Dict[str, Any]:
        """Check if sandbox is ready for a chat"""
        try:
            sandbox_info = await self.sandbox_manager.get_sandbox_info(chat_id)
            if not sandbox_info:
                return {
                    "success": True,
                    "sandbox_status": "creating",
                    "message": "Sandbox is still being created"
                }
            
            return {
                "success": True,
                "sandbox_status": "ready",
                "sandbox_url": sandbox_info.url,
                "message": "Sandbox is ready"
            }
        except Exception as e:
            print(f"Error checking sandbox status: {e}")
            return {
                "success": False,
                "message": f"Error checking sandbox status: {str(e)}"
            }

    async def get_mcps(self) -> Dict[str, Any]:
        """Get all available MCPs"""
        try:
            mcps = await self.db_manager.get_all_mcps()
            return {
                "success": True,
                "mcps": mcps
            }
        except Exception as e:
            print(f"Error getting MCPs: {e}")
            return {
                "success": False,
                "message": f"Error getting MCPs: {str(e)}"
            }

    async def update_mcp_general_env(self, mcp_id: str, env_updates: Dict[str, Any], auth_data: Dict[str, Any]) -> Dict[str, Any]:
        """Update general env (owner-managed) for an MCP. No auth client to DB; enforce ownership here."""
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
                # Nothing to update
                return {"success": True, "mcp": mcp}

            # Filter updates to allowed general env keys
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
        """Toggle MCP visibility (owner only)."""
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
        """Get per-user MCP settings (env vars) for an MCP"""
        try:
            user_id = auth_data['user_id']
            row = await self.db_manager.get_client_mcp(user_id, mcp_id)
            return {"success": True, "client_mcp": row}
        except Exception as e:
            print(f"Error getting client MCP for {mcp_id}: {e}")
            return {"success": False, "message": f"Error getting client MCP: {str(e)}"}

    async def save_client_mcp(self, mcp_id: str, env_variables: Dict[str, Any], auth_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create/Update per-user MCP settings (env vars). Uses authenticated Supabase client."""
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
        """Create a new MCP"""
        try:
            # Validate required fields
            if not name or not command:
                return {
                    "success": False,
                    "message": "Name and command are required"
                }
            
            if not args or len(args) == 0:
                return {
                    "success": False,
                    "message": "At least one argument is required"
                }
            
            # Check if MCP with same name already exists
            existing = await self.db_manager.get_mcp_by_name(name)
            if existing:
                return {
                    "success": False,
                    "message": f"MCP with name '{name}' already exists"
                }
            
            # Set defaults for optional fields
            final_title = title if title and title.strip() else name
            final_description = description if description and description.strip() else f"MCP tool: {name}"
            final_env = env if env else None
            
            # Build config object
            config = {
                "command": command,
                "args": args,
                "env": final_env
            }
            
            user_id = auth_data['user_id'] if auth_data else None
            mcp = await self.db_manager.create_mcp(name, final_title, final_description, config, user_id)
            if mcp:
                return {
                    "success": True,
                    "mcp": mcp,
                    "message": "MCP created successfully"
                }
            else:
                return {
                    "success": False,
                    "message": "Failed to create MCP"
                }
        except Exception as e:
            print(f"Error creating MCP: {e}")
            return {
                "success": False,
                "message": f"Error creating MCP: {str(e)}"
            }

    async def delete_mcp(self, mcp_id: str) -> Dict[str, Any]:
        """Delete MCP (empty endpoint - deletion ignored)"""
        return {
            "success": False,
            "message": "MCP deletion is not supported"
        }

    async def toggle_mcp_for_chat(self, chat_id: str, mcp_name: str, enabled: bool, config: dict = None, auth_data: Dict[str, Any] = None) -> Dict[str, Any]:
        """Toggle MCP for a chat session and update session table"""
        try:
            user_id = auth_data['user_id'] if auth_data else None
            # Get sandbox info
            sandbox_info = await self.sandbox_manager.get_sandbox_info(chat_id)
            if not sandbox_info or not sandbox_info.url:
                return {
                    "success": False,
                    "message": "No active sandbox found"
                }

            # Forward toggle request to sandbox
            sandbox_url = sandbox_info.url
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{sandbox_url}/toggle-mcp",
                    json={
                        "mcp_name": mcp_name,
                        "enabled": enabled,
                        "config": config if enabled else None
                    },
                    headers={"Content-Type": "application/json"}
                )
                
                if response.status_code != 200:
                    return {
                        "success": False,
                        "message": f"Sandbox toggle failed: {response.status_code}"
                    }

            # Update session table with the MCP change
            session_data = await self.db_manager.load_chat_session(chat_id, user_id)
            if session_data:
                enabled_mcps = session_data.enabled_mcps.copy() if session_data.enabled_mcps else []
                
                if enabled:
                    # Add/update MCP in enabled list
                    enabled_mcps = [mcp for mcp in enabled_mcps if mcp.get("name") != mcp_name]
                    enabled_mcps.append({"name": mcp_name, "config": config})
                else:
                    # Remove MCP from enabled list
                    enabled_mcps = [mcp for mcp in enabled_mcps if mcp.get("name") != mcp_name]
                
                # Save updated MCP list to session
                await self.db_manager.save_chat_session(chat_id, user_id, enabled_mcps)
            
            # Update activity
            await self.sandbox_manager.update_activity(chat_id)
            self.session_manager.update_activity(chat_id)
            await self.db_manager.update_session_activity(chat_id, user_id)
            
            return {
                "success": True,
                "message": f"MCP '{mcp_name}' {'enabled' if enabled else 'disabled'} successfully"
            }

        except Exception as e:
            print(f"Error toggling MCP: {e}")
            return {
                "success": False,
                "message": f"Error toggling MCP: {str(e)}"
            }

    async def health_check(self) -> Dict[str, Any]:
        """Health check endpoint"""
        try:
            active_sandboxes = self.sandbox_manager.list_active_sandboxes()
            
            return {
                "status": "healthy",
                "service": "Sandbox Orchestrator",
                "active_sandboxes": len(active_sandboxes),
                "sandboxes": active_sandboxes
            }
            
        except Exception as e:
            return {
                "status": "unhealthy",
                "error": str(e)
            }
