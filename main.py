from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from typing import Optional, List, Dict, Any
import os

from descope import DescopeClient, AuthException

from core.sandbox_manager import SandboxManager
from core.session_manager import SessionManager
from core.database import DatabaseManager
from api.endpoints import OrchestratorEndpoints
from models.schemas import (
    CreateSandboxRequest,
    APIResponse
)

# Initialize Descope client
DESCOPE_PROJECT_ID = os.environ.get("DESCOPE_PROJECT_ID")
if DESCOPE_PROJECT_ID:
    descope_client = DescopeClient(project_id=DESCOPE_PROJECT_ID)
else:
    descope_client = None

async def validate_session_token(authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    """Dependency to validate Descope session token and return user info"""
    if not descope_client:
        raise HTTPException(
            status_code=500,
            detail="Descope client not configured"
        )

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Authorization header missing or invalid"
        )

    session_token = authorization.split(" ")[1]

    try:
        jwt_response = descope_client.validate_session(session_token=session_token)

        # Extract user_id from 'sub' field
        user_id = jwt_response.get("sub")
        if not user_id:
            raise HTTPException(
                status_code=401,
                detail="User ID not found in token"
            )

        return {
            "user_id": user_id,
            "token_data": jwt_response,
            "session_token": session_token
        }

    except AuthException as e:
        raise HTTPException(
            status_code=401,
            detail="Session expired or invalid"
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

# Device ID handling removed; user identity is derived from auth_data

# Endpoints
@app.post("/create-sandbox")
async def create_sandbox(
    request: CreateSandboxRequest,
    auth_data: Dict[str, Any] = Depends(validate_session_token)
):
    """Create a new sandbox for a chat session"""
    if not endpoints:
        raise HTTPException(status_code=500, detail="Service not initialized")
    return await endpoints.create_sandbox(request, auth_data)

@app.get("/sandbox/{chat_id}")
async def get_sandbox_info(
    chat_id: str,
    auth_data: Dict[str, Any] = Depends(validate_session_token)
):
    """Get sandbox information for a chat"""
    if not endpoints:
        raise HTTPException(status_code=500, detail="Service not initialized")

    return await endpoints.get_sandbox_info(chat_id, auth_data)

@app.delete("/sandbox/{chat_id}")
async def terminate_sandbox(
    chat_id: str,
    auth_data: Dict[str, Any] = Depends(validate_session_token)
):
    """Manually terminate a sandbox"""
    if not endpoints:
        raise HTTPException(status_code=500, detail="Service not initialized")

    return await endpoints.terminate_sandbox(chat_id, auth_data)


@app.get("/session-data/{chat_id}")
async def load_session_data(
    chat_id: str,
    auth_data: Dict[str, Any] = Depends(validate_session_token)
):
    """Load session data from database for sandbox initialization"""
    if not endpoints:
        raise HTTPException(status_code=500, detail="Service not initialized")

    return await endpoints.load_session_data(chat_id, auth_data)

@app.post("/session-data/{chat_id}")
async def save_session_data(
    chat_id: str,
    chat_history: List[dict],
    enabled_mcps: List[dict],
    auth_data: Dict[str, Any] = Depends(validate_session_token)
):
    """Save session data to database"""
    if not endpoints:
        raise HTTPException(status_code=500, detail="Service not initialized")

    return await endpoints.save_session_data(chat_id, auth_data, chat_history, enabled_mcps)

@app.post("/chat/{chat_id}/stream")
async def chat_stream_endpoint(
    chat_id: str,
    request: dict,
    auth_data: Dict[str, Any] = Depends(validate_session_token)
):
    """Stream chat response from sandbox"""
    if not endpoints:
        raise HTTPException(status_code=500, detail="Service not initialized")

    # Extract data from request body
    message = request.get("message")

    return StreamingResponse(
        endpoints.chat_stream(chat_id, message, auth_data),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"}
    )

@app.post("/connect-chat")
async def connect_chat(
    request: dict,
    auth_data: Dict[str, Any] = Depends(validate_session_token)
):
    """Connect to a chat - handles both new and existing chats"""
    if not endpoints:
        raise HTTPException(status_code=500, detail="Service not initialized")

    chat_id = request.get("chat_id")

    if not chat_id:
        raise HTTPException(status_code=400, detail="Missing required field: chat_id")

    return await endpoints.connect_chat(chat_id, auth_data)

@app.get("/user/sessions")
async def get_user_sessions(    
    auth_data: Dict[str, Any] = Depends(validate_session_token)
):
    """Get all chat sessions for a user"""
    if not endpoints:
        raise HTTPException(status_code=500, detail="Service not initialized")

    return await endpoints.get_user_chat_sessions(auth_data)

@app.get("/mcps")
async def get_mcps():
    """Get all available MCPs"""
    if not endpoints:
        raise HTTPException(status_code=500, detail="Service not initialized")
    
    return await endpoints.get_mcps()

@app.get("/client-mcps/{mcp_id}")
async def get_client_mcp(
    mcp_id: str,
    auth_data: Dict[str, Any] = Depends(validate_session_token)
):
    if not endpoints:
        raise HTTPException(status_code=500, detail="Service not initialized")
    return await endpoints.get_client_mcp(mcp_id, auth_data)

@app.post("/client-mcps/{mcp_id}")
async def save_client_mcp(
    mcp_id: str,
    request: dict,
    auth_data: Dict[str, Any] = Depends(validate_session_token)
):
    if not endpoints:
        raise HTTPException(status_code=500, detail="Service not initialized")
    env_variables = request.get('env_variables') or {}
    return await endpoints.save_client_mcp(mcp_id, env_variables, auth_data)

@app.post("/mcps")
async def create_mcp(
    request: dict,
    auth_data: Dict[str, Any] = Depends(validate_session_token)
):
    """Create a new MCP"""
    if not endpoints:
        raise HTTPException(status_code=500, detail="Service not initialized")

    name = request.get("name")
    command = request.get("command")
    args = request.get("args", [])
    title = request.get("title")
    description = request.get("description")
    env = request.get("env")

    if not name or not command:
        raise HTTPException(status_code=400, detail="Missing required fields: name, command")

    return await endpoints.create_mcp(name, command, args, title, description, env, auth_data)

@app.post("/mcps/{mcp_id}/env")
async def update_mcp_general_env(
    mcp_id: str,
    request: dict,
    auth_data: Dict[str, Any] = Depends(validate_session_token)
):
    if not endpoints:
        raise HTTPException(status_code=500, detail="Service not initialized")
    env_updates = request.get('env') or {}
    return await endpoints.update_mcp_general_env(mcp_id, env_updates, auth_data)

@app.post("/mcps/{mcp_id}/visibility")
async def update_mcp_visibility(
    mcp_id: str,
    request: dict,
    auth_data: Dict[str, Any] = Depends(validate_session_token)
):
    if not endpoints:
        raise HTTPException(status_code=500, detail="Service not initialized")
    visibility = request.get('visibility')
    return await endpoints.update_mcp_visibility(mcp_id, visibility, auth_data)

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
    auth_data: Dict[str, Any] = Depends(validate_session_token)
):
    """Toggle MCP for a chat session and update session table"""
    if not endpoints:
        raise HTTPException(status_code=500, detail="Service not initialized")

    mcp_name = request.get("mcp_name")
    enabled = request.get("enabled")
    config = request.get("config")

    if not mcp_name or enabled is None:
        raise HTTPException(status_code=400, detail="Missing required fields: mcp_name, enabled")

    return await endpoints.toggle_mcp_for_chat(chat_id, mcp_name, enabled, config, auth_data)

