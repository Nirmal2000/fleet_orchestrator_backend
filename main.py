from fastapi import FastAPI, HTTPException, Header, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, Response
from typing import Optional, List, Dict, Any
import os
import httpx
import jwt

from descope import DescopeClient, AuthException

from core.sandbox_manager import SandboxManager
from core.session_manager import SessionManager
from core.database import DatabaseManager
from api.endpoints import OrchestratorEndpoints
from models.schemas import (
    CreateSandboxRequest,
    APIResponse
)
import traceback

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
        print(jwt_response.get("claims", {}).get("scope", "").split())
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

# Inbound App OAuth config
DESCOPE_BASE_URL = os.environ.get("DESCOPE_BASE_URL", os.environ.get("BASE", "https://api.descope.com"))
INBOUND_ACCESS_COOKIE = os.environ.get("INBOUND_ACCESS_COOKIE_NAME", "inbound_access_token")

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
    print("DEI",auth_data['token_data']['roles'])
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

@app.get("/mcps/{mcp_id}/inbound-config")
async def get_mcp_inbound_config(mcp_id: str):
    if not endpoints:
        raise HTTPException(status_code=500, detail="Service not initialized")
    return await endpoints.get_inbound_config(mcp_id)

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
    http_request: Request,
    auth_data: Dict[str, Any] = Depends(validate_session_token)
):
    """Toggle MCP for a chat session and update session table"""
    if not endpoints:
        raise HTTPException(status_code=500, detail="Service not initialized")

    mcp_id = request.get("mcp_id")
    enabled = request.get("enabled")
    print("Toggle MCP Request:", request)
    if not mcp_id or enabled is None:
        raise HTTPException(status_code=400, detail="Missing required fields: mcp_id, enabled")

    # If enabling an MCP and no inbound token stored, instruct client to run inbound flow
    try:
        if enabled:
            user_id = auth_data.get("user_id")
            client_row = await db_manager.get_client_mcp(user_id, mcp_id)
            if not client_row or not client_row.get("access_token"):
                return {
                    "success": False,
                    "inbound_required": True,
                    "message": "Inbound app authorization required"
                }
    except Exception:
        if enabled:
            return {
                "success": False,
                "inbound_required": True,
                "message": "Inbound app authorization required"
            }

    return await endpoints.toggle_mcp_for_chat(chat_id, mcp_id, enabled, auth_data=auth_data)

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

@app.post("/membership/set-role")
async def set_membership_role(
    request: dict,
    auth_data: Dict[str, Any] = Depends(validate_session_token)
):
    if not endpoints:
        raise HTTPException(status_code=500, detail="Service not initialized")
    desired_role = (request or {}).get('role')
    return await endpoints.set_user_membership_role(desired_role, auth_data)

# ---------------- Outbound Apps ----------------

@app.get("/outbound-apps")
async def list_outbound_apps(
    auth_data: Dict[str, Any] = Depends(validate_session_token)
):
    if not endpoints:
        raise HTTPException(status_code=500, detail="Service not initialized")    
    return await endpoints.list_outbound_apps(auth_data)

@app.get("/outbound-apps/{app_id}/connected")
async def is_outbound_connected(app_id: str, tenant_id: Optional[str] = None, auth_data: Dict[str, Any] = Depends(validate_session_token)):
    if not endpoints:
        raise HTTPException(status_code=500, detail="Service not initialized")
    return await endpoints.is_outbound_connected(app_id, tenant_id, auth_data)

# ---------------- Gmail Integration ----------------

@app.post("/chat/{chat_id}/integrations/gmail/toggle")
async def toggle_gmail_integration(chat_id: str, request: dict, auth_data: Dict[str, Any] = Depends(validate_session_token)):
    if not endpoints:
        raise HTTPException(status_code=500, detail="Service not initialized")
    app_id = (request or {}).get("app_id")
    tenant_id = (request or {}).get("tenant_id")
    enabled = bool((request or {}).get("enabled"))
    if enabled and not app_id:
        raise HTTPException(status_code=400, detail="Missing required field: app_id")
    return await endpoints.toggle_gmail_integration(chat_id, app_id, tenant_id, enabled, auth_data)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

# --------------- Inbound App OAuth (Callback only) ---------------

@app.post("/inbound/callback")
async def inbound_callback(payload: dict, auth_data: Dict[str, Any] = Depends(validate_session_token)) -> Response:
    """
    Exchange authorization code for tokens and set httpOnly access cookie.
    Note: Refresh handling intentionally omitted per current requirements.
    """
    print("Inbound callback payload:", payload)
    code = (payload or {}).get("code")
    code_verifier = (payload or {}).get("code_verifier")
    redirect_uri = (payload or {}).get("redirect_uri")
    clientId = (payload or {}).get("client_id")
    mcpId = (payload or {}).get("mcp_id")
    if not code or not code_verifier or not redirect_uri:
        raise HTTPException(status_code=400, detail="code, code_verifier, redirect_uri required")

    form = {
        "grant_type": "authorization_code",
        "client_id": clientId,
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(
            f"{DESCOPE_BASE_URL}/oauth2/v1/apps/token",
            headers={"content-type": "application/x-www-form-urlencoded"},
            data=form,
        )
    body = None
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text}
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail={"message": "token exchange failed", "body": body})

    access_token = body.get("access_token")
    print("Inbound token response:", body)
    if not access_token:
        raise HTTPException(status_code=502, detail="No access_token in response")

    # Auth dependency gives us the user id
    resolved_user_id = auth_data.get("user_id")
    if not resolved_user_id:
        raise HTTPException(status_code=401, detail="Unable to resolve user identity")

    # Map clientId -> mcp_id and persist access_token
    try:
        if mcpId:
            mcp_row = await db_manager.get_mcp_by_id(mcpId)
        elif clientId:
            mcp_row = await db_manager.find_mcp_by_client_id(clientId)
        else:
            raise HTTPException(status_code=400, detail="clientId or mcpId required")
        if not mcp_row:
            raise HTTPException(status_code=404, detail="MCP not found for clientId")
        mcp_id = mcp_row.get("id")
        await db_manager.save_client_mcp_access_token(resolved_user_id, mcp_id, access_token)
    except HTTPException:
        raise
    except Exception as e:
        print("Failed to persist inbound token:", e)
        raise HTTPException(status_code=500, detail="Failed to persist inbound token")

    # Also set short-lived cookie for compatibility
    resp = Response(status_code=204)
    try:
        resp.set_cookie(
            key=INBOUND_ACCESS_COOKIE,
            value=access_token,
            httponly=True,
            secure=True,
            samesite="Lax",
            path="/",
            max_age=int(body.get("expires_in") or 3600),
        )
    except Exception:
        pass
    return resp
