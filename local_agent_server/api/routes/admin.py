"""Admin API routes."""

import time
from contextlib import asynccontextmanager

from fastapi import APIRouter, HTTPException

from local_agent_server.models.schemas import (
    AdminStatsResponse,
    HealthResponse,
    ServerStats,
    SettingsResponse,
    UpdateSettingsRequest,
)
from local_agent_server.services import get_conversation_manager, get_workspace_manager

router = APIRouter(prefix="/api/admin", tags=["admin"])

# Server start time for uptime calculation
start_time = time.time()


@router.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint."""
    cm = get_conversation_manager()
    stats = cm.get_stats()
    
    return HealthResponse(
        status="healthy",
        version="1.0.0",
        conversations=stats["total_conversations"],
        api_key_configured=bool(cm.api_key),
    )


@router.get("/stats", response_model=AdminStatsResponse)
async def get_stats():
    """Get server statistics."""
    cm = get_conversation_manager()
    wm = get_workspace_manager()
    stats = cm.get_stats()
    
    total_events = 0
    total_messages = 0
    for conv in cm.conversations.values():
        total_events += len(conv.sdk_conversation.state.events) if conv.sdk_conversation else 0
        total_messages += len(conv.sdk_conversation.state.messages) if conv.sdk_conversation else 0
    
    return AdminStatsResponse(
        stats=ServerStats(
            total_conversations=stats["total_conversations"],
            active_conversations=stats["active_conversations"],
            total_messages=total_messages,
            total_events=total_events,
        ),
        uptime_seconds=time.time() - start_time,
        version="1.0.0",
    )


@router.get("/settings", response_model=SettingsResponse)
async def get_settings():
    """Get server settings."""
    cm = get_conversation_manager()
    wm = get_workspace_manager()
    
    return SettingsResponse(
        model=cm.model,
        enable_browser=cm.enable_browser,
        workspace_base_dir=str(wm.base_dir),
        default_agent_type=cm.default_agent_type,
    )


@router.post("/settings")
async def update_settings(body: UpdateSettingsRequest):
    """Update server settings."""
    cm = get_conversation_manager()
    
    if body.api_key:
        cm.set_api_key(body.api_key)
    if body.model:
        cm.set_model(body.model)
    
    return {"status": "ok"}


@router.post("/reset")
async def reset_server():
    """Reset the server (clear all conversations)."""
    cm = get_conversation_manager()
    wm = get_workspace_manager()
    
    for conv_id in list(cm.conversations.keys()):
        cm.delete_conversation(conv_id)
    
    return {"status": "reset", "message": "All conversations cleared"}


@router.get("/workspaces")
async def list_workspaces():
    """List all workspaces."""
    wm = get_workspace_manager()
    workspaces = wm.list_workspaces()
    
    return {
        "workspaces": [
            {
                "conversation_id": ws_id,
                "path": wm.get_workspace(ws_id),
                "size": wm.get_workspace_size(ws_id),
            }
            for ws_id in workspaces
        ]
    }


@router.delete("/workspaces/{conversation_id}")
async def delete_workspace(conversation_id: str):
    """Delete a workspace."""
    wm = get_workspace_manager()
    
    if not wm.get_workspace(conversation_id):
        raise HTTPException(status_code=404, detail="Workspace not found")
    
    wm.delete_workspace(conversation_id)
    
    return {"status": "deleted", "conversation_id": conversation_id}