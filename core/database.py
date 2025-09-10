import os
import jwt
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
        self.supabase_key = os.environ.get("SUPABASE_KEY")            # anon (or service); SDK requires a key
        self.supabase_jwt_secret = os.environ.get("SUPABASE_JWT_SECRET")
        self.schema = os.environ.get("SUPABASE_SCHEMA", "fleet_descope")

        if not self.supabase_url or not self.supabase_key:
            raise ValueError("SUPABASE_PROJECT_URL and SUPABASE_KEY environment variables are required")
        if not self.supabase_jwt_secret:
            raise ValueError("SUPABASE_JWT_SECRET environment variable is required")

        # Base client (no user Authorization header). Use ONLY for public tables.
        self.client: Client = create_client(self.supabase_url, self.supabase_key)

    def mint_supabase_jwt(self, user_id: str) -> str:
        """Mint a Supabase JWT for user authentication."""
        payload = {
            "sub": user_id,
            "role": "authenticated",
            "exp": datetime.utcnow() + timedelta(hours=1),
        }
        return jwt.encode(payload, self.supabase_jwt_secret, algorithm="HS256")

    def _get_authenticated_client(self, user_id: Optional[str]) -> Client:
        """
        Return a Supabase client that sends Authorization: Bearer <user_jwt>.
        Required for any RLS-protected table access.
        """
        if not user_id:
            # Only safe for truly public tables
            return self.client

        supabase_jwt = self.mint_supabase_jwt(user_id)
        return create_client(
            self.supabase_url,
            self.supabase_key,
            options=ClientOptions(headers={"Authorization": f"Bearer {supabase_jwt}"})
        )

    # -------------------
    # Chat Session Methods
    # -------------------

    async def save_chat_session(self, chat_id: str, user_id: str, enabled_mcps: List[Dict[str, Any]]):
        try:
            sc = self._get_authenticated_client(user_id).schema(self.schema)
            session_data = {
                "chat_id": chat_id,
                "user_id": user_id,
                "enabled_mcps": enabled_mcps,
                "sandbox_url": None,
                "last_activity": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            result = sc.table("chat_sessions").upsert(session_data, on_conflict="chat_id").execute()
            return result.data[0] if result.data else None
        except Exception as e:
            print(traceback.format_exc())
            print(f"Error saving chat session {chat_id}: {e}")
            return None

    async def save_chat_message(self, chat_id: str, user_id: str, message: ChatMessage):
        try:
            sc = self._get_authenticated_client(user_id).schema(self.schema)
            message_data = {
                "chat_id": chat_id,
                "user_id": user_id,
                "message": message.model_dump(),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            result = sc.table("chat_messages").insert(message_data).execute()
            return result.data[0] if result.data else None
        except Exception as e:
            print(f"Error saving chat message: {e}")
            return None

    async def load_chat_session(self, chat_id: str, user_id: str) -> Optional[ChatSessionData]:
        try:
            sc = self._get_authenticated_client(user_id).schema(self.schema)

            session_result = sc.table("chat_sessions").select("*").eq("chat_id", chat_id).execute()
            if not session_result.data:
                return None
            session = session_result.data[0]

            messages_result = (
                sc.table("chat_messages")
                .select("message")
                .eq("chat_id", chat_id)
                .order("created_at", desc=False)
                .execute()
            )
            chat_history = [ChatMessage(**row["message"]) for row in messages_result.data]

            return ChatSessionData(
                chat_id=session["chat_id"],
                user_id=session["user_id"],
                chat_history=chat_history,
                enabled_mcps=session.get("enabled_mcps", []),
                last_activity=session.get("last_activity", ""),
                sandbox_url=session.get("sandbox_url"),
            )
        except Exception as e:
            print(f"Error loading chat session {chat_id}: {e}")
            return None

    async def get_chat_messages(self, chat_id: str, user_id: str, limit: int = 100) -> List[ChatMessage]:
        try:
            sc = self._get_authenticated_client(user_id).schema(self.schema)
            result = (
                sc.table("chat_messages")
                .select("message")
                .eq("chat_id", chat_id)
                .order("created_at", desc=False)
                .limit(limit)
                .execute()
            )
            return [ChatMessage(**row["message"]) for row in result.data]
        except Exception as e:
            print(f"Error getting chat messages for {chat_id}: {e}")
            return []

    async def update_session_activity(self, chat_id: str, user_id: str):
        try:
            sc = self._get_authenticated_client(user_id).schema(self.schema)
            result = (
                sc.table("chat_sessions")
                .update({
                    "last_activity": datetime.now(timezone.utc).isoformat(),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })
                .eq("chat_id", chat_id)
                .execute()
            )
            return result.data[0] if result.data else None
        except Exception as e:
            print(f"Error updating session activity for {chat_id}: {e}")
            return None

    async def update_sandbox_url(self, chat_id: str, user_id: str, sandbox_url: str):
        try:
            sc = self._get_authenticated_client(user_id).schema(self.schema)
            result = (
                sc.table("chat_sessions")
                .update({
                    "sandbox_url": sandbox_url,
                    "last_activity": datetime.now(timezone.utc).isoformat(),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })
                .eq("chat_id", chat_id)
                .execute()
            )
            return result.data[0] if result.data else None
        except Exception as e:
            print(f"Error updating sandbox URL for {chat_id}: {e}")
            return None

    async def delete_chat_session(self, chat_id: str, user_id: str):
        try:
            sc = self._get_authenticated_client(user_id).schema(self.schema)
            sc.table("chat_messages").delete().eq("chat_id", chat_id).execute()
            sc.table("chat_sessions").delete().eq("chat_id", chat_id).execute()
            return True
        except Exception as e:
            print(f"Error deleting chat session {chat_id}: {e}")
            return False

    async def get_user_chat_sessions(self, user_id: str) -> List[Dict[str, Any]]:
        try:
            sc = self._get_authenticated_client(user_id).schema(self.schema)
            result = (
                sc.table("chat_sessions")
                .select("chat_id, last_activity, updated_at")
                .order("updated_at", desc=True)
                .execute()
            )
            return result.data
        except Exception as e:
            print(f"Error getting chat sessions for user {user_id}: {e}")
            return []

    async def cleanup_old_sessions(self, days_old: int = 30, service_key: Optional[str] = None):
        """
        Cleanup requires broader access. Either:
        - Pass a SERVICE_ROLE key (bypasses RLS) to run globally, OR
        - Call per-user with authenticated client (slower, needs list of users).
        """
        try:
            cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()

            if service_key:
                # Service-role for cross-user maintenance
                svc = create_client(self.supabase_url, service_key)
                sc_svc = svc.schema(self.schema)
                result = sc_svc.table("chat_sessions").select("chat_id").lt("last_activity", cutoff_iso).execute()
                for row in result.data:
                    sc_svc.table("chat_messages").delete().eq("chat_id", row["chat_id"]).execute()
                    sc_svc.table("chat_sessions").delete().eq("chat_id", row["chat_id"]).execute()
                return len(result.data)

            print("cleanup_old_sessions: supply service_key for global cleanup.")
            return 0

        except Exception as e:
            print(f"Error cleaning up old sessions: {e}")
            return 0

    # -------------------
    # Task Tracking Methods (public tables via anon)
    # -------------------

    async def create_task(self, name: str, input_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            sc = self.client.schema(self.schema)
            payload = {
                "name": name,
                "status": "pending",
                "progress": "starting",
                "input": input_data,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            res = sc.table("mcp_tasks").insert(payload).execute()
            return res.data[0] if res.data else None
        except Exception as e:
            print(f"Error creating task: {e}")
            return None

    async def update_task_progress(self, task_id: str, progress: str, status: Optional[str] = None) -> bool:
        try:
            sc = self.client.schema(self.schema)
            payload = {"progress": progress, "updated_at": datetime.now(timezone.utc).isoformat()}
            if status:
                payload["status"] = status
            sc.table("mcp_tasks").update(payload).eq("id", task_id).execute()
            return True
        except Exception as e:
            print(f"Error updating task {task_id} progress: {e}")
            return False

    async def complete_task(self, task_id: str, status: str, result: Dict[str, Any]) -> bool:
        try:
            sc = self.client.schema(self.schema)
            sc.table("mcp_tasks").update({
                "status": status,
                "result": result,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", task_id).execute()
            return True
        except Exception as e:
            sc = self.client.schema(self.schema)
            sc.table("mcp_tasks").update({
                "status": "failed",
                "result": result,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", task_id).execute()
            print(f"Error completing task {task_id}: {e}")
            return False

    async def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        try:
            sc = self.client.schema(self.schema)
            res = sc.table("mcp_tasks").select("*").eq("id", task_id).limit(1).execute()
            return res.data[0] if res.data else None
        except Exception as e:
            print(f"Error getting task {task_id}: {e}")
            return None

    # -------------------
    # MCP Management
    # -------------------

    async def get_all_mcps(self) -> List[Dict[str, Any]]:
        try:
            # If mcps is public, self.client is fine; otherwise use authenticated client.
            sc = self.client.schema(self.schema)
            result = sc.table("mcps").select("*").order("name").execute()
            return result.data
        except Exception as e:
            print(traceback.format_exc())
            print(f"Error getting MCPs: {e}")
            return []

    async def create_mcp(self, name: str, title: str, description: str, config: Dict[str, Any], user_id: str) -> Optional[Dict[str, Any]]:
        try:
            # mcps table is public via anon with RLS policies; use anon client to avoid user RLS issues
            sc = self.client.schema(self.schema)
            payload = {
                "name": name,
                "title": title,
                "description": description,
                "config": config,
                "user_id": user_id,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            result = sc.table("mcps").upsert(payload, on_conflict="name").execute()
            return result.data[0] if result.data else None
        except Exception as e:
            print(f"Error creating MCP {name}: {e}")
            return None

    async def get_mcp_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        try:
            sc = self.client.schema(self.schema)
            result = sc.table("mcps").select("*").eq("name", name).execute()
            return result.data[0] if result.data else None
        except Exception as e:
            print(traceback.format_exc())
            print(f"Error getting MCP {name}: {e}")
            return None

    async def get_mcp_by_id(self, mcp_id: str) -> Optional[Dict[str, Any]]:
        try:
            sc = self.client.schema(self.schema)
            result = sc.table("mcps").select("*").eq("id", mcp_id).limit(1).execute()
            return result.data[0] if result.data else None
        except Exception as e:
            print(traceback.format_exc())
            print(f"Error getting MCP by id {mcp_id}: {e}")
            return None

    async def update_mcp_env_by_id(self, mcp_id: str, new_env: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            sc = self.client.schema(self.schema)
            # Fetch current config
            res = sc.table("mcps").select("config").eq("id", mcp_id).limit(1).execute()
            if not res.data:
                return None
            cfg = res.data[0].get("config") or {}
            cfg["env"] = new_env or {}
            upd = sc.table("mcps").update({
                "config": cfg,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", mcp_id).execute()
            return upd.data[0] if upd.data else None
        except Exception as e:
            print(traceback.format_exc())
            print(f"Error updating MCP env {mcp_id}: {e}")
            return None

    async def update_mcp_visibility_by_id(self, mcp_id: str, visibility: str) -> Optional[Dict[str, Any]]:
        try:
            if visibility not in ("public", "private"):
                return None
            sc = self.client.schema(self.schema)
            res = sc.table("mcps").select("config").eq("id", mcp_id).limit(1).execute()
            if not res.data:
                return None
            cfg = res.data[0].get("config") or {}
            meta = cfg.get("metadata") or {}
            meta["visibility"] = visibility
            cfg["metadata"] = meta
            upd = sc.table("mcps").update({
                "config": cfg,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", mcp_id).execute()
            return upd.data[0] if upd.data else None
        except Exception as e:
            print(traceback.format_exc())
            print(f"Error updating MCP visibility {mcp_id}: {e}")
            return None

    async def update_mcp_metadata_by_id(self, mcp_id: str, metadata_updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Merge/patch config.metadata with provided keys and persist."""
        try:
            sc = self.client.schema(self.schema)
            res = sc.table("mcps").select("config").eq("id", mcp_id).limit(1).execute()
            if not res.data:
                return None
            cfg = res.data[0].get("config") or {}
            meta = cfg.get("metadata") or {}
            meta.update(metadata_updates or {})
            cfg["metadata"] = meta
            upd = sc.table("mcps").update({
                "config": cfg,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", mcp_id).execute()
            return upd.data[0] if upd.data else None
        except Exception as e:
            print(traceback.format_exc())
            print(f"Error updating MCP metadata {mcp_id}: {e}")
            return None

    # -------------------
    # Client MCP (per-user settings)
    # -------------------

    async def upsert_client_mcp(self, user_id: str, mcp_id: str, mcp_env_variables: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            sc = self._get_authenticated_client(user_id).schema(self.schema)
            payload = {
                "user_id": user_id,
                "mcp_id": mcp_id,
                "mcp_env_variables": mcp_env_variables or {},
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            res = sc.table("client_mcps").upsert(payload, on_conflict="user_id,mcp_id").execute()
            return res.data[0] if res.data else None
        except Exception as e:
            print(f"Error upserting client_mcp for user {user_id}, mcp {mcp_id}: {e}")
            return None

    async def get_client_mcp(self, user_id: str, mcp_id: str) -> Optional[Dict[str, Any]]:
        try:
            sc = self._get_authenticated_client(user_id).schema(self.schema)
            res = sc.table("client_mcps").select("*").eq("user_id", user_id).eq("mcp_id", mcp_id).limit(1).execute()
            return res.data[0] if res.data else None
        except Exception as e:
            print(f"Error fetching client_mcp for user {user_id}, mcp {mcp_id}: {e}")
            return None

    async def save_client_mcp_access_token(self, user_id: str, mcp_id: str, access_token: str) -> Optional[Dict[str, Any]]:
        """Persist inbound access_token for a user+MCP. Requires column client_mcps.access_token."""
        try:
            sc = self._get_authenticated_client(user_id).schema(self.schema)
            payload = {
                "user_id": user_id,
                "mcp_id": mcp_id,
                "access_token": access_token,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            res = sc.table("client_mcps").upsert(payload, on_conflict="user_id,mcp_id").execute()
            return res.data[0] if res.data else None
        except Exception as e:
            print(f"Error saving inbound access token for user {user_id}, mcp {mcp_id}: {e}")
            return None

    async def find_mcp_by_client_id(self, client_id: str) -> Optional[Dict[str, Any]]:
        """Find MCP row whose config.metadata.inbound_app.clientId == client_id. Fallback client-side filtering."""
        try:
            sc = self.client.schema(self.schema)
            # Fetch minimal fields and filter in Python for portability
            res = sc.table("mcps").select("id, name, config").execute()
            for row in (res.data or []):
                cfg = (row or {}).get("config") or {}
                meta = (cfg.get("metadata") or {})
                inbound = (meta.get("inbound_app") or {})
                if (inbound.get("clientId") or "") == client_id:
                    return row
            return None
        except Exception as e:
            print(f"Error finding MCP by clientId: {e}")
            return None