@app.post("/sandbox-status")
async def check_sandbox_status(
    request: dict,
    auth_data: Dict[str, Any] = Depends(validate_session_token)
):
    """Check if sandbox is ready for a chat"""
    if not endpoints:
        raise HTTPException(status_code=500, detail="Service not initialized")

    chat_id = request.get("chat_id")

    if not chat_id:
        raise HTTPException(status_code=400, detail="Missing required field: chat_id")

    return await endpoints.check_sandbox_status(chat_id, auth_data)

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

# ---------------- MCP Validation Task Routes ----------------

@app.post("/mcp/tasks/start")
async def start_mcp_task(
    request: dict,
    auth_data: Dict[str, Any] = Depends(validate_session_token)
):
    if not endpoints:
        raise HTTPException(status_code=500, detail="Service not initialized")
    return await endpoints.start_mcp_validation(request, auth_data)

@app.get("/mcp/tasks/{task_id}")
async def get_mcp_task_status(task_id: str):
    if not endpoints:
        raise HTTPException(status_code=500, detail="Service not initialized")
    return await endpoints.get_task_status(task_id)

# ---------------- Descope Management Routes ----------------

@app.get("/descope/roles")
async def list_descope_roles(
    auth_data: Dict[str, Any] = Depends(validate_session_token)
):
    if not endpoints:
        raise HTTPException(status_code=500, detail="Service not initialized")
    return await endpoints.list_descope_roles(auth_data)

@app.post("/mcps/{mcp_id}/tool-roles")
async def update_mcp_tool_roles(
    mcp_id: str,
    request: dict,
    auth_data: Dict[str, Any] = Depends(validate_session_token)
):
    if not endpoints:
        raise HTTPException(status_code=500, detail="Service not initialized")
    role_map = request.get('roles') or {}
    return await endpoints.update_mcp_tool_roles(mcp_id, role_map, auth_data)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
