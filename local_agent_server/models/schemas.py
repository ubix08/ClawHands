"""Pydantic models for the Local Agent Server."""

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class AgentType(str, Enum):
    """Agent types supported by the server."""

    DEFAULT = "default"
    PLANNING = "plan"
    CODE = "code"


class ConversationStatus(str, Enum):
    """Status of a conversation."""

    CREATED = "created"
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"


class MessageRole(str, Enum):
    """Role of a message sender."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


# ============================================================================
# REQUEST MODELS
# ============================================================================


class CreateConversationRequest(BaseModel):
    """Request to create a new conversation."""

    initial_message: Optional[str] = Field(None, description="Initial message to send to the agent")
    workspace_dir: Optional[str] = Field(None, description="Custom workspace directory")
    agent_type: AgentType = Field(AgentType.DEFAULT, description="Type of agent to create")
    enable_browser: bool = Field(True, description="Enable browser tool")


class SendMessageRequest(BaseModel):
    """Request to send a message to a conversation."""

    message: str = Field(..., min_length=1, description="Message to send")
    role: MessageRole = Field(MessageRole.USER, description="Role of the message sender")


class ExecuteCommandRequest(BaseModel):
    """Request to execute a command in a workspace."""

    command: str = Field(..., min_length=1, description="Command to execute")
    shell: str = Field("/bin/bash", description="Shell to use")
    cwd: Optional[str] = Field(None, description="Working directory")


class UpdateSettingsRequest(BaseModel):
    """Request to update server settings."""

    api_key: Optional[str] = Field(None, description="API key for LLM")
    model: Optional[str] = Field(None, description="LLM model to use")
    enable_browser: Optional[bool] = Field(None, description="Enable browser tool")
    workspace_base_dir: Optional[str] = Field(None, description="Base directory for workspaces")


# ============================================================================
# RESPONSE MODELS
# ============================================================================


class ConversationResponse(BaseModel):
    """Response containing conversation metadata."""

    id: str = Field(..., description="Conversation ID")
    workspace_dir: str = Field(..., description="Workspace directory")
    status: ConversationStatus = Field(..., description="Current status")
    title: str = Field("", description="Conversation title")
    agent_type: AgentType = Field(..., description="Type of agent")
    enable_browser: bool = Field(..., description="Browser enabled")
    created_at: datetime = Field(..., description="Creation timestamp")


class MessageResponse(BaseModel):
    """Response containing a message."""

    id: str = Field(..., description="Message ID")
    role: MessageRole = Field(..., description="Role of sender")
    content: str = Field(..., description="Message content")
    created_at: datetime = Field(..., description="Creation timestamp")


class EventResponse(BaseModel):
    """Response containing an event."""

    type: str = Field(..., description="Event type")
    content: Optional[str] = Field(None, description="Event content")
    action: Optional[str] = Field(None, description="Action name")
    tool: Optional[str] = Field(None, description="Tool used")
    timestamp: datetime = Field(..., description="Event timestamp")


class ExecutionResponse(BaseModel):
    """Response from command execution."""

    exit_code: int = Field(..., description="Exit code")
    stdout: str = Field("", description="Standard output")
    stderr: str = Field("", description="Standard error")


class HealthResponse(BaseModel):
    """Response from health check."""

    status: str = Field(..., description="Server status")
    version: str = Field(..., description="Server version")
    conversations: int = Field(..., description="Number of active conversations")
    api_key_configured: bool = Field(..., description="API key is configured")


class SettingsResponse(BaseModel):
    """Response containing server settings."""

    model: str = Field(..., description="LLM model")
    enable_browser: bool = Field(..., description="Browser enabled")
    workspace_base_dir: str = Field(..., description="Workspace base directory")
    default_agent_type: AgentType = Field(..., description="Default agent type")


# ============================================================================
# ADMIN MODELS
# ============================================================================


class ServerStats(BaseModel):
    """Server statistics."""

    total_conversations: int = Field(0, description="Total conversations created")
    active_conversations: int = Field(0, description="Currently running conversations")
    total_messages: int = Field(0, description="Total messages processed")
    total_events: int = Field(0, description="Total events generated")


class AdminStatsResponse(BaseModel):
    """Admin statistics response."""

    stats: ServerStats = Field(..., description="Server statistics")
    uptime_seconds: float = Field(..., description="Server uptime in seconds")
    version: str = Field(..., description="Server version")