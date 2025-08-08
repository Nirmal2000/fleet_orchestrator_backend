from fastapi import HTTPException
from typing import Dict, Any, AsyncIterator, List
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
    ForceConnectRequest, 
    SessionConflictResponse,
    APIResponse,
    SandboxResponse,
    ChatMessage
)

class OrchestratorEndpoints:
    def __init__(self, sandbox_manager: SandboxManager, session_manager: SessionManager, db_manager: DatabaseManager):
        self.sandbox_manager = sandbox_manager
        self.session_manager = session_manager
        self.db_manager = db_manager

    async def create_sandbox(self, request: CreateSandboxRequest, device_id: str, access_token: str = None) -> Dict[str, Any]:
        """Create a new sandbox for a chat session"""
        try:
            # Check for session conflicts
            conflict = self.session_manager.check_session_conflict(request.chat_id, device_id)
            if conflict.conflict:
                return {
                    "success": False,
                    "conflict": True,
                    "message": conflict.message,
                    "current_device": conflict.current_device
                }
            
            # Create session
            session_created = self.session_manager.create_session(request.chat_id, request.user_id, device_id)
            if not session_created:
                raise HTTPException(status_code=409, detail="Session conflict")
            
            # Create sandbox
            sandbox_response = await self.sandbox_manager.create_sandbox(request.chat_id, request.user_id)
            
            if sandbox_response.status == "error":
                return {
                    "success": False,
                    "message": "Failed to create sandbox"
                }
            
            # Save initial session data to database with auth token
            await self.db_manager.save_chat_session(
                request.chat_id,
                request.user_id,
                request.enabled_mcps,
                access_token
            )
            
            # Store sandbox URL in database
            if sandbox_response.url:
                await self.db_manager.update_sandbox_url(
                    request.chat_id,
                    request.user_id, 
                    sandbox_response.url,
                    access_token
                )
            
            # Save initial chat messages if any
            for message in request.chat_history:
                from models.schemas import ChatMessage
                if isinstance(message, dict):
                    msg = ChatMessage(**message)
                else:
                    msg = message
                await self.db_manager.save_chat_message(request.chat_id, request.user_id, msg, access_token)
            
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

    async def get_sandbox_info(self, chat_id: str, user_id: str, device_id: str) -> Dict[str, Any]:
        """Get sandbox information for a chat"""
        try:
            # Check session access
            # if not self.session_manager.is_session_active(chat_id, device_id):
            #     return {
            #         "success": False,
            #         "message": "No active session for this device"
            #     }
            
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

    async def terminate_sandbox(self, chat_id: str, user_id: str, device_id: str) -> APIResponse:
        """Manually terminate a sandbox"""
        try:
            # Check session access
            # if not self.session_manager.is_session_active(chat_id, device_id):
            #     return APIResponse(
            #         success=False,
            #         message="No active session for this device"
            #     )
            
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

    async def force_connect(self, request: ForceConnectRequest) -> Dict[str, Any]:
        """Force connect to a chat session (kick out other device)"""
        try:
            # Force connect
            self.session_manager.force_connect(request.chat_id, request.user_id, request.device_id)
            
            # Check if sandbox exists
            sandbox_info = await self.sandbox_manager.get_sandbox_info(request.chat_id)
            
            if sandbox_info:
                # Update activity
                await self.sandbox_manager.update_activity(request.chat_id)
                await self.db_manager.update_session_activity(request.chat_id, request.user_id)
                
                return {
                    "success": True,
                    "message": "Force connected successfully",
                    "sandbox": sandbox_info.dict()
                }
            else:
                # Need to create new sandbox
                return {
                    "success": True,
                    "message": "Force connected, sandbox needs to be created",
                    "needs_sandbox": True
                }
            
        except Exception as e:
            print(f"Error force connecting: {e}")
            return {
                "success": False,
                "message": f"Error force connecting: {str(e)}"
            }

    async def load_session_data(self, chat_id: str, user_id: str, access_token: str = None) -> Dict[str, Any]:
        """Load session data from database for sandbox initialization"""
        try:
            session_data = await self.db_manager.load_chat_session(chat_id, user_id, access_token)
            
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

    async def save_session_data(self, chat_id: str, user_id: str, chat_history: list, enabled_mcps: list) -> APIResponse:
        """Save session data to database"""
        try:
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

    async def chat_stream(self, chat_id: str, user_id: str, message: str, device_id: str, access_token: str = None) -> AsyncIterator[str]:
        """Handle streaming chat with sandbox"""
        try:
            # Check session access
            # if not self.session_manager.is_session_active(chat_id, device_id):
            #     yield f"data: {json.dumps({'error': 'No active session for this device'})}\n\n"
            #     return

            # Get sandbox info
            sandbox_info = await self.sandbox_manager.get_sandbox_info(chat_id)
            if not sandbox_info or not sandbox_info.url:
                yield f"data: {json.dumps({'error': 'No active sandbox found'})}\n\n"
                return

            # Save user message to database
            user_message = ChatMessage(role="user", content=message)
            await self.db_manager.save_chat_message(chat_id, user_id, user_message, access_token)

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
                    
                    # Stream response from sandbox - process chunks for download links
                    async for chunk in response.aiter_text():
                        if chunk.strip():
                            # Extract content for saving while processing
                            processed_chunk = ""
                            for line in chunk.split('\n'):
                                if line.startswith('data: '):
                                    try:
                                        data = json.loads(line[6:])
                                        if 'chunk' in data:
                                            chunk_content = data['chunk']
                                            assistant_content += chunk_content
                                            
                                            # Check for create_download_link pattern
                                            download_pattern = r'create_download_link\(([^)]+)\)'
                                            match = re.search(download_pattern, chunk_content)
                                            if match:
                                                filepath = match.group(1)
                                                try:
                                                    # Connect to sandbox and get download URL
                                                    sandbox = self.sandbox_manager.active_sandboxes[sandbox_info.sandbox_id]['sandbox']
                                                    print(f"Downloading file {filepath} from sandbox {sandbox_info.sandbox_id}")
                                                    signed_url = await sandbox.download_url(path=filepath)
                                                    # Replace the chunk with download URL
                                                    data['chunk'] = f"Download URL: {signed_url}"
                                                    processed_chunk += f"data: {json.dumps(data)}\n"
                                                except Exception as e:
                                                    print(f"Error creating download link: {e}")
                                                    data['chunk'] = f"Error creating download link: {str(e)}"
                                                    processed_chunk += f"data: {json.dumps(data)}\n"
                                            else:
                                                processed_chunk += line + "\n"
                                        else:
                                            processed_chunk += line + "\n"
                                    except Exception as e:
                                        print(f"Error processing chunk: {e}")
                                        processed_chunk += line + "\n"
                                else:
                                    processed_chunk += line + "\n"
                            yield processed_chunk if processed_chunk else chunk

                    # Save complete assistant response to database
                    if assistant_content.strip():
                        assistant_message = ChatMessage(role="assistant", content=assistant_content.strip())
                        await self.db_manager.save_chat_message(chat_id, user_id, assistant_message, access_token)

            yield f"data: {json.dumps({'done': True})}\n\n"

        except Exception as e:
            print(traceback.format_exc())
            print(f"Error in chat stream: {e}")
            yield f"data: {json.dumps({'error': f'Chat error: {str(e)}'})}\n\n"

    async def get_user_chat_sessions(self, user_id: str, access_token: str = None) -> Dict[str, Any]:
        """Get all chat sessions for a user"""
        try:
            sessions = await self.db_manager.get_user_chat_sessions(user_id, access_token)
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

    async def connect_chat(self, chat_id: str, user_id: str, device_id: str, access_token: str = None) -> Dict[str, Any]:
        """Connect to a chat - handles both new and existing chats"""
        try:
            # Load existing session data if it exists
            session_data = await self.db_manager.load_chat_session(chat_id, user_id, access_token)
            print(f"Connecting to chat {chat_id} for user {user_id} with device {device_id}")
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
                await self.db_manager.save_chat_session(chat_id, user_id, enabled_mcps, access_token)
            
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
                asyncio.create_task(self._create_sandbox_async(chat_id, user_id, access_token, chat_history, enabled_mcps))
                
                # Return immediately with pending status
                self.session_manager.force_connect(chat_id, user_id, device_id)
                return {
                    "success": True,
                    "chat_id": chat_id,
                    "sandbox_url": None,
                    "chat_history": chat_history,
                    "enabled_mcps": enabled_mcps,
                    "sandbox_status": "creating",
                    "message": "Sandbox is being created, please wait..."
                }
            
            # Force connect to establish session
            self.session_manager.force_connect(chat_id, user_id, device_id)
            
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

    async def _create_sandbox_async(self, chat_id: str, user_id: str, access_token: str, chat_history: list, enabled_mcps: list):
        """Create sandbox asynchronously"""
        try:
            sandbox_response = await self.sandbox_manager.create_sandbox(chat_id, user_id)
            if sandbox_response.status != "error":
                sandbox_url = sandbox_response.url
                # Update sandbox URL in database
                await self.db_manager.update_sandbox_url(chat_id, user_id, sandbox_url, access_token)
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

    async def check_sandbox_status(self, chat_id: str, user_id: str) -> Dict[str, Any]:
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

    async def create_mcp(self, name: str, command: str, args: List[str], title: str = None, description: str = None, env: Dict[str, str] = None, access_token: str = None) -> Dict[str, Any]:
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
            
            mcp = await self.db_manager.create_mcp(name, final_title, final_description, config, access_token)
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

    async def toggle_mcp_for_chat(self, chat_id: str, user_id: str, device_id: str, mcp_name: str, enabled: bool, config: dict = None, access_token: str = None) -> Dict[str, Any]:
        """Toggle MCP for a chat session and update session table"""
        try:
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
            session_data = await self.db_manager.load_chat_session(chat_id, user_id, access_token)
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
                await self.db_manager.save_chat_session(chat_id, user_id, enabled_mcps, access_token)
            
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