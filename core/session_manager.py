from typing import Dict, Optional, List
from datetime import datetime

from models.schemas import ChatSessionData, SessionConflictResponse

class SessionManager:
    def __init__(self):
        self.active_sessions: Dict[str, Dict] = {}  # chat_id -> session_info
        
    def create_session(self, chat_id: str, user_id: str, device_id: str) -> bool:
        """Create a new session or check for conflicts"""
        if chat_id in self.active_sessions:
            current_device = self.active_sessions[chat_id]["device_id"]
            if current_device != device_id:
                # Session conflict
                return False
        
        # Create or update session
        self.active_sessions[chat_id] = {
            "user_id": user_id,
            "device_id": device_id,
            "created_at": datetime.utcnow(),
            "last_activity": datetime.utcnow()
        }
        return True
    
    def check_session_conflict(self, chat_id: str, device_id: str) -> SessionConflictResponse:
        """Check if there's a session conflict"""
        if chat_id not in self.active_sessions:
            return SessionConflictResponse(
                conflict=False,
                message="No active session"
            )
        
        current_device = self.active_sessions[chat_id]["device_id"]
        if current_device == device_id:
            return SessionConflictResponse(
                conflict=False,
                message="Same device"
            )
        
        return SessionConflictResponse(
            conflict=True,
            current_device=current_device,
            message=f"Chat is currently active on device {current_device}"
        )
    
    def force_connect(self, chat_id: str, user_id: str, device_id: str) -> bool:
        """Force connect to a session (kick out other device)"""
        self.active_sessions[chat_id] = {
            "user_id": user_id,
            "device_id": device_id,
            "created_at": datetime.utcnow(),
            "last_activity": datetime.utcnow()
        }
        return True
    
    def update_activity(self, chat_id: str):
        """Update last activity for a session"""
        if chat_id in self.active_sessions:
            self.active_sessions[chat_id]["last_activity"] = datetime.utcnow()
    
    def remove_session(self, chat_id: str):
        """Remove a session"""
        if chat_id in self.active_sessions:
            del self.active_sessions[chat_id]
    
    def get_session_device(self, chat_id: str) -> Optional[str]:
        """Get the device ID for an active session"""
        if chat_id in self.active_sessions:
            return self.active_sessions[chat_id]["device_id"]
        return None
    
    def is_session_active(self, chat_id: str, device_id: str) -> bool:
        """Check if a session is active for a specific device"""
        if chat_id not in self.active_sessions:
            return False
        return self.active_sessions[chat_id]["device_id"] == device_id