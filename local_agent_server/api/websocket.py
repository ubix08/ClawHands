"""WebSocket handler for real-time chat communication."""

import asyncio
import json
import logging
from typing import Optional

from fastapi import WebSocket, WebSocketDisconnect

from local_agent_server.services import get_conversation_manager

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Manages WebSocket connections for conversations."""
    
    def __init__(self):
        self.active_connections: dict[str, WebSocket] = {}
    
    async def connect(self, conversation_id: str, websocket: WebSocket):
        """Connect a WebSocket to a conversation."""
        await websocket.accept()
        self.active_connections[conversation_id] = websocket
        logger.info(f"WebSocket connected: {conversation_id}")
    
    def disconnect(self, conversation_id: str):
        """Disconnect a WebSocket from a conversation."""
        if conversation_id in self.active_connections:
            del self.active_connections[conversation_id]
            logger.info(f"WebSocket disconnected: {conversation_id}")
    
    async def send_message(self, conversation_id: str, data: dict):
        """Send a message to a conversation's WebSocket."""
        if conversation_id in self.active_connections:
            websocket = self.active_connections[conversation_id]
            await websocket.send_json(data)
    
    async def broadcast(self, conversation_id: str, data: dict):
        """Broadcast a message to all connected clients."""
        await self.send_message(conversation_id, data)


# Global connection manager
connection_manager = ConnectionManager()


async def websocket_endpoint(websocket: WebSocket, conversation_id: str):
    """WebSocket endpoint for real-time conversation streaming.
    
    Usage:
        Connect: ws://host:port/ws/{conversation_id}
        
    Messages:
        - Send message: {"type": "message", "content": "..."}
        - Run: {"type": "run"}
        - Stop: {"type": "stop"}
        
    Responses:
        - {"type": "status", "status": "..."}
        - {"type": "event", "event_type": "...", "content": "..."}
        - {"type": "done", "status": "..."}
        - {"type": "error", "error": "..."}
    """
    cm = get_conversation_manager()
    conv = cm.get_conversation(conversation_id)
    
    if not conv:
        await websocket.send_json({
            "type": "error",
            "error": "Conversation not found"
        })
        await websocket.close()
        return
    
    await connection_manager.connect(conversation_id, websocket)
    
    # Send initial status
    await connection_manager.send_message(conversation_id, {
        "type": "status",
        "status": conv.status.value,
    })
    
    try:
        last_event_count = 0
        
        while conv.status.value == "running":
            # Send new events
            events = cm.get_events(conversation_id)
            
            if len(events) > last_event_count:
                for event in events[last_event_count:]:
                    await connection_manager.send_message(conversation_id, {
                        "type": "event",
                        **event
                    })
                last_event_count = len(events)
            
            await asyncio.sleep(0.1)
        
        # Send final status
        await connection_manager.send_message(conversation_id, {
            "type": "done",
            "status": conv.status.value,
        })
        
    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected: {conversation_id}")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        await connection_manager.send_message(conversation_id, {
            "type": "error",
            "error": str(e)
        })
    finally:
        connection_manager.disconnect(conversation_id)


async def websocket_chat_endpoint(websocket: WebSocket, conversation_id: str):
    """WebSocket endpoint for interactive chat.
    
    This is a simplified version that handles message sending
    and automatically runs the conversation.
    """
    cm = get_conversation_manager()
    conv = cm.get_conversation(conversation_id)
    
    if not conv:
        await websocket.send_json({
            "type": "error",
            "error": "Conversation not found"
        })
        await websocket.close()
        return
    
    await connection_manager.connect(conversation_id, websocket)
    
    try:
        # Send initial status
        await connection_manager.send_message(conversation_id, {
            "type": "status",
            "status": conv.status.value,
            "conversation_id": conversation_id,
        })
        
        while True:
            # Receive message from client
            data = await websocket.receive_json()
            msg_type = data.get("type")
            
            if msg_type == "message":
                # Send message to agent
                content = data.get("content", "")
                if content:
                    cm.send_message(conversation_id, content)
                    
                    await connection_manager.send_message(conversation_id, {
                        "type": "message_sent",
                        "content": content,
                    })
                    
                    # Run the conversation
                    cm.run_conversation(conversation_id)
                    
                    # Stream events while running
                    last_count = 0
                    while conv.status.value == "running":
                        events = cm.get_events(conversation_id)
                        if len(events) > last_count:
                            for event in events[last_count:]:
                                await connection_manager.send_message(conversation_id, {
                                    "type": "event",
                                    **event
                                })
                            last_count = len(events)
                        await asyncio.sleep(0.1)
                    
                    # Send completion status
                    await connection_manager.send_message(conversation_id, {
                        "type": "complete",
                        "status": conv.status.value,
                    })
            
            elif msg_type == "ping":
                await connection_manager.send_message(conversation_id, {
                    "type": "pong"
                })
            
            else:
                await connection_manager.send_message(conversation_id, {
                    "type": "error",
                    "error": f"Unknown message type: {msg_type}"
                })
    
    except WebSocketDisconnect:
        logger.info(f"Chat WebSocket disconnected: {conversation_id}")
    except Exception as e:
        logger.error(f"Chat WebSocket error: {e}")
        await connection_manager.send_message(conversation_id, {
            "type": "error",
            "error": str(e)
        })
    finally:
        connection_manager.disconnect(conversation_id)