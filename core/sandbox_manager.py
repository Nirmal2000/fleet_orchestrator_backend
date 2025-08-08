import os
import asyncio
import httpx
from typing import Dict, Optional
from datetime import datetime, timedelta
from dotenv import load_dotenv
from e2b_code_interpreter import AsyncSandbox

from models.schemas import SandboxStatus, SandboxResponse

load_dotenv()

class SandboxManager:
    def __init__(self):
        # Check if we're in local testing mode
        self.local_testing = os.environ.get("LOCAL_TESTING", "false").lower() == "true"
        self.local_chatbot_url = os.environ.get("LOCAL_CHATBOT_URL", "http://localhost:3000")
        
        if not self.local_testing:
            self.template_id = os.environ.get("E2B_TEMPLATE_ID")
            if not self.template_id:
                raise ValueError("E2B_TEMPLATE_ID environment variable is required when LOCAL_TESTING=false")
        
        self.active_sandboxes: Dict[str, Dict] = {}  # chat_id -> sandbox_info
        self.inactivity_timers: Dict[str, asyncio.Task] = {}  # chat_id -> timer_task
        self.INACTIVITY_TIMEOUT = 20*60  # 10 minutes in seconds
        
        # Prepare environment variables for sandbox (only used in E2B mode)
        self.sandbox_envs = {
            'E2B_TEMPLATE_ID': os.environ.get("E2B_TEMPLATE_ID", ""),
            'E2B_API_KEY': os.environ.get("E2B_API_KEY", ""),
            'OPENAI_API_KEY': os.environ.get("OPENAI_API_KEY", ""),
            'GOOGLE_API_KEY': os.environ.get("GOOGLE_API_KEY", ""),
            'SUPABASE_PROJECT_URL': os.environ.get("SUPABASE_PROJECT_URL", ""),
            'SUPABASE_KEY': os.environ.get("SUPABASE_KEY", ""),
            'SUPABASE_SCHEMA': os.environ.get("SUPABASE_SCHEMA", "fleet_db"),
        }

    async def create_sandbox(self, chat_id: str, user_id: str) -> SandboxResponse:
        """Create a new E2B sandbox for a chat session"""
        try:
            # Kill existing sandbox if any
            if chat_id in self.active_sandboxes:
                await self.terminate_sandbox(chat_id)
            
            # Create new sandbox with environment variables or use local testing
            if self.local_testing:
                # Local testing mode - use localhost
                sandbox_url = self.local_chatbot_url
                sandbox = None  # No actual sandbox in local mode
                print(f"LOCAL TESTING: Using {sandbox_url} for chat {chat_id}")
            else:
                # E2B mode - create actual sandbox
                sandbox = await AsyncSandbox.create(self.template_id, envs=self.sandbox_envs, timeout=3000, api_key=self.sandbox_envs['E2B_API_KEY'])
                await sandbox.commands.run("mkdir -p /tmp/pw", background=False)
                await sandbox.commands.run("cd /app && nohup python main.py > /tmp/app.log 2>&1 &", background=True)
                host = sandbox.get_host(3000)
                sandbox_url = f"https://{host}"
                print(f"Created sandbox for chat {chat_id} at {sandbox_url}")
                # loop until sandbox_url/heath is ready
                async with httpx.AsyncClient(timeout=30.0) as client:
                    while True:
                        try:
                            print(f"Waiting for sandbox {chat_id} to be ready at {sandbox_url}/health")
                            response = await client.get(f"{sandbox_url}/health")
                            if response.status_code == 200:
                                break
                        except httpx.RequestError:
                            pass
                        await asyncio.sleep(5)
            
            # Store sandbox info
            created_at = datetime.utcnow()
            expires_at = created_at + timedelta(seconds=self.INACTIVITY_TIMEOUT)
            
            sandbox_info = {
                "sandbox": sandbox,
                "chat_id": chat_id,
                "user_id": user_id,
                "url": sandbox_url,
                "status": SandboxStatus.ACTIVE,
                "created_at": created_at,
                "expires_at": expires_at,
                "last_activity": created_at
            }
            
            self.active_sandboxes[chat_id] = sandbox_info
            
            # Initialize the chatbot with the correct session ID
            await self._initialize_chatbot(sandbox_url, chat_id)
            
            # Start inactivity timer
            await self._start_inactivity_timer(chat_id)
            
            return SandboxResponse(
                sandbox_id=chat_id,  # Using chat_id as sandbox_id
                chat_id=chat_id,
                status=SandboxStatus.ACTIVE,
                url=sandbox_url,
                created_at=created_at.isoformat(),
                expires_at=expires_at.isoformat()
            )
            
        except Exception as e:
            print(f"Error creating sandbox for chat {chat_id}: {e}")
            return SandboxResponse(
                sandbox_id=chat_id,
                chat_id=chat_id,
                status=SandboxStatus.ERROR,
                created_at=datetime.utcnow().isoformat()
            )

    async def get_sandbox_info(self, chat_id: str) -> Optional[SandboxResponse]:
        """Get information about an existing sandbox"""
        if chat_id not in self.active_sandboxes:
            return None
        
        info = self.active_sandboxes[chat_id]
        return SandboxResponse(
            sandbox_id=chat_id,
            chat_id=chat_id,
            status=info["status"],
            url=info["url"],
            created_at=info["created_at"].isoformat(),
            expires_at=info["expires_at"].isoformat()
        )

    async def terminate_sandbox(self, chat_id: str) -> bool:
        """Terminate a sandbox and clean up resources"""
        try:
            if chat_id in self.active_sandboxes:
                sandbox_info = self.active_sandboxes[chat_id]
                print(f"Terminating sandbox for chat {chat_id} at {sandbox_info}")
                # Kill the sandbox (only if not local testing)
                if not self.local_testing and sandbox_info["sandbox"]:
                    print(f"Terminating sandbox for chat {chat_id} at {sandbox_info['url']}")
                    await sandbox_info["sandbox"].kill()
                
                # Cancel inactivity timer
                if chat_id in self.inactivity_timers:
                    self.inactivity_timers[chat_id].cancel()
                    del self.inactivity_timers[chat_id]
                
                # Remove from active sandboxes
                del self.active_sandboxes[chat_id]
                
                print(f"Terminated sandbox for chat {chat_id}")
                return True
            
            return False
            
        except Exception as e:
            print(f"Error terminating sandbox for chat {chat_id}: {e}")
            return False

    async def update_activity(self, chat_id: str):
        """Update last activity time and reset inactivity timer"""
        if chat_id in self.active_sandboxes:
            self.active_sandboxes[chat_id]["last_activity"] = datetime.utcnow()
            
            # Reset inactivity timer
            await self._start_inactivity_timer(chat_id)

    async def _start_inactivity_timer(self, chat_id: str):
        """Start or restart the inactivity timer for a sandbox"""
        # Cancel existing timer
        if chat_id in self.inactivity_timers:
            self.inactivity_timers[chat_id].cancel()
        
        # Create new timer
        async def inactivity_callback():
            try:
                await asyncio.sleep(self.INACTIVITY_TIMEOUT)
                print(f"Sandbox {chat_id} expired due to inactivity")
                await self.terminate_sandbox(chat_id)
            except asyncio.CancelledError:
                # Timer was cancelled (normal behavior)
                pass
        
        self.inactivity_timers[chat_id] = asyncio.create_task(inactivity_callback())

    def is_sandbox_active(self, chat_id: str) -> bool:
        """Check if a sandbox is currently active"""
        return chat_id in self.active_sandboxes and \
               self.active_sandboxes[chat_id]["status"] == SandboxStatus.ACTIVE

    def get_sandbox_url(self, chat_id: str) -> Optional[str]:
        """Get the URL for an active sandbox"""
        if self.is_sandbox_active(chat_id):
            return self.active_sandboxes[chat_id]["url"]
        return None

    def list_active_sandboxes(self) -> Dict[str, Dict]:
        """Get list of all active sandboxes"""
        return {
            chat_id: {
                "chat_id": info["chat_id"],
                "user_id": info["user_id"],
                "status": info["status"],
                "url": info["url"],
                "created_at": info["created_at"].isoformat(),
                "last_activity": info["last_activity"].isoformat()
            }
            for chat_id, info in self.active_sandboxes.items()
        }

    async def _initialize_chatbot(self, sandbox_url: str, chat_id: str):
        """Initialize the FastAPI chatbot with the correct session ID"""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{sandbox_url}/initialize",
                    json={
                        "chat_history": [],
                        "enabled_mcps": [],
                        "session_id": chat_id  # This ensures session_id matches chat_id
                    },
                    headers={"Content-Type": "application/json"}
                )
                
                if response.status_code == 200:
                    print(f"Successfully initialized chatbot for chat {chat_id}")
                else:
                    print(f"Failed to initialize chatbot for chat {chat_id}: {response.status_code}")
                    
        except Exception as e:
            print(f"Error initializing chatbot for chat {chat_id}: {e}")

    async def cleanup_all(self):
        """Clean up all sandboxes and timers"""
        chat_ids = list(self.active_sandboxes.keys())
        for chat_id in chat_ids:
            await self.terminate_sandbox(chat_id)