"""Conversation manager service - Manages all conversations."""

import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Any

import logging

from openhands.sdk import Conversation as SDKConversation

from ..models.schemas import AgentType, ConversationStatus, MessageRole
from .agent_factory import get_agent_factory
from .workspace_manager import get_workspace_manager

logger = logging.getLogger(__name__)


@dataclass
class Conversation:
    """Represents a conversation with an agent."""
    
    id: str
    workspace_dir: str
    agent_type: AgentType
    enable_browser: bool
    created_at: datetime = field(default_factory=datetime.now)
    status: ConversationStatus = ConversationStatus.CREATED
    title: str = ""
    agent: Any = None
    sdk_conversation: Optional[SDKConversation] = None
    
    def to_dict(self) -> dict:
        """Convert to dictionary for API responses."""
        return {
            "id": self.id,
            "workspace_dir": self.workspace_dir,
            "status": self.status.value,
            "title": self.title,
            "agent_type": self.agent_type.value,
            "enable_browser": self.enable_browser,
            "created_at": self.created_at,
        }


class ConversationManager:
    """Manages all conversations.
    
    Replicates the conversation handling from:
    openhands/app_server/app_conversation/live_status_app_conversation_service.py
    """
    
    def __init__(
        self,
        workspace_manager: Optional[Any] = None,
        api_key: Optional[str] = None,
    ):
        """Initialize the conversation manager.
        
        Args:
            workspace_manager: Workspace manager instance
            api_key: API key for LLM (falls back to env var)
        """
        self.workspace_manager = workspace_manager or get_workspace_manager()
        self.conversations: dict[str, Conversation] = {}
        
        # Load configuration from environment
        self.api_key = api_key or os.getenv("OPENHANDS_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
        self.model = os.getenv("LLM_MODEL", "anthropic/claude-sonnet-4-5-20250929")
        self.enable_browser = os.getenv("ENABLE_BROWSER", "true").lower() == "true"
        self.default_agent_type = AgentType.DEFAULT
        
        if not self.api_key:
            logger.warning("No API key configured!")
        
        logger.info(f"ConversationManager initialized (api_key_configured={bool(self.api_key)})")
    
    def set_api_key(self, api_key: str) -> None:
        """Set the API key."""
        self.api_key = api_key
    
    def set_model(self, model: str) -> None:
        """Set the default model."""
        self.model = model
    
    def create_conversation(
        self,
        workspace_dir: Optional[str] = None,
        initial_message: Optional[str] = None,
        agent_type: Optional[AgentType] = None,
        enable_browser: Optional[bool] = None,
        model: Optional[str] = None,
    ) -> Conversation:
        """Create a new conversation with an agent.
        
        This replicates:
        - _build_start_conversation_request() from original
        - ConversationSettings.create_request() from original
        """
        if not self.api_key:
            raise ValueError("API key not configured")
        
        # Use provided options or defaults
        agent_type = agent_type or self.default_agent_type
        enable_browser = enable_browser if enable_browser is not None else self.enable_browser
        
        conversation_id = str(uuid.uuid4())
        workspace = workspace_dir or self.workspace_manager.create_workspace(conversation_id)
        
        # Create agent with exact same config as original
        # Replicates lines ~1407-1425 from original
        factory = get_agent_factory()
        agent = factory.create_agent(
            api_key=self.api_key,
            agent_type=agent_type,
            enable_browser=enable_browser,
            model=model or self.model,
        )
        
        # Create SDK conversation (same as original)
        sdk_conversation = SDKConversation(
            agent=agent,
            workspace=workspace,
        )
        
        conversation = Conversation(
            id=conversation_id,
            workspace_dir=workspace,
            agent_type=agent_type,
            enable_browser=enable_browser,
            agent=agent,
            sdk_conversation=sdk_conversation,
        )
        
        # Send initial message if provided
        if initial_message:
            sdk_conversation.send_message(initial_message)
            conversation.title = initial_message[:50]
            conversation.status = ConversationStatus.RUNNING
        
        self.conversations[conversation_id] = conversation
        logger.info(
            f"Created conversation {conversation_id} "
            f"(type={agent_type}, browser={enable_browser})"
        )
        
        return conversation
    
    def get_conversation(self, conversation_id: str) -> Optional[Conversation]:
        """Get a conversation by ID."""
        return self.conversations.get(conversation_id)
    
    def list_conversations(self) -> List[Conversation]:
        """List all conversations."""
        return list(self.conversations.values())
    
    def send_message(self, conversation_id: str, message: str, role: MessageRole = MessageRole.USER) -> None:
        """Send a message to a conversation."""
        conv = self.get_conversation(conversation_id)
        if conv:
            conv.sdk_conversation.send_message(message)
            conv.status = ConversationStatus.RUNNING
    
    def run_conversation(self, conversation_id: str) -> None:
        """Run a conversation (process messages)."""
        conv = self.get_conversation(conversation_id)
        if conv:
            try:
                conv.sdk_conversation.run()
                conv.status = ConversationStatus.STOPPED
            except Exception as e:
                conv.status = ConversationStatus.ERROR
                logger.error(f"Error running conversation {conversation_id}: {e}")
    
    def delete_conversation(self, conversation_id: str) -> None:
        """Delete a conversation."""
        if conversation_id in self.conversations:
            del self.conversations[conversation_id]
            self.workspace_manager.delete_workspace(conversation_id)
            logger.info(f"Deleted conversation {conversation_id}")
    
    def get_events(self, conversation_id: str) -> List[dict]:
        """Get events from a conversation."""
        conv = self.get_conversation(conversation_id)
        if not conv:
            return []
        
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
        return events
    
    def get_messages(self, conversation_id: str) -> List[dict]:
        """Get messages from a conversation."""
        conv = self.get_conversation(conversation_id)
        if not conv:
            return []
        
        messages = []
        for msg in conv.sdk_conversation.state.messages:
            msg_dict = {
                "content": msg.content,
                "role": getattr(msg, "role", "user"),
            }
            messages.append(msg_dict)
        return messages
    
    def get_stats(self) -> dict:
        """Get conversation statistics."""
        total = len(self.conversations)
        active = sum(1 for c in self.conversations.values() if c.status == ConversationStatus.RUNNING)
        return {
            "total_conversations": total,
            "active_conversations": active,
        }


# Factory function for dependency injection
_conversation_manager: Optional[ConversationManager] = None


def get_conversation_manager() -> ConversationManager:
    """Get the global conversation manager instance."""
    global _conversation_manager
    if _conversation_manager is None:
        from .workspace_manager import get_workspace_manager
        _conversation_manager = ConversationManager(
            workspace_manager=get_workspace_manager()
        )
    return _conversation_manager


def set_conversation_manager(manager: ConversationManager) -> None:
    """Set the global conversation manager instance."""
    global _conversation_manager
    _conversation_manager = manager