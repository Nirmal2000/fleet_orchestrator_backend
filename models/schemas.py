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
    # Text content for user/assistant messages or tool output
    content: Optional[str] = None
    # OR-format tool linkage
    tool_call_id: Optional[str] = None
    name: Optional[str] = None  # tool name for role='tool'
    # Assistant tool calls (when assistant triggers tools)
    tool_calls: Optional[List[Dict[str, Any]]] = None

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
