"""Conversation API routes."""

from typing import List

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from local_agent_server.models.schemas import (
    ConversationResponse,
    CreateConversationRequest,
    SendMessageRequest,
)
from local_agent_server.services import get_conversation_manager

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


@router.post("", response_model=ConversationResponse, status_code=201)
async def create_conversation(request: Request, body: CreateConversationRequest):
    """Create a new conversation."""
    cm = get_conversation_manager()
    
    # Get API key from body or header
    api_key = body.workspace_dir if hasattr(body, 'workspace_dir') else None
    if not cm.api_key:
        # Try to get from Authorization header
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            cm.set_api_key(auth[7:])
    
    conv = cm.create_conversation(
        workspace_dir=body.workspace_dir,
        initial_message=body.initial_message,
        agent_type=body.agent_type,
        enable_browser=body.enable_browser,
    )
    
    return ConversationResponse(
        id=conv.id,
        workspace_dir=conv.workspace_dir,
        status=conv.status,
        title=conv.title,
        agent_type=conv.agent_type,
        enable_browser=conv.enable_browser,
        created_at=conv.created_at,
    )


@router.get("/{conversation_id}", response_model=ConversationResponse)
async def get_conversation(conversation_id: str):
    """Get a conversation by ID."""
    cm = get_conversation_manager()
    conv = cm.get_conversation(conversation_id)
    
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    
    return ConversationResponse(
        id=conv.id,
        workspace_dir=conv.workspace_dir,
        status=conv.status,
        title=conv.title,
        agent_type=conv.agent_type,
        enable_browser=conv.enable_browser,
        created_at=conv.created_at,
    )


@router.get("")
async def list_conversations():
    """List all conversations."""
    cm = get_conversation_manager()
    convs = cm.list_conversations()
    
    return {
        "conversations": [
            {
                "id": c.id,
                "workspace_dir": c.workspace_dir,
                "status": c.status.value,
                "title": c.title,
                "agent_type": c.agent_type.value,
                "enable_browser": c.enable_browser,
                "created_at": c.created_at.isoformat(),
            }
            for c in convs
        ]
    }


@router.post("/{conversation_id}/messages")
async def send_message(conversation_id: str, body: SendMessageRequest):
    """Send a message to a conversation."""
    cm = get_conversation_manager()
    conv = cm.get_conversation(conversation_id)
    
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    
    cm.send_message(conversation_id, body.message, body.role)
    
    return {"status": "ok", "conversation_id": conversation_id}


@router.post("/{conversation_id}/run")
async def run_conversation(conversation_id: str):
    """Run a conversation (process messages)."""
    cm = get_conversation_manager()
    conv = cm.get_conversation(conversation_id)
    
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    
    # Run in background
    import asyncio
    asyncio.create_task(cm.run_conversation(conversation_id))
    
    return {"status": "started", "conversation_id": conversation_id}


@router.get("/{conversation_id}/events")
async def get_events(conversation_id: str):
    """Get events from a conversation."""
    cm = get_conversation_manager()
    conv = cm.get_conversation(conversation_id)
    
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    
    events = cm.get_events(conversation_id)
    return {"events": events}


@router.get("/{conversation_id}/messages")
async def get_messages(conversation_id: str):
    """Get messages from a conversation."""
    cm = get_conversation_manager()
    conv = cm.get_conversation(conversation_id)
    
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    
    messages = cm.get_messages(conversation_id)
    return {"messages": messages}


@router.delete("/{conversation_id}")
async def delete_conversation(conversation_id: str):
    """Delete a conversation."""
    cm = get_conversation_manager()
    conv = cm.get_conversation(conversation_id)
    
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    
    cm.delete_conversation(conversation_id)
    
    return {"status": "deleted", "conversation_id": conversation_id}