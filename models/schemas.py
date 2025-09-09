from typing import List, Dict, Any, Optional
from pydantic import BaseModel
from enum import Enum

class SandboxStatus(str, Enum):
    CREATING = "creating"
    ACTIVE = "active"
    INACTIVE = "inactive"
    EXPIRED = "expired"
    ERROR = "error"

class ChatMessage(BaseModel):
    role: str
    content: str

class MCPConfig(BaseModel):
    command: str
    args: List[str]
    env: Optional[Dict[str, str]] = None

class CreateSandboxRequest(BaseModel):
    chat_id: str
    chat_history: List[ChatMessage] = []
    enabled_mcps: List[Dict[str, Any]] = []  # List of {name: str, config: MCPConfig}

class SandboxResponse(BaseModel):
    sandbox_id: str
    chat_id: str
    status: SandboxStatus
    url: Optional[str] = None
    created_at: str
    expires_at: Optional[str] = None

class ChatSessionData(BaseModel):
    chat_id: str
    user_id: str
    chat_history: List[ChatMessage]
    enabled_mcps: List[Dict[str, Any]]
    last_activity: str
    sandbox_url: Optional[str] = None

# Deprecated models related to device-level session control have been removed

class APIResponse(BaseModel):
    success: bool
    message: str
    data: Optional[Dict[str, Any]] = None
