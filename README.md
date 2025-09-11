# Fleet Sandbox Orchestrator

Youtube - https://youtu.be/0socQ2D_6xU
## Please go to dcp branch - https://github.com/Nirmal2000/fleet_orchestrator_backend/tree/dcp

## Section 1: What is Fleet?

Fleet is an AI-powered platform that enables seamless interaction with AI assistants through secure, isolated sandbox environments integrated with Model Context Protocol (MCP) tools. It leverages Descope for brief authentication management, including sign-in and login flows, ensuring users can securely access the platform. Fleet supports inbound MCP apps for receiving external data and requests, as well as outbound integrations for connecting to third-party services, allowing flexible data flow and tool interoperability. Users have the ability to select specific tools and functions to run within their chat sessions, tailored to their needs. Role-Based Access Control (RBAC) is implemented for MCP tools, restricting access based on user tiers such as free or premium, ensuring appropriate resource allocation. Additionally, Fleet uses Supabase to sign Descope user IDs, enabling authenticated calls to the database for secure data persistence and management.

## Section 2: Getting Started

To get started with Fleet, you'll need to set up and run the three core services: Sandbox Orchestrator, FastAPI Chatbot, and Fleet Frontend. Follow these steps to launch each service.

### Prerequisites
- Python 3.11+
- Node.js 18+
- Supabase account
- Descope project
- OpenAI API key (for chatbot)
- Browserbase credentials (for browser automation, optional)

### 1. Clone the Repository
```bash
git clone <repository-url>
cd fleet
```

### 2. Set Up Environment Variables

Create `.env` files for each service with the required variables:

#### Sandbox Orchestrator Environment Variables (`.env` for sandbox_orchestrator)
```env
# Authentication and Authorization
DESCOPE_PROJECT_ID=your_descope_project_id
DESCOPE_MANAGEMENT_KEY=your_descope_management_key
DESCOPE_MCP_INBOUND_APP_ID=your_mcp_inbound_app_id

# Database
SUPABASE_PROJECT_URL=your_supabase_project_url
SUPABASE_KEY=your_supabase_anon_key
SUPABASE_SCHEMA=fleet_descope
SUPABASE_JWT_SECRET=your_jwt_secret

# Sandbox and Development
LOCAL_TESTING=true/false
LOCAL_CHATBOT_URL=http://localhost:3001
GOOGLE_OAUTH_CLIENT_ID=your_google_oauth_client_id
GOOGLE_OAUTH_CLIENT_SECRET=your_google_oauth_client_secret
```

#### FastAPI Chatbot Environment Variables (`.env` for fastapi_chatbot)
```env
# AI and API Keys
OPENAI_API_KEY=sk-or-v1-your-openai_api_key
GOOGLE_API_KEY=your_google_api_key
BROWSERBASE_PROJECT_ID=your_browserbase_project_id
BROWSERBASE_API_KEY=your_browserbase_api_key

# Model Configuration
MODEL=google/gemini-2.5-pro  # or other OpenRouter model
```

#### Fleet Frontend Environment Variables (`.env.local` for fleet_frontend)
```env
NEXT_PUBLIC_DESCOPE_PROJECT_ID=your_descope_project_id
NEXT_PUBLIC_SANDBOX_ORCHESTRATOR_URL=http://localhost:8000
NEXT_PUBLIC_DEBUG_MODE=true
```

### 3. Install Dependencies

#### Sandbox Orchestrator
```bash
cd sandbox_orchestrator
pip install -r requirements.txt
```

#### FastAPI Chatbot
```bash
cd fastapi_chatbot
pip install -r requirements.txt
```

#### Fleet Frontend
```bash
cd fleet_frontend
npm install
```

### 4. Start the Services

Run each service independently. Start with the sandbox orchestrator, then the chatbot, and finally the frontend.

#### Sandbox Orchestrator (uvicorn command)
```bash
cd sandbox_orchestrator
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

#### FastAPI Chatbot (uvicorn command)
```bash
cd fastapi_chatbot
uvicorn main:app --host 0.0.0.0 --port 3000 --reload
```

#### Fleet Frontend (npm run dev)
```bash
cd fleet_frontend
npm run dev
```

### 5. Verify Installation
- Open http://localhost:3000 for the frontend
- Sandbox orchestrator API at http://localhost:8000/health
- Chatbot service at http://localhost:3000/health

## Section 3: Sandbox Orchestrator Service

The Sandbox Orchestrator is a FastAPI-based service that manages isolated code execution environments (sandboxes) for AI chat sessions. It handles sandbox lifecycle management, MCP tool registration, session persistence, and OAuth authentication flows. Key features include E2B sandbox provisioning, Descope role-based access control, Supabase database integration for session data, and real-time streaming chat coordination between users and AI assistants running in secure isolated environments.

## Section 4: FastAPI Chatbot Service

The FastAPI Chatbot is an AI-powered service providing sophisticated chatbot functionality with MCP tool integration. It supports streaming AI responses, browser automation via Browserbase, file operations, command execution, and external service integrations. The service runs within E2B sandboxes for secure, isolated execution, enabling complex multi-step tool workflows while maintaining session state and user context through streaming JSON responses.

## Section 5: Fleet Frontend Service

The Fleet Frontend is a React/Next.js web application offering an intuitive user interface for Fleet's AI chat and tool management capabilities. It provides real-time chat streaming, MCP tool configuration, OAuth authentication flows, and responsive design with dark theme support. The frontend integrates with the Sandbox Orchestrator for session management and coordinates chat interactions with the FastAPI Chatbot running in isolated sandboxes, delivering a seamless user experience for tool-augmented AI conversations.
