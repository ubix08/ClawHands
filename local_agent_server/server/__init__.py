"""
Local Agent Server - Personal AI Coding Assistant
Built on OpenHands SDK without sandboxing or Docker

This server provides:
- REST API for conversation management
- WebSocket for real-time event streaming
- Direct workspace access (no sandboxing)
- Skill loading from local files

Usage:
    python -m local_agent_server.server
    
Or with custom settings:
    OPENHANDS_API_KEY=sk-... python -m local_agent_server.server --port 8000
"""

import os
import sys
import uuid
import json
import asyncio
import logging
import tempfile
from pathlib import Path
from datetime import datetime
from typing import Optional, Any
from dataclasses import dataclass, field

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, SecretStr
from contextlib import asynccontextmanager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ============================================================================
# MODELS
# ============================================================================


class SendMessageRequest(BaseModel):
    """Request to send a message to a conversation."""
    message: str
    role: str = "user"


class ExecuteRequest(BaseModel):
    """Request to execute a command in workspace."""
    command: str


class ExecuteResponse(BaseModel):
    """Response from command execution."""
    exit_code: int
    stdout: str
    stderr: str


# ============================================================================
# CONVERSATION CLASS
# ============================================================================


@dataclass
class Conversation:
    """Represents a conversation with an agent."""
    id: str
    workspace_dir: str
    created_at: datetime = field(default_factory=datetime.now)
    status: str = "created"
    title: str = ""
    agent: Any = None
    sdk_conversation: Any = None
    events: list = field(default_factory=list)
    
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "workspace": self.workspace_dir,
            "status": self.status,
            "title": self.title,
            "created_at": self.created_at.isoformat(),
        }


# ============================================================================
# WORKSPACE MANAGER
# ============================================================================


class WorkspaceManager:
    """Manages workspace directories for conversations."""
    
    def __init__(self, base_dir: Optional[str] = None):
        if base_dir:
            self.base_dir = Path(base_dir)
        else:
            self.base_dir = Path(os.path.expanduser("~/agent-workspaces"))
        
        self.base_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Workspace manager initialized: {self.base_dir}")
    
    def create_workspace(self, conversation_id: str) -> str:
        workspace = self.base_dir / conversation_id
        workspace.mkdir(parents=True, exist_ok=True)
        return str(workspace)
    
    def get_workspace(self, conversation_id: str) -> Optional[str]:
        workspace = self.base_dir / conversation_id
        return str(workspace) if workspace.exists() else None
    
    def delete_workspace(self, conversation_id: str) -> None:
        import shutil
        workspace = self.base_dir / conversation_id
        if workspace.exists():
            shutil.rmtree(workspace)


# ============================================================================
# AGENT FACTORY - Replicates OpenHands agent-server behavior
# ============================================================================


# System prompt that matches original
PLANNING_AGENT_INSTRUCTION = """<IMPORTANT_PLANNING_BOUNDARIES>
You are a planning agent. Your role is to:
1. Understand the user's goal
2. Break down the task into steps
3. Execute each step using available tools
4. When done, use the "finish" action with a summary

Important rules:
- Always confirm before making irreversible changes
- Keep track of what you've done
- Ask for clarification if the goal is unclear
</IMPORTANT_PLANNING_BOUNDARIES>"""


def create_agent(
    llm_api_key: str,
    model: str = "anthropic/claude-sonnet-4-5-20250929",
    enable_browser: bool = True,
    agent_type: str = "default",  # "default" or "plan"
    system_message_suffix: Optional[str] = None,
) -> "Agent":
    """Create an OpenHands agent with EXACT same config as agent-server.
    
    This replicates the behavior from:
    openhands/app_server/app_conversation/live_status_app_conversation_service.py
    lines ~1400-1425
    """
    try:
        from openhands.sdk import LLM, Agent, AgentContext
        from openhands.sdk.context import Skill, KeywordTrigger
        from openhands.sdk.context.skills import load_skills_from_dir
        from openhands.tools.preset.default import get_default_tools
    except ImportError as e:
        logger.error(f"Failed to import OpenHands SDK: {e}")
        raise
    
    # 1. Create LLM configuration (same as original)
    llm = LLM(
        usage_id="local-agent",
        model=model,
        api_key=SecretStr(llm_api_key),
        # Additional params that match original
        base_url=os.getenv("LLM_BASE_URL"),  # Support custom endpoints
    )
    logger.info(f"Created LLM: model={model}")
    
    # 2. Get tools - EXACT same as original (line 1405)
    # Use get_default_tools with browser enabled (same as agent-server)
    if agent_type == "plan":
        # Planning agent uses limited tools - simplified for planning
        from openhands.tools.task_tracker import TaskTrackerTool
        tools = [
            TaskTrackerTool,
        ]
    else:
        # Default agent uses full toolset - EXACT same as original
        tools = get_default_tools(enable_browser=enable_browser)
    
    logger.info(f"Loaded {len(tools)} tools (browser={enable_browser})")
    
    # 3. Load skills from local directory (replicates agent-server behavior)
    skills_dir = Path(__file__).parent.parent / "skills"
    repo_skills = {}
    knowledge_skills = {}
    agent_skills = {}
    
    if skills_dir.exists():
        repo_skills, knowledge_skills, agent_skills = load_skills_from_dir(str(skills_dir))
        logger.info(f"Loaded skills: {len(repo_skills)} repo, {len(knowledge_skills)} knowledge")
    
    # 4. Create default skill (acts as system prompt) - matches original behavior
    default_content = system_message_suffix or """You are an expert AI coding assistant.

Your role:
1. Write clean, well-documented code
2. Use type hints in Python
3. Add docstrings to all public functions
4. Handle errors gracefully
5. Write tests for new features
6. Keep functions under 50 lines

When editing files:
- Use absolute paths in the workspace
- Create directories as needed
- Always verify changes work

When completing a task:
- Summarize what was done
- Use the finish action
"""
    
    all_skills_list = [
        Skill(
            name="coding-assistant",
            content=default_content,
            trigger=None,  # Always active
        ),
    ]
    
    # Add loaded skills
    all_skills_list.extend(list(repo_skills.values()))
    all_skills_list.extend(list(knowledge_skills.values()))
    
    # 5. Add public skills (same as original)
    # The original loads from .openhands/skills if exists
    user_skills_dir = Path.home() / ".openhands" / "skills"
    if user_skills_dir.exists():
        user_repo, user_knowledge, _ = load_skills_from_dir(str(user_skills_dir))
        all_skills_list.extend(list(user_repo.values()))
        all_skills_list.extend(list(user_knowledge.values()))
    
    # 6. Build effective system_message_suffix (matches original lines 1379-1396)
    effective_suffix = system_message_suffix or ""
    
    # Add web host context if available
    web_url = os.getenv("WEB_URL")
    if web_url:
        effective_suffix += f"\n\n<HOST>\n{web_url}\n</HOST>"
    
    # Add planning agent instruction if needed
    if agent_type == "plan":
        if effective_suffix:
            effective_suffix = f"{PLANNING_AGENT_INSTRUCTION}\n\n{effective_suffix}"
        else:
            effective_suffix = PLANNING_AGENT_INSTRUCTION
    
    # 7. Create AgentContext (matches original line 1415-1418)
    agent_context = AgentContext(
        skills=all_skills_list,
        system_message_suffix=effective_suffix,
        user_message_suffix=None,  # Can add if needed
    )
    
    # 8. Create Agent (matches original line 1421)
    agent = Agent(
        llm=llm,
        tools=tools,
        agent_context=agent_context,
    )
    
    logger.info(
        f"Created agent: type={agent_type}, "
        f"skills={len(all_skills_list)}, "
        f"tools={len(tools)}"
    )
    
    return agent


# ============================================================================
# CONVERSATION MANAGER
# ============================================================================


class ConversationManager:
    """Manages all conversations.
    
    Replicates the conversation handling from:
    openhands/app_server/app_conversation/live_status_app_conversation_service.py
    """
    
    def __init__(self, workspace_manager: WorkspaceManager):
        self.workspace_manager = workspace_manager
        self.conversations: dict[str, Conversation] = {}
        self.api_key: Optional[str] = None
        self.model: str = "anthropic/claude-sonnet-4-5-20250929"
        self.enable_browser: bool = True  # Same as original
        self.agent_type: str = "default"  # "default" or "plan"
        
        # Load configuration from environment (same as original)
        self.api_key = os.getenv("OPENHANDS_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
        self.model = os.getenv("LLM_MODEL", self.model)
        self.enable_browser = os.getenv("ENABLE_BROWSER", "true").lower() == "true"
        
        if not self.api_key:
            logger.warning("No API key! Set OPENHANDS_API_KEY")
    
    def set_api_key(self, api_key: str) -> None:
        self.api_key = api_key
    
    def create_conversation(
        self,
        workspace_dir: Optional[str] = None,
        initial_message: Optional[str] = None,
        agent_type: Optional[str] = None,
        enable_browser: Optional[bool] = None,
    ) -> Conversation:
        """Create a new conversation with agent.
        
        This replicates:
        - _build_start_conversation_request() from original
        - ConversationSettings.create_request() from original
        """
        if not self.api_key:
            raise ValueError("API key not configured")
        
        # Use provided options or defaults
        agent_type = agent_type or self.agent_type
        enable_browser = enable_browser if enable_browser is not None else self.enable_browser
        
        conv_id = str(uuid.uuid4())
        ws = workspace_dir or self.workspace_manager.create_workspace(conv_id)
        
        # Create agent with EXACT same config as original
        # Replicates lines ~1407-1425 from original
        agent = create_agent(
            llm_api_key=self.api_key,
            model=self.model,
            enable_browser=enable_browser,
            agent_type=agent_type,
            system_message_suffix=None,  # Can be customized
        )
        
        # Import SDK conversation (same as original)
        from openhands.sdk import Conversation as SDKConversation
        
        # Create conversation - same as original
        sdk_conversation = SDKConversation(
            agent=agent,
            workspace=ws,
        )
        
        conversation = Conversation(
            id=conv_id,
            workspace_dir=ws,
            agent=agent,
            sdk_conversation=sdk_conversation,
        )
        
        if initial_message:
            sdk_conversation.send_message(initial_message)
            conversation.title = initial_message[:50]
        
        self.conversations[conv_id] = conversation
        logger.info(f"Created conversation {conv_id} (type={agent_type}, browser={enable_browser})")
        
        return conversation
    
    def get_conversation(self, conv_id: str) -> Optional[Conversation]:
        return self.conversations.get(conv_id)
    
    def send_message(self, conv_id: str, message: str) -> None:
        conv = self.get_conversation(conv_id)
        if conv:
            conv.sdk_conversation.send_message(message)
            conv.status = "running"
    
    def run_conversation(self, conv_id: str) -> None:
        conv = self.get_conversation(conv_id)
        if conv:
            try:
                conv.sdk_conversation.run()
                conv.status = "finished"
            except Exception as e:
                conv.status = "error"
                logger.error(f"Error: {e}")
    
    def delete_conversation(self, conv_id: str) -> None:
        if conv_id in self.conversations:
            del self.conversations[conv_id]
        self.workspace_manager.delete_workspace(conv_id)


# ============================================================================
# FASTAPI APP
# ============================================================================


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Local Agent Server...")
    
    app.state.workspace_manager = WorkspaceManager(os.getenv("WORKSPACE_BASE_DIR"))
    app.state.conversation_manager = ConversationManager(app.state.workspace_manager)
    
    logger.info(f"Workspace: {app.state.workspace_manager.base_dir}")
    logger.info(f"API key: {bool(app.state.conversation_manager.api_key)}")
    
    yield
    logger.info("Shutting down...")


app = FastAPI(
    title="Local Agent Server",
    description="Personal AI Coding Assistant",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================================
# ENDPOINTS
# ============================================================================


@app.get("/")
async def root():
    return {
        "name": "Local Agent Server",
        "version": "1.0.0",
    }


@app.get("/health")
async def health():
    cm = app.state.conversation_manager
    return {
        "status": "healthy",
        "conversations": len(cm.conversations),
        "api_key_configured": bool(cm.api_key),
    }


@app.post("/api/conversations")
async def create_conversation(request: Request):
    body = await request.json() if await request.body() else {}
    
    cm = app.state.conversation_manager
    
    api_key = body.get("api_key") or cm.api_key
    if not api_key:
        raise HTTPException(status_code=401, detail="API key required")
    cm.set_api_key(api_key)
    
    conv = cm.create_conversation(
        workspace_dir=body.get("workspace"),
        initial_message=body.get("initial_message"),
    )
    
    return {"id": conv.id, "workspace": conv.workspace_dir}


@app.get("/api/conversations/{conv_id}")
async def get_conversation(conv_id: str):
    conv = app.state.conversation_manager.get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Not found")
    return conv.to_dict()


@app.post("/api/conversations/{conv_id}/messages")
async def send_message(conv_id: str, request: Request):
    body = await request.json()
    message = body.get("message", "")
    
    if not message:
        raise HTTPException(status_code=400, detail="message required")
    
    cm = app.state.conversation_manager
    conv = cm.get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Not found")
    
    cm.send_message(conv_id, message)
    asyncio.create_task(cm.run_conversation(conv_id))
    
    return {"status": "ok", "conversation_id": conv_id}


@app.get("/api/conversations/{conv_id}/events")
async def get_events(conv_id: str):
    conv = app.state.conversation_manager.get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Not found")
    
    events = []
    for event in conv.sdk_conversation.state.events:
        event_dict = {"type": type(event).__name__}
        if hasattr(event, "content"):
            event_dict["content"] = event.content
        if hasattr(event, "action"):
            event_dict["action"] = event.action
        if hasattr(event, "tool"):
            event_dict["tool"] = event.tool
        events.append(event_dict)
    
    return {"events": events}


@app.delete("/api/conversations/{conv_id}")
async def delete_conversation(conv_id: str):
    cm = app.state.conversation_manager
    conv = cm.get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Not found")
    
    cm.delete_conversation(conv_id)
    return {"status": "deleted"}


@app.post("/api/workspaces/{conv_id}/execute")
async def execute(conv_id: str, request: Request):
    body = await request.json()
    command = body.get("command", "")
    
    if not command:
        raise HTTPException(status_code=400, detail="command required")
    
    conv = app.state.conversation_manager.get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Not found")
    
    import subprocess
    result = subprocess.run(
        command, shell=True, cwd=conv.workspace_dir,
        capture_output=True, text=True,
    )
    
    return {
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


@app.get("/api/workspaces/{conv_id}/files")
async def list_files(conv_id: str):
    conv = app.state.conversation_manager.get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Not found")
    
    workspace = Path(conv.workspace_dir)
    files = [str(f.relative_to(workspace)) for f in workspace.rglob("*") if f.is_file()]
    
    return {"files": files}


# ============================================================================
# WEBSOCKET
# ============================================================================


@app.websocket("/ws/{conv_id}")
async def websocket_stream(websocket: WebSocket, conv_id: str):
    await websocket.accept()
    
    conv = app.state.conversation_manager.get_conversation(conv_id)
    if not conv:
        await websocket.send_json({"type": "error", "content": "Not found"})
        await websocket.close()
        return
    
    try:
        await websocket.send_json({"type": "state", "status": conv.status})
        
        last_count = 0
        while conv.status == "running":
            events = conv.sdk_conversation.state.events
            
            if len(events) > last_count:
                for event in events[last_count:]:
                    data = {"type": type(event).__name__}
                    if hasattr(event, "content"):
                        data["content"] = str(event.content)[:500]
                    if hasattr(event, "action"):
                        data["action"] = event.action
                    await websocket.send_json(data)
                    
                last_count = len(events)
            
            await asyncio.sleep(0.1)
        
        await websocket.send_json({"type": "done", "status": conv.status})
        
    except Exception as e:
        await websocket.send_json({"type": "error", "content": str(e)})
    finally:
        await websocket.close()


# ============================================================================
# MAIN
# ============================================================================


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    
    uvicorn.run(
        "local_agent_server.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )