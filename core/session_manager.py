from typing import Dict, Optional, List
from datetime import datetime

from models.schemas import ChatSessionData

class SessionManager:
    def __init__(self):
        self.active_sessions: Dict[str, Dict] = {}  # chat_id -> session_info
        
    def create_session(self, chat_id: str, user_id: str) -> bool:
        # Create or update session
        self.active_sessions[chat_id] = {
            "user_id": user_id,            
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
    
    # Device-specific session checks removed; session activity is tracked per chat_id
