from typing import Dict, Any, AsyncIterator, List
import json
import httpx
import traceback
import asyncio
import re
import os
from e2b_code_interpreter import AsyncSandbox

from core.sandbox_manager import SandboxManager
from core.session_manager import SessionManager
from core.database import DatabaseManager
from models.schemas import (
    CreateSandboxRequest,
    APIResponse,
    ChatMessage,
)


class SandboxEndpoints:
    def __init__(self, sandbox_manager: SandboxManager, session_manager: SessionManager, db_manager: DatabaseManager):
        self.sandbox_manager = sandbox_manager
        self.session_manager = session_manager
        self.db_manager = db_manager

    async def create_sandbox(self, request: CreateSandboxRequest, auth_data: Dict[str, Any]) -> Dict[str, Any]:
        user_id = auth_data['user_id']
        try:
            session_created = self.session_manager.create_session(request.chat_id, user_id)
            if not session_created:
                return {"success": False, "message": "Session conflict"}

            sandbox_response = await self.sandbox_manager.create_sandbox(request.chat_id, user_id)
            if sandbox_response.status == "error":
                return {"success": False, "message": "Failed to create sandbox"}

            await self.db_manager.save_chat_session(
                request.chat_id,
                user_id,
                request.enabled_mcps,
            )

            if sandbox_response.url:
                await self.db_manager.update_sandbox_url(request.chat_id, user_id, sandbox_response.url)

            for message in request.chat_history:
                if isinstance(message, dict):
                    msg = ChatMessage(**message)
                else:
                    msg = message
                await self.db_manager.save_chat_message(request.chat_id, user_id, msg)

            return {"success": True, "sandbox": sandbox_response.dict(), "message": "Sandbox created successfully"}

        except Exception as e:
            print(f"Error creating sandbox: {e}")
            return {"success": False, "message": f"Error creating sandbox: {str(e)}"}

    async def get_sandbox_info(self, chat_id: str, auth_data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            user_id = auth_data['user_id']
            sandbox_info = await self.sandbox_manager.get_sandbox_info(chat_id)
            if not sandbox_info:
                return {"success": False, "message": "No active sandbox found"}

            await self.sandbox_manager.update_activity(chat_id)
            self.session_manager.update_activity(chat_id)
            await self.db_manager.update_session_activity(chat_id, user_id)

            return {"success": True, "sandbox": sandbox_info.dict()}

        except Exception as e:
            print(f"Error getting sandbox info: {e}")
            return {"success": False, "message": f"Error getting sandbox info: {str(e)}"}

    async def terminate_sandbox(self, chat_id: str, auth_data: Dict[str, Any]) -> APIResponse:
        try:
            await self.sandbox_manager.terminate_sandbox(chat_id)
            self.session_manager.remove_session(chat_id)
            return APIResponse(success=True, message="Sandbox terminated successfully")
        except Exception as e:
            print(f"Error terminating sandbox: {e}")
            return APIResponse(success=False, message=f"Error terminating sandbox: {str(e)}")

    async def load_session_data(self, chat_id: str, auth_data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            user_id = auth_data['user_id']
            session_data = await self.db_manager.load_chat_session(chat_id, user_id)
            if not session_data:
                return {"success": False, "message": "No session data found"}
            return {
                "success": True,
                "data": {
                    "chat_history": [msg.model_dump() for msg in session_data.chat_history],
                    "enabled_mcps": session_data.enabled_mcps,
                },
            }
        except Exception as e:
            print(f"Error loading session data: {e}")
            return {"success": False, "message": f"Error loading session data: {str(e)}"}

    async def save_session_data(self, chat_id: str, auth_data: Dict[str, Any], chat_history: list, enabled_mcps: list) -> APIResponse:
        try:
            user_id = auth_data['user_id']
            await self.db_manager.save_chat_session(chat_id, user_id, enabled_mcps)
            for msg in chat_history:
                if isinstance(msg, dict):
                    message = ChatMessage(**msg)
                else:
                    message = msg
                await self.db_manager.save_chat_message(chat_id, user_id, message)
            return APIResponse(success=True, message="Session data saved successfully")
        except Exception as e:
            print(f"Error saving session data: {e}")
            return APIResponse(success=False, message=f"Error saving session data: {str(e)}")

    async def chat_stream(self, chat_id: str, message: str, auth_data: Dict[str, Any]) -> AsyncIterator[str]:
        try:
            user_id = auth_data['user_id']
            sandbox_info = await self.sandbox_manager.get_sandbox_info(chat_id)
            if not sandbox_info or not sandbox_info.url:
                yield f"data: {json.dumps({'error': 'No active sandbox found'})}\n\n"
                return

            user_message = ChatMessage(role="user", content=message)
            await self.db_manager.save_chat_message(chat_id, user_id, user_message)

            await self.sandbox_manager.update_activity(chat_id)
            self.session_manager.update_activity(chat_id)
            await self.db_manager.update_session_activity(chat_id, user_id)

            print("User role names:", (auth_data.get('token_data') or {}).get('roles', []))

            sandbox_url = sandbox_info.url
            async with httpx.AsyncClient(timeout=300.0) as client:
                async with client.stream(
                    "POST",
                    f"{sandbox_url}/chat/stream",
                    json={
                        "message": message,
                        "session_id": chat_id,
                        "user_id": user_id,
                        "user_role": ((auth_data.get('token_data') or {}).get('roles') or ['free'])[0]
                    },
                    headers={"Content-Type": "application/json"},
                ) as response:
                    if response.status_code != 200:
                        yield f"data: {json.dumps({'error': f'Sandbox error: {response.status_code}'})}\n\n"
                        return
                    assistant_content = ""
                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        # Pass through SSE data lines without double-prefixing
                        if line.startswith("data: "):
                            # Try to parse JSON to accumulate assistant content chunks and handle tool events
                            payload = line[len("data: "):]
                            try:
                                data = json.loads(payload)
                                if isinstance(data, dict):
                                    if data.get("type") == "content":
                                        assistant_content += data.get("content", "")
                                        # Forward as JSON to keep consistency
                                        yield f"data: {json.dumps(data)}\n\n"
                                        continue
                                    if data.get("type") == "assistant_message":
                                        # Persist the assistant message that includes tool_calls (OR-format)
                                        try:
                                            msg = data.get("message") or {}
                                            # Store exactly as provided
                                            assistant_msg = ChatMessage(**msg)
                                            await self.db_manager.save_chat_message(chat_id, user_id, assistant_msg)
                                        except Exception:
                                            pass
                                        # Forward downstream
                                        yield f"data: {json.dumps(data)}\n\n"
                                        continue
                                    if data.get("type") == "tool":
                                        # Before persisting any tool message, flush any accumulated assistant text
                                        if assistant_content.strip():
                                            try:
                                                assistant_message = ChatMessage(role="assistant", content=assistant_content.strip())
                                                await self.db_manager.save_chat_message(chat_id, user_id, assistant_message)
                                            except Exception:
                                                pass
                                            finally:
                                                assistant_content = ""
                                        # If final tool output, normalize links
                                        if data.get("state") == "output-available":
                                            tool_output = data.get("output") or {}
                                            tool_result = tool_output.get("result", "")
                                            try:
                                                # Handle create_download_link(path) -> actual URL
                                                if 'create_download_link(' in str(tool_result):
                                                    filepath_match = re.search(r'create_download_link\(([^)]+)\)', str(tool_result))
                                                    if filepath_match:
                                                        filepath = filepath_match.group(1).strip('\"\'')
                                                        try:
                                                            if filepath.startswith('/tmp/nirmal/'):
                                                                filename = filepath.replace('/tmp/nirmal/', '')
                                                                actual_url = f"{sandbox_url}/images/{filename}"
                                                            else:
                                                                sandbox_id = sandbox_url.split('-')[1].split('.')[0]
                                                                sandbox = await AsyncSandbox.connect(sandbox_id, api_key=os.environ.get("E2B_API_KEY"))
                                                                actual_url = sandbox.download_url(path=filepath)
                                                            data['output']['result'] = actual_url
                                                        except Exception as e:
                                                            data['output']['result'] = f"Error creating download link: {str(e)}"
                                                # Handle get_live_url(url) -> direct URL
                                                elif 'get_live_url(' in str(tool_result):
                                                    url_match = re.search(r'get_live_url\(([^)]+)\)', str(tool_result))
                                                    if url_match:
                                                        live_url = url_match.group(1).strip('\"\'')
                                                        data['output']['result'] = live_url
                                            except Exception:
                                                # Continue without modification on any error
                                                pass

                                            # Persist tool message in DB in OR format
                                            try:
                                                # Prefer full content if provided by backend; fallback to result preview
                                                full_content = data.get("content")
                                                if isinstance(full_content, str) and 'create_download_link(' in full_content:
                                                    # Apply same link normalization to full content
                                                    filepath_match = re.search(r'create_download_link\(([^)]+)\)', full_content)
                                                    if filepath_match:
                                                        filepath = filepath_match.group(1).strip('\"\'')
                                                        try:
                                                            if filepath.startswith('/tmp/nirmal/'):
                                                                filename = filepath.replace('/tmp/nirmal/', '')
                                                                full_content = f"{sandbox_url}/images/{filename}"
                                                            else:
                                                                sandbox_id = sandbox_url.split('-')[1].split('.')[0]
                                                                sandbox = await AsyncSandbox.connect(sandbox_id, api_key=os.environ.get("E2B_API_KEY"))
                                                                full_content = sandbox.download_url(path=filepath)
                                                        except Exception:
                                                            pass
                                                elif isinstance(full_content, str) and 'get_live_url(' in full_content:
                                                    url_match = re.search(r'get_live_url\(([^)]+)\)', full_content)
                                                    if url_match:
                                                        full_content = url_match.group(1).strip('\"\'')

                                                tool_message = ChatMessage(
                                                    role="tool",
                                                    tool_call_id=data.get("tool_call_id"),
                                                    name=(data.get("name") or data.get("tool_name")),
                                                    content=(full_content or (tool_output.get("result") if isinstance(tool_output, dict) else None))
                                                )
                                                await self.db_manager.save_chat_message(chat_id, user_id, tool_message)
                                            except Exception:
                                                pass
                                        # Forward (possibly modified) JSON tool event
                                        yield f"data: {json.dumps(data)}\n\n"
                                        continue

                                    # Flush assistant content when we hit a done marker (end of full stream)
                                    if data.get("done") is True and assistant_content.strip():
                                        try:
                                            assistant_message = ChatMessage(role="assistant", content=assistant_content.strip())
                                            await self.db_manager.save_chat_message(chat_id, user_id, assistant_message)
                                        except Exception:
                                            pass
                                        finally:
                                            assistant_content = ""
                            except Exception:
                                # Ignore non-JSON payloads for accumulation; fall back to raw forwarding
                                pass
                            # Forward raw line as-is if not handled above
                            yield f"{line}\n\n"
                        else:
                            yield f"data: {line}\n\n"

            # Persist full assistant message content after stream ends
            if assistant_content.strip():
                assistant_message = ChatMessage(role="assistant", content=assistant_content.strip())
                await self.db_manager.save_chat_message(chat_id, user_id, assistant_message)

        except Exception as e:
            print(traceback.format_exc())
            print(f"Error in chat stream: {e}")
            yield f"data: {json.dumps({'error': f'Chat error: {str(e)}'})}\n\n"

    async def get_user_chat_sessions(self, auth_data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            user_id = auth_data['user_id']
            sessions = await self.db_manager.get_user_chat_sessions(user_id)
            return {"success": True, "sessions": sessions}
        except Exception as e:
            print(f"Error getting user chat sessions: {e}")
            return {"success": False, "message": f"Error getting chat sessions: {str(e)}"}

    async def connect_chat(self, chat_id: str, auth_data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            user_id = auth_data['user_id']
            session_data = await self.db_manager.load_chat_session(chat_id, user_id)
            existing_sandbox_url = None
            chat_history = []
            enabled_mcps = []

            if session_data:
                chat_history = [msg.model_dump() for msg in session_data.chat_history]
                enabled_mcps = session_data.enabled_mcps
                existing_sandbox_url = session_data.sandbox_url
            else:
                await self.db_manager.save_chat_session(chat_id, user_id, enabled_mcps)

            sandbox_url = existing_sandbox_url
            sandbox_active = False

            if existing_sandbox_url:
                try:
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        response = await client.get(f"{existing_sandbox_url}/health")
                        if response.status_code == 200:
                            sandbox_active = True
                            from models.schemas import SandboxResponse, SandboxStatus
                            from datetime import datetime, timezone, timedelta
                            sandbox_id = existing_sandbox_url.split('-')[1].split('.')[0]
                            sandbox = await AsyncSandbox.connect(sandbox_id)
                            sandbox_info = {
                                "sandbox": sandbox,
                                "chat_id": chat_id,
                                "user_id": user_id,
                                "url": existing_sandbox_url,
                                "status": SandboxStatus.ACTIVE,
                                "created_at": datetime.now(timezone.utc),
                                "expires_at": datetime.now(timezone.utc) + timedelta(seconds=600),
                                "last_activity": datetime.now(timezone.utc),
                            }
                            self.sandbox_manager.active_sandboxes[chat_id] = sandbox_info
                except Exception:
                    pass

            if not sandbox_active:
                asyncio.create_task(self._create_sandbox_async(chat_id, user_id, chat_history, enabled_mcps))
                return {
                    "success": True,
                    "chat_id": chat_id,
                    "sandbox_url": None,
                    "chat_history": chat_history,
                    "enabled_mcps": enabled_mcps,
                    "sandbox_status": "creating",
                    "message": "Sandbox is being created, please wait...",
                }

            if chat_history:
                try:
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        await client.post(
                            f"{sandbox_url}/initialize",
                            json={
                                "chat_history": chat_history,
                                "enabled_mcps": enabled_mcps,
                                "session_id": chat_id,
                            },
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
                "message": "Connected to chat successfully",
            }

        except Exception as e:
            print(f"Error connecting to chat {chat_id}: {e}")
            return {"success": False, "message": f"Error connecting to chat: {str(e)}"}

    async def _create_sandbox_async(self, chat_id: str, user_id: str, chat_history: list, enabled_mcps: list):
        try:
            sandbox_response = await self.sandbox_manager.create_sandbox(chat_id, user_id)
            if sandbox_response.status != "error":
                sandbox_url = sandbox_response.url
                await self.db_manager.update_sandbox_url(chat_id, user_id, sandbox_url)
                if chat_history:
                    try:
                        async with httpx.AsyncClient(timeout=30.0) as client:
                            await client.post(
                                f"{sandbox_url}/initialize",
                                json={
                                    "chat_history": chat_history,
                                    "enabled_mcps": enabled_mcps,
                                    "session_id": chat_id,
                                },
                            )
                    except Exception as e:
                        print(f"Warning: Failed to initialize sandbox with history: {e}")
        except Exception as e:
            print(f"Error in async sandbox creation for chat {chat_id}: {e}")

    async def check_sandbox_status(self, chat_id: str, auth_data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            sandbox_info = await self.sandbox_manager.get_sandbox_info(chat_id)
            if not sandbox_info:
                return {"success": True, "sandbox_status": "creating", "message": "Sandbox is still being created"}
            return {"success": True, "sandbox_status": "ready", "sandbox_url": sandbox_info.url, "message": "Sandbox is ready"}
        except Exception as e:
            print(f"Error checking sandbox status: {e}")
            return {"success": False, "message": f"Error checking sandbox status: {str(e)}"}

    async def health_check(self) -> Dict[str, Any]:
        try:
            active_sandboxes = self.sandbox_manager.list_active_sandboxes()
            return {
                "status": "healthy",
                "service": "Sandbox Orchestrator",
                "active_sandboxes": len(active_sandboxes),
                "sandboxes": active_sandboxes,
            }
        except Exception as e:
            return {"status": "unhealthy", "error": str(e)}
