from core.sandbox_manager import SandboxManager
from core.session_manager import SessionManager
from core.database import DatabaseManager

from .sandbox_endpoints import SandboxEndpoints
from .mcp_endpoints import MCPEndpoints


class OrchestratorEndpoints:
    """Thin orchestrator delegating to domain-specific endpoint handlers.

    Public method names and signatures remain unchanged to avoid any change in routing.
    """

    def __init__(self, sandbox_manager: SandboxManager, session_manager: SessionManager, db_manager: DatabaseManager):
        self.sandbox = SandboxEndpoints(sandbox_manager, session_manager, db_manager)
        self.mcp = MCPEndpoints(sandbox_manager, session_manager, db_manager)

        # Delegate attributes so existing routes continue to work without changes
        self.start_mcp_validation = self.mcp.start_mcp_validation
        self.list_descope_roles = self.mcp.list_descope_roles
        self.update_mcp_tool_roles = self.mcp.update_mcp_tool_roles
        self.get_task_status = self.mcp.get_task_status
        self.list_outbound_apps = self.mcp.list_outbound_apps
        self.latest_outbound_token = self.mcp.latest_outbound_token
        self.is_outbound_connected = self.mcp.is_outbound_connected
        self.toggle_gmail_integration = self.mcp.toggle_gmail_integration

        self.create_sandbox = self.sandbox.create_sandbox
        self.get_sandbox_info = self.sandbox.get_sandbox_info
        self.terminate_sandbox = self.sandbox.terminate_sandbox
        self.load_session_data = self.sandbox.load_session_data
        self.save_session_data = self.sandbox.save_session_data
        self.chat_stream = self.sandbox.chat_stream
        self.connect_chat = self.sandbox.connect_chat
        self.get_user_chat_sessions = self.sandbox.get_user_chat_sessions
        self.check_sandbox_status = self.sandbox.check_sandbox_status
        self.health_check = self.sandbox.health_check

        self.get_mcps = self.mcp.get_mcps
        self.update_mcp_general_env = self.mcp.update_mcp_general_env
        self.update_mcp_visibility = self.mcp.update_mcp_visibility
        self.get_client_mcp = self.mcp.get_client_mcp
        self.save_client_mcp = self.mcp.save_client_mcp
        self.create_mcp = self.mcp.create_mcp
        self.delete_mcp = self.mcp.delete_mcp
        self.toggle_mcp_for_chat = self.mcp.toggle_mcp_for_chat
        self.set_user_membership_role = self.mcp.set_user_membership_role
