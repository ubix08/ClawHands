"""Services package for Local Agent Server."""

from local_agent_server.services.agent_factory import AgentFactory, agent_factory, get_agent_factory
from local_agent_server.services.conversation_manager import (
    Conversation,
    ConversationManager,
    get_conversation_manager,
    set_conversation_manager,
)
from local_agent_server.services.workspace_manager import (
    WorkspaceManager,
    get_workspace_manager,
    set_workspace_manager,
)

__all__ = [
    # Agent factory
    "AgentFactory",
    "agent_factory",
    "get_agent_factory",
    # Workspace manager
    "WorkspaceManager",
    "get_workspace_manager",
    "set_workspace_manager",
    # Conversation manager
    "Conversation",
    "ConversationManager",
    "get_conversation_manager",
    "set_conversation_manager",
]