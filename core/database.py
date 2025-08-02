import os
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone, timedelta
from supabase import create_client, Client, ClientOptions
from dotenv import load_dotenv
import traceback

from models.schemas import ChatMessage, ChatSessionData

load_dotenv()

class DatabaseManager:
    def __init__(self):
        self.supabase_url = os.environ.get("SUPABASE_PROJECT_URL")
        self.supabase_key = os.environ.get("SUPABASE_KEY")
        self.schema = os.environ.get("SUPABASE_SCHEMA", "fleet_db")
        
        if not self.supabase_url or not self.supabase_key:
            raise ValueError("SUPABASE_PROJECT_URL and SUPABASE_KEY environment variables are required")
        
        self.client: Client = create_client(
            self.supabase_url, 
            self.supabase_key,
            options=ClientOptions(schema=self.schema)
        )

    def _get_authenticated_client(self, access_token: str = None):
        """Get Supabase client with user authentication if token provided"""
        if access_token:
            # Create authenticated client
            from supabase import create_client, ClientOptions
            client = create_client(
                self.supabase_url, 
                self.supabase_key,
                options=ClientOptions(schema=self.schema)
            )
            # Set the auth token (set_session expects access_token and refresh_token as separate args)
            client.auth.set_session(access_token, "")
            return client
        else:
            # Return default client
            return self.client

    async def save_chat_session(self, chat_id: str, user_id: str, enabled_mcps: List[Dict[str, Any]], access_token: str = None):
        """Save or update chat session data (MCP config only)"""
        try:
            client = self._get_authenticated_client(access_token)
            session_data = {
                "chat_id": chat_id,
                "user_id": user_id,
                "enabled_mcps": enabled_mcps,
                "sandbox_url": None,  # Will be updated when sandbox is created
                "last_activity": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat()
            }
            
            # Upsert (insert or update) the session
            result = client.table("chat_sessions").upsert(
                session_data,
                on_conflict="chat_id"
            ).execute()
            
            return result.data[0] if result.data else None
            
        except Exception as e:
            print(traceback.format_exc())
            print(f"Error saving chat session {chat_id}: {e}")
            return None

    async def save_chat_message(self, chat_id: str, user_id: str, message: ChatMessage, access_token: str = None):
        """Save a single chat message to the database"""
        try:
            client = self._get_authenticated_client(access_token)
            message_data = {
                "chat_id": chat_id,
                "user_id": user_id,
                "message": message.model_dump(),
                "created_at": datetime.now(timezone.utc).isoformat()
            }
            
            result = client.table("chat_messages").insert(message_data).execute()
            return result.data[0] if result.data else None
            
        except Exception as e:
            print(f"Error saving chat message: {e}")
            return None

    async def load_chat_session(self, chat_id: str, user_id: str, access_token: str = None) -> Optional[ChatSessionData]:
        """Load chat session data from database"""
        try:
            client = self._get_authenticated_client(access_token)
            
            # Get session data
            session_result = client.table("chat_sessions").select("*").eq("chat_id", chat_id).eq("user_id", user_id).execute()
            
            if not session_result.data:
                return None
            
            session = session_result.data[0]
            
            # Get chat messages from messages table
            messages_result = client.table("chat_messages").select("message").eq("chat_id", chat_id).eq("user_id", user_id).order("created_at", desc=False).execute()
            
            # Convert message dicts to ChatMessage objects
            chat_history = []
            for msg_row in messages_result.data:
                message_dict = msg_row["message"]
                chat_history.append(ChatMessage(**message_dict))
            
            return ChatSessionData(
                chat_id=session["chat_id"],
                user_id=session["user_id"],
                chat_history=chat_history,
                enabled_mcps=session.get("enabled_mcps", []),
                last_activity=session.get("last_activity", ""),
                sandbox_url=session.get("sandbox_url")
            )
            
        except Exception as e:
            print(f"Error loading chat session {chat_id}: {e}")
            return None


    async def get_chat_messages(self, chat_id: str, user_id: str, limit: int = 100) -> List[ChatMessage]:
        """Get chat messages for a session"""
        try:
            result = self.client.table("chat_messages").select("message").eq("chat_id", chat_id).eq("user_id", user_id).order("created_at", desc=False).limit(limit).execute()
            
            messages = []
            for msg_row in result.data:
                message_dict = msg_row["message"]
                messages.append(ChatMessage(**message_dict))
            
            return messages
            
        except Exception as e:
            print(f"Error getting chat messages for {chat_id}: {e}")
            return []

    async def update_session_activity(self, chat_id: str, user_id: str):
        """Update last activity timestamp for a session"""
        try:
            result = self.client.table("chat_sessions").update({
                "last_activity": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat()
            }).eq("chat_id", chat_id).eq("user_id", user_id).execute()
            
            return result.data[0] if result.data else None
            
        except Exception as e:
            print(f"Error updating session activity for {chat_id}: {e}")
            return None

    async def update_sandbox_url(self, chat_id: str, user_id: str, sandbox_url: str, access_token: str = None):
        """Update sandbox URL for a session"""
        try:
            client = self._get_authenticated_client(access_token)
            result = client.table("chat_sessions").update({
                "sandbox_url": sandbox_url,
                "last_activity": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat()
            }).eq("chat_id", chat_id).eq("user_id", user_id).execute()
            
            return result.data[0] if result.data else None
            
        except Exception as e:
            print(f"Error updating sandbox URL for {chat_id}: {e}")
            return None

    async def delete_chat_session(self, chat_id: str, user_id: str):
        """Delete a chat session and all its messages"""
        try:
            # Delete messages first
            self.client.table("chat_messages").delete().eq("chat_id", chat_id).eq("user_id", user_id).execute()
            
            # Delete session
            result = self.client.table("chat_sessions").delete().eq("chat_id", chat_id).eq("user_id", user_id).execute()
            
            return True
            
        except Exception as e:
            print(f"Error deleting chat session {chat_id}: {e}")
            return False

    async def get_user_chat_sessions(self, user_id: str, access_token: str = None) -> List[Dict[str, Any]]:
        """Get all chat sessions for a user"""
        try:
            client = self._get_authenticated_client(access_token)
            result = client.table("chat_sessions").select("chat_id, last_activity, updated_at").eq("user_id", user_id).order("updated_at", desc=True).execute()
            
            return result.data
            
        except Exception as e:
            print(f"Error getting chat sessions for user {user_id}: {e}")
            return []

    async def cleanup_old_sessions(self, days_old: int = 30):
        """Clean up old inactive sessions"""
        try:
            cutoff_date = datetime.now(timezone.utc) - timedelta(days=days_old)
            
            # Get old sessions
            result = self.client.table("chat_sessions").select("chat_id, user_id").lt("last_activity", cutoff_date.isoformat()).execute()
            
            # Delete old sessions and their messages
            for session in result.data:
                await self.delete_chat_session(session["chat_id"], session["user_id"])
                
            return len(result.data)
            
        except Exception as e:
            print(f"Error cleaning up old sessions: {e}")
            return 0

    # MCP Management Methods
    async def get_all_mcps(self) -> List[Dict[str, Any]]:
        """Get all available MCPs"""
        try:
            result = self.client.table("mcps").select("*").order("name").execute()
            return result.data
            
        except Exception as e:
            print(traceback.format_exc())
            print(f"Error getting MCPs: {e}")
            return []

    async def create_mcp(self, name: str, title: str, description: str, config: Dict[str, Any], access_token: str = None) -> Optional[Dict[str, Any]]:
        """Create a new MCP"""
        try:
            client = self._get_authenticated_client(access_token)
            mcp_data = {
                "name": name,
                "title": title,
                "description": description,
                "config": config
            }
            
            result = client.table("mcps").insert(mcp_data).execute()
            return result.data[0] if result.data else None
            
        except Exception as e:
            print(f"Error creating MCP {name}: {e}")
            return None

    async def get_mcp_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Get MCP by name"""
        try:
            result = self.client.table("mcps").select("*").eq("name", name).execute()
            return result.data[0] if result.data else None
            
        except Exception as e:
            print(traceback.format_exc())
            print(f"Error getting MCP {name}: {e}")
            return None