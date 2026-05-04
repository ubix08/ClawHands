"""
Local Agent Server - Main Application Entry Point

A personal AI coding assistant built on OpenHands SDK without sandboxing.
Provides REST API, WebSocket, and Admin endpoints.

Usage:
    python -m local_agent_server.server
    
Or with custom port:
    python -m local_agent_server.server --port 8080
"""

import os
import sys
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware

# Core
from local_agent_server.core.config import settings, get_settings

# Services
from local_agent_server.services import (
    WorkspaceManager,
    ConversationManager,
    get_workspace_manager,
    get_conversation_manager,
    set_workspace_manager,
    set_conversation_manager,
)

# API Routes
from local_agent_server.api import (
    admin_router,
    conversations_router,
    workspaces_router,
    skills_router,
    websocket_endpoint,
    websocket_chat_endpoint,
)

# Try to import GitHub routes
try:
    from local_agent_server.api.routes.github import router as github_router
    HAS_GITHUB = True
except ImportError:
    HAS_GITHUB = False
    github_router = None

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    logger.info("Starting Local Agent Server...")
    
    # Initialize services
    wm = WorkspaceManager(base_dir=settings.workspace_base_dir)
    set_workspace_manager(wm)
    
    cm = ConversationManager(
        workspace_manager=wm,
        api_key=os.getenv("OPENHANDS_API_KEY") or os.getenv("ANTHROPIC_API_KEY"),
    )
    set_conversation_manager(cm)
    
    # Load skills from local directory
    from local_agent_server.skills import load_skills_from_directory
    skills_dir = Path(__file__).parent / "skills" / "skills"
    skill_count = load_skills_from_directory(str(skills_dir))
    logger.info(f"Loaded {skill_count} skills")
    
    logger.info(f"Workspace: {wm.base_dir}")
    logger.info(f"API Key: {'configured' if cm.api_key else 'NOT CONFIGURED'}")
    
    yield
    
    # Cleanup
    logger.info("Shutting down Local Agent Server...")


# Create FastAPI app
app = FastAPI(
    title="Local Agent Server",
    description="Personal AI Coding Assistant - Built on OpenHands SDK",
    version="1.0.0",
    lifespan=lifespan,
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Root endpoint
@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "name": "Local Agent Server",
        "version": "1.0.0",
        "docs": "/docs",
    }


# Health check
@app.get("/health")
async def health():
    """Health check endpoint."""
    cm = get_conversation_manager()
    wm = get_workspace_manager()
    return {
        "status": "healthy",
        "version": "1.0.0",
        "conversations": len(cm.conversations),
        "api_key_configured": bool(cm.api_key),
    }


# Register routers
app.include_router(admin_router)
app.include_router(conversations_router)
app.include_router(workspaces_router)
app.include_router(skills_router)

# Register GitHub router if available
if HAS_GITHUB and github_router:
    app.include_router(github_router)
    logger.info("GitHub integration enabled")


# WebSocket endpoints
@app.websocket("/ws/{conversation_id}")
async def ws_endpoint(websocket: WebSocket, conversation_id: str):
    """WebSocket endpoint for real-time streaming."""
    await websocket_endpoint(websocket, conversation_id)


@app.websocket("/chat/{conversation_id}")
async def ws_chat_endpoint(websocket: WebSocket, conversation_id: str):
    """WebSocket endpoint for interactive chat."""
    await websocket_chat_endpoint(websocket, conversation_id)


def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Local Agent Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind to")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload")
    parser.add_argument("--log-level", default="info", help="Log level")
    
    args = parser.parse_args()
    
    # Run server
    uvicorn.run(
        "local_agent_server.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
        factory=True,
    )


if __name__ == "__main__":
    main()