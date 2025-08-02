from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from typing import Optional, List
import uuid

from core.sandbox_manager import SandboxManager
from core.session_manager import SessionManager
from core.database import DatabaseManager
from api.endpoints import OrchestratorEndpoints
from models.schemas import (
    CreateSandboxRequest,
    ForceConnectRequest,
    APIResponse
)

app = FastAPI(title="Sandbox Orchestrator API", version="1.0.0")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure this properly for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global instances
sandbox_manager = None
session_manager = None
db_manager = None
endpoints = None

@app.on_event("startup")
async def startup_event():
    global sandbox_manager, session_manager, db_manager, endpoints
    sandbox_manager = SandboxManager()
    session_manager = SessionManager()
    db_manager = DatabaseManager()
    endpoints = OrchestratorEndpoints(sandbox_manager, session_manager, db_manager)

@app.on_event("shutdown")
async def shutdown_event():
    if sandbox_manager:
        await sandbox_manager.cleanup_all()

def get_device_id(x_device_id: Optional[str] = Header(None)) -> str:
    """Get device ID from header or generate one"""
    if x_device_id:
        return x_device_id
    return str(uuid.uuid4())

# Endpoints
@app.post("/create-sandbox")
async def create_sandbox(
    request: CreateSandboxRequest,
    device_id: str = Header(..., alias="X-Device-ID"),
    authorization: Optional[str] = Header(None)
):
    """Create a new sandbox for a chat session"""
    if not endpoints:
        raise HTTPException(status_code=500, detail="Service not initialized")
    
    # Extract access token from Authorization header
    access_token = None
    if authorization and authorization.startswith("Bearer "):
        access_token = authorization.split(" ")[1]
    
    return await endpoints.create_sandbox(request, device_id, access_token)

@app.get("/sandbox/{chat_id}")
async def get_sandbox_info(
    chat_id: str,
    user_id: str,
    device_id: str = Header(..., alias="X-Device-ID")
):
    """Get sandbox information for a chat"""
    if not endpoints:
        raise HTTPException(status_code=500, detail="Service not initialized")
    
    return await endpoints.get_sandbox_info(chat_id, user_id, device_id)

@app.delete("/sandbox/{chat_id}")
async def terminate_sandbox(
    chat_id: str,
    user_id: str,
    device_id: str = Header(..., alias="X-Device-ID")
):
    """Manually terminate a sandbox"""
    if not endpoints:
        raise HTTPException(status_code=500, detail="Service not initialized")
    
    return await endpoints.terminate_sandbox(chat_id, user_id, device_id)

@app.post("/force-connect")
async def force_connect(
    request: ForceConnectRequest
):
    """Force connect to a chat session (kick out other device)"""
    if not endpoints:
        raise HTTPException(status_code=500, detail="Service not initialized")
    
    return await endpoints.force_connect(request)

@app.get("/session-data/{chat_id}")
async def load_session_data(
    chat_id: str,
    user_id: str,
    authorization: Optional[str] = Header(None)
):
    """Load session data from database for sandbox initialization"""
    if not endpoints:
        raise HTTPException(status_code=500, detail="Service not initialized")
    
    # Extract access token from Authorization header
    access_token = None
    if authorization and authorization.startswith("Bearer "):
        access_token = authorization.split(" ")[1]
    
    return await endpoints.load_session_data(chat_id, user_id, access_token)

@app.post("/session-data/{chat_id}")
async def save_session_data(
    chat_id: str,
    user_id: str,
    chat_history: List[dict],
    enabled_mcps: List[dict]
):
    """Save session data to database"""
    if not endpoints:
        raise HTTPException(status_code=500, detail="Service not initialized")
    
    return await endpoints.save_session_data(chat_id, user_id, chat_history, enabled_mcps)

@app.post("/chat/{chat_id}/stream")
async def chat_stream_endpoint(
    chat_id: str,
    request: dict,
    authorization: Optional[str] = Header(None)
):
    """Stream chat response from sandbox"""
    if not endpoints:
        raise HTTPException(status_code=500, detail="Service not initialized")
    
    # Extract access token from Authorization header
    access_token = None
    if authorization and authorization.startswith("Bearer "):
        access_token = authorization.split(" ")[1]
    
    # Extract data from request body
    message = request.get("message")
    user_id = request.get("user_id") 
    device_id = request.get("device_id")
    
    return StreamingResponse(
        endpoints.chat_stream(chat_id, user_id, message, device_id, access_token),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"}
    )

@app.post("/connect-chat")
async def connect_chat(
    request: dict,
    authorization: Optional[str] = Header(None)
):
    """Connect to a chat - handles both new and existing chats"""
    if not endpoints:
        raise HTTPException(status_code=500, detail="Service not initialized")
    
    # Extract access token from Authorization header
    access_token = None
    if authorization and authorization.startswith("Bearer "):
        access_token = authorization.split(" ")[1]
    
    chat_id = request.get("chat_id")
    user_id = request.get("user_id") 
    device_id = request.get("device_id")
    
    if not chat_id or not user_id or not device_id:
        raise HTTPException(status_code=400, detail="Missing required fields: chat_id, user_id, device_id")
    
    return await endpoints.connect_chat(chat_id, user_id, device_id, access_token)

@app.get("/user/{user_id}/sessions")
async def get_user_sessions(
    user_id: str,
    authorization: Optional[str] = Header(None)
):
    """Get all chat sessions for a user"""
    if not endpoints:
        raise HTTPException(status_code=500, detail="Service not initialized")
    
    # Extract access token from Authorization header
    access_token = None
    if authorization and authorization.startswith("Bearer "):
        access_token = authorization.split(" ")[1]
    
    return await endpoints.get_user_chat_sessions(user_id, access_token)

@app.get("/mcps")
async def get_mcps():
    """Get all available MCPs"""
    if not endpoints:
        raise HTTPException(status_code=500, detail="Service not initialized")
    
    return await endpoints.get_mcps()

@app.post("/mcps")
async def create_mcp(
    request: dict,
    authorization: Optional[str] = Header(None)
):
    """Create a new MCP"""
    if not endpoints:
        raise HTTPException(status_code=500, detail="Service not initialized")
    
    # Extract access token from Authorization header
    access_token = None
    if authorization and authorization.startswith("Bearer "):
        access_token = authorization.split(" ")[1]
    
    name = request.get("name")
    command = request.get("command")
    args = request.get("args", [])
    title = request.get("title")
    description = request.get("description")
    env = request.get("env")
    
    if not name or not command:
        raise HTTPException(status_code=400, detail="Missing required fields: name, command")
    
    return await endpoints.create_mcp(name, command, args, title, description, env, access_token)

@app.delete("/mcps/{mcp_id}")
async def delete_mcp(mcp_id: str):
    """Delete MCP (empty endpoint - deletion ignored)"""
    if not endpoints:
        raise HTTPException(status_code=500, detail="Service not initialized")
    
    return await endpoints.delete_mcp(mcp_id)

@app.post("/chat/{chat_id}/toggle-mcp")
async def toggle_mcp_for_chat(
    chat_id: str,
    request: dict,
    device_id: str = Header(..., alias="X-Device-ID"),
    authorization: Optional[str] = Header(None)
):
    """Toggle MCP for a chat session and update session table"""
    if not endpoints:
        raise HTTPException(status_code=500, detail="Service not initialized")
    
    # Extract access token from Authorization header
    access_token = None
    if authorization and authorization.startswith("Bearer "):
        access_token = authorization.split(" ")[1]
    
    mcp_name = request.get("mcp_name")
    enabled = request.get("enabled")
    config = request.get("config")
    user_id = request.get("user_id")
    
    if not mcp_name or enabled is None or not user_id:
        raise HTTPException(status_code=400, detail="Missing required fields: mcp_name, enabled, user_id")
    
    return await endpoints.toggle_mcp_for_chat(chat_id, user_id, device_id, mcp_name, enabled, config, access_token)

@app.post("/sandbox-status")
async def check_sandbox_status(
    request: dict,
    authorization: Optional[str] = Header(None)
):
    """Check if sandbox is ready for a chat"""
    if not endpoints:
        raise HTTPException(status_code=500, detail="Service not initialized")
    
    chat_id = request.get("chat_id")
    user_id = request.get("user_id")
    
    if not chat_id or not user_id:
        raise HTTPException(status_code=400, detail="Missing required fields: chat_id, user_id")
    
    return await endpoints.check_sandbox_status(chat_id, user_id)

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    if not endpoints:
        return {"status": "unhealthy", "error": "Service not initialized"}
    
    return await endpoints.health_check()

# Additional utility endpoints
@app.get("/active-sandboxes")
async def list_active_sandboxes():
    """List all active sandboxes (admin endpoint)"""
    if not sandbox_manager:
        raise HTTPException(status_code=500, detail="Service not initialized")
    
    return sandbox_manager.list_active_sandboxes()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)