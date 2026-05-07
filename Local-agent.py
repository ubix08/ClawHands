"""local_runtime.py — ClawHands Local Runtime
OpenHands agent_server-compatible, zero-docker, pure-SDK implementation.

FIXES APPLIED (this version)
==============================
P0-A  startup() double-call — _started guard prevents double-load
P0-B  str(sdk_evt) repr strings — proper field extraction from SDK events
P0-C  No agent persistence note — runtime-level warning added; agents are
      in-memory by design; conversations now store enough to give clear errors
P0-D  EventStore.get() since/limit order — since filter applied BEFORE limit
P0-E  Initial message race — asyncio.sleep(0.1) before first SSE fire;
      history replay makes this safe in practice
P0-F  _api_keys/_tos/_onboarding in closure — moved to app.state
P1-A  extra_context still concat'd — use ConversationSettings.extra_context
      when available; fall back to system prompt injection, not user turn
P1-B  _build_sdk_agent HookConfig() crash — guarded same as HooksManager
P1-C  Skill source field — guarded with try/except + hasattr
P1-D  Workspace per-agent not per-conversation — path is now agent/conv_id
P1-E  No multi-turn history to SDK — history rebuilt as context each turn
P1-F  MCP servers not passed to SDK Agent — fetched and passed if Agent
      accepts mcp_servers kwarg
P2-A  Dual event systems — WebhookManager called directly from _fire_sse
P2-B  HooksManager dead code noted — hooks wired into send_message loop
P2-C  GitProvider stubs — GITLAB/BITBUCKET/AZURE raise NotImplementedError
P2-D  search_conversations — SQL LIKE filter pushed to DB
P2-E  Pydantic models inside create_app — promoted to module level
P2-F  ZIP blocking I/O — wrapped in asyncio.to_thread
P2-G  read_text blocking I/O — wrapped in asyncio.to_thread
P3-A  skills_search_router + skills_router same prefix — merged
P3-B  Three send-message pathways — POST /events removed; only
      POST /send-message and WebSocket remain
P3-C  Route ordering comment added
P3-D  BYOR path moved to /api/llm/configure
P3-E  export_security_trace N*M DB calls — single JOIN query
P3-F  201 Created on resource-creation routes
P3-G  lifespan close() async cleanup — cancels SSE background tasks
P4-A  SSE generator cancellation — asyncio.wait_for inner task issue noted;
      timeout re-structured with CancelledError guard
P4-B  SSE history vs live format mismatch — unified _event_to_wire_dict()
      used by both DB serialiser and live emitter
P4-C  WebSocket missing history replay — added
P4-D  WebSocket swallows non-disconnect errors — broad except added
P4-E  conversation_id absent from SSE wire format — added
P5-A  BYOR acronym clarified in docstring
P5-B  _rt() guard — AttributeError guard added
P5-C  page_id cursor param — documented as not-yet-implemented
P5-D  DELETE /secrets 404 vs DELETE /settings success:false — unified to 404
P5-E  ConversationRole used in _histories — replaced raw strings with enum
P5-F  httpx import inside _on_event — moved to module level
P5-G  StreamingResponse alias — import at module level
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import copy
import zipfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator, Optional, Union
from uuid import uuid4

# ── third-party ──────────────────────────────────────────────────────────────
import httpx          # P5-F: module-level import
import yaml

# ── Load .env file ────────────────────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv()

# ── SQLAlchemy 2.x ───────────────────────────────────────────────────────────
from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey,
    Integer, JSON, String, Text,
    create_engine, select, delete, update,
    func, or_, text,
)
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker, relationship
from sqlalchemy.pool import StaticPool

# ── OpenHands SDK 1.19.1 ─────────────────────────────────────────────────────
from openhands.sdk import Agent, AgentContext
from openhands.sdk import TextContent as SDKTextContent
from openhands.sdk.llm import LLM
from openhands.sdk.workspace import LocalWorkspace
from openhands.sdk.settings import AgentSettings, ConversationSettings
from openhands.sdk.secret import StaticSecret
from openhands.sdk.skills import Skill, KeywordTrigger, TaskTrigger
from openhands.sdk.hooks import HookConfig

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# UTC helper
# ─────────────────────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ============================================================================
# DATABASE
# ============================================================================

class Base(DeclarativeBase):
    pass


class DBConversation(Base):
    __tablename__ = "conversations"

    id                  = Column(String, primary_key=True)
    agent_id            = Column(String, nullable=True)
    title               = Column(String, nullable=True)
    agent_type          = Column(String, default="default")
    selected_repository = Column(String, nullable=True)
    git_provider        = Column(String, nullable=True)
    selected_branch     = Column(String, nullable=True)
    created_at          = Column(DateTime(timezone=True), default=_utcnow)
    updated_at          = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
    user_id             = Column(String, default="local")
    meta_data           = Column(JSON, nullable=True)

    events = relationship("DBEvent", back_populates="conversation",
                          cascade="all, delete-orphan", passive_deletes=True)


class DBEvent(Base):
    __tablename__ = "events"

    id              = Column(String, primary_key=True)
    conversation_id = Column(String, ForeignKey("conversations.id", ondelete="CASCADE"))
    event_type      = Column(String)
    timestamp       = Column(DateTime(timezone=True), default=_utcnow)
    content         = Column(Text)
    action_type     = Column(String, nullable=True)
    source          = Column(String, nullable=True)
    # P4-B: thought stored so history replay matches live format
    thought         = Column(Text, nullable=True)
    meta_data       = Column(JSON, nullable=True)

    conversation = relationship("DBConversation", back_populates="events")


class DBSetting(Base):
    __tablename__ = "settings"

    key        = Column(String, primary_key=True)
    value      = Column(Text)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class DBSecret(Base):
    __tablename__ = "secrets"

    name       = Column(String, primary_key=True)
    value      = Column(Text)
    created_at = Column(DateTime(timezone=True), default=_utcnow)


class DBMCPConfig(Base):
    __tablename__ = "mcp_configs"

    id      = Column(String, primary_key=True)
    name    = Column(String)
    command = Column(String)
    args    = Column(JSON, nullable=True)
    env     = Column(JSON, nullable=True)
    enabled = Column(Boolean, default=True)


class DatabaseManager:
    """
    Thread-safe SQLite manager.
    All DB operations dispatched via asyncio.to_thread() because SQLAlchemy's
    synchronous Session is not safe to run on the asyncio event loop thread.
    """

    def __init__(self, db_path: str = "./local_runtime.db"):
        self.db_path = db_path
        self.engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self._migrate()

    def _migrate(self) -> None:
        """Run migrations to add new columns to existing tables."""
        with self.engine.connect() as conn:
            # Add thought column to events table if missing
            try:
                conn.execute(text("SELECT thought FROM events LIMIT 1"))
            except OperationalError:
                conn.execute(text("ALTER TABLE events ADD COLUMN thought TEXT"))
                conn.commit()
            except Exception:
                pass

        self._SessionLocal = sessionmaker(
            bind=self.engine, autocommit=False, autoflush=False
        )

    def _session(self) -> Session:
        return self._SessionLocal()

    def run_sync(self, fn):
        sess = self._session()
        try:
            result = fn(sess)
            sess.commit()
            return result
        except Exception:
            sess.rollback()
            raise
        finally:
            sess.close()

    async def run(self, fn):
        """Async wrapper: runs fn(session) in a thread-pool thread."""
        return await asyncio.to_thread(self.run_sync, fn)

    def close(self):
        self.engine.dispose()


# ============================================================================
# ENUMS
# ============================================================================

class AgentState(Enum):
    CREATED        = "created"
    STARTING       = "starting"
    RUNNING        = "running"
    AWAITING_INPUT = "awaiting_input"
    PAUSED         = "paused"
    STOPPED        = "stopped"
    ERROR          = "error"


class ProviderType(Enum):
    GITHUB    = "github"
    GITLAB    = "gitlab"      # P2-C: stub raises NotImplementedError
    BITBUCKET = "bitbucket"   # P2-C: stub raises NotImplementedError
    AZURE     = "azure"       # P2-C: stub raises NotImplementedError


class ConversationRole(Enum):  # P5-E: now actually used in _histories
    USER      = "user"
    ASSISTANT = "assistant"
    SYSTEM    = "system"


# ============================================================================
# DOMAIN MODELS
# ============================================================================

@dataclass
class ContentBlock:
    type: str = "text"
    text: str = ""

    @classmethod
    def from_sdk(cls, sdk_content: SDKTextContent) -> "ContentBlock":
        return cls(type="text", text=sdk_content.text)


@dataclass
class Message:
    role:      ConversationRole
    content:   list[ContentBlock]
    timestamp: datetime = field(default_factory=_utcnow)


@dataclass
class Event:
    id:              str      = field(default_factory=lambda: str(uuid4()))
    conversation_id: str      = ""
    timestamp:       datetime = field(default_factory=_utcnow)


@dataclass
class ActionEvent(Event):
    action_type: str = ""
    thought:     str = ""
    content:     str = ""
    observation: str = ""


@dataclass
class ObservationEvent(Event):
    content: str = ""
    source:  str = ""


@dataclass
class AgentInfo:
    id:         str
    name:       str
    agent_type: str
    state:      AgentState
    created_at: datetime


# ============================================================================
# PROMPTS
# ============================================================================

PLANNING_AGENT_INSTRUCTION = """<IMPORTANT_PLANNING_BOUNDARIES>
You are a Planning Agent that can ONLY create plans – you cannot execute code or make changes.

After you finalize the plan in PLAN.md:
- Do NOT ask "Ready to proceed?" or offer to execute the plan
- Do NOT attempt to run any implementation commands
- Instead, inform the user they have two options to proceed:
  1. Click the **Build** button below the plan preview – this will automatically switch to
     the code agent and instruct it to execute the plan.
  2. Switch to the code agent manually, then send a message instructing it to execute the plan.

Your role ends when the plan is finalized. Implementation is handled by the code agent.
</IMPORTANT_PLANNING_BOUNDARIES>"""

DEFAULT_SYSTEM_MESSAGE = """You are an AI software development agent. You operate in a workflow that allows you to interact with a file system, run commands, and browse the web.

Capabilities:
- Read, write, and execute files using the file_editor or bash tools
- Run shell commands using the bash tool
- Browse websites using the browser tool

IMPORTANT:
- NEVER use the 'think' tool - it only logs thoughts and does NOT execute actions
- ALWAYS use bash, file_editor, or browser tools to accomplish tasks
- The think tool does NOT create files or run commands - it only logs thoughts

Guidelines:
- Use 'bash' tool to run shell commands (e.g., echo, python, etc.)
- Use 'file_editor' tool to read/write files
- Test your solutions
- Stay within the user's constraints"""


# ============================================================================
# MODULE-LEVEL LLM HELPER
# ============================================================================

def make_llm(
    model:    str | None = None,
    api_key:  str | None = None,
    base_url: str | None = None,
    thinking: bool = True,
) -> LLM | None:
    """Construct an LLM from explicit params, falling back to env vars."""
    # First try explicit params / OpenHands-specific env vars
    model = model or os.environ.get("OPENHANDS_LLM_MODEL")
    api_key = api_key or os.environ.get("OPENHANDS_LLM_API_KEY", "")
    base_url = base_url or os.environ.get("OPENHANDS_LLM_BASE_URL")
    
    # Fall back to common OpenAI env vars if no OpenHands config
    if not model:
        model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    if not api_key:
        api_key = os.environ.get("OPENAI_API_KEY", "")
    if not base_url:
        base_url = os.environ.get("OPENAI_BASE_URL")
    
    # Map custom models to openai-compatible endpoints
    if model and base_url and "nvidia" in base_url.lower():
        # Z-ai, Deepseek, etc on NVIDIA Use openai-compatible format
        model = f"openai/{model}"
    
    if not model or not api_key:
        return None
    
    # Build extra_body for thinking (Z-ai, Claude, etc.)
    extra_body = {}
    if thinking:
        extra_body = {"chat_template_kwargs": {"enable_thinking": True, "clear_thinking": False}}
    
    return LLM(model=model, api_key=api_key, base_url=base_url, litellm_extra_body=extra_body)


# ============================================================================
# TEXT EXTRACTOR
# ============================================================================

def extract_text(msg: Any) -> str:
    """Normalise any OpenHands message shape to a plain string."""
    if isinstance(msg, str):
        return msg
    if isinstance(msg, list):
        parts = [extract_text(item) for item in msg]
        return " ".join(p for p in parts if p)
    if isinstance(msg, dict):
        for key in ("text", "message"):
            if key in msg:
                return extract_text(msg[key])
        content = msg.get("content")
        if content is not None:
            return extract_text(content)
    return str(msg) if msg is not None else ""


# ============================================================================
# SDK EVENT CONTENT EXTRACTOR  (P0-B)
# ============================================================================

def _extract_sdk_content(sdk_evt: Any) -> str:
    """
    P0-B fix: extract actual text from a typed SDK event rather than
    stringifying the whole object (which gives repr garbage like
    "<MessageAction thought='...' content='...'>").

    Priority order:
      1. .message   — SDK MessageAction / final response
      2. .content   — most action types
      3. .thought   — reasoning/planning events
      4. .output    — CmdOutputObservation etc.
      5. .text      — generic text field
      6. str()      — last resort (repr)
    """
    for attr in ("message", "content", "thought", "output", "text"):
        val = getattr(sdk_evt, attr, None)
        if val and isinstance(val, str):
            return val
        # Handle list-of-content-block format
        if val and isinstance(val, list):
            parts = []
            for item in val:
                if isinstance(item, str):
                    parts.append(item)
                elif hasattr(item, "text"):
                    parts.append(item.text)
                elif isinstance(item, dict):
                    parts.append(item.get("text") or item.get("content") or "")
            result = " ".join(p for p in parts if p)
            if result:
                return result
    return str(sdk_evt)


def _extract_sdk_thought(sdk_evt: Any) -> str:
    """Extract the thought/reasoning from an SDK event."""
    return getattr(sdk_evt, "thought", "") or ""


# ============================================================================
# UNIFIED WIRE-FORMAT SERIALISER  (P4-B)
# ============================================================================

def _event_to_wire_dict(
    event_id:        str,
    conversation_id: str,
    event_type:      str,
    timestamp:       str,
    content:         str,
    action_type:     str = "",
    source:          str = "",
    thought:         str = "",
) -> dict:
    """
    P4-B: single serialiser used by BOTH history replay and live SSE/WS
    so the frontend always receives the same schema.
    """
    return {
        "id":              event_id,
        "conversation_id": conversation_id,   # P4-E: always included
        "type":            event_type,
        "timestamp":       timestamp,
        "content":         content,
        "action_type":     action_type,
        "source":          source,
        "thought":         thought,
    }


# ============================================================================
# PYDANTIC REQUEST MODELS
# ============================================================================
# These are defined at module level to ensure they're fully resolved before
# FastAPI route handlers use them. The original lazy-loading approach caused
# FastAPI to incorrectly treat request models as query parameters.

from pydantic import BaseModel, ConfigDict
from typing import Optional, List, Union, Any

class ConversationCreateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: Optional[str] = None
    agent_id: Optional[str] = None
    agent_type: Optional[str] = "default"
    selected_repository: Optional[str] = None
    git_provider: Optional[str] = None
    selected_branch: Optional[str] = None
    initial_message: Optional[Union[str, dict]] = None
    llm_model: Optional[str] = None
    llm_api_key: Optional[str] = None
    llm_base_url: Optional[str] = None
    system_message_suffix: Optional[str] = None


class ConversationUpdateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: Optional[str] = None
    selected_branch: Optional[str] = None


class AgentCreateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str
    agent_type: Optional[str] = "code"
    llm_model: Optional[str] = None
    llm_api_key: Optional[str] = None
    llm_base_url: Optional[str] = None
    system_message: Optional[str] = None


class AgentLLMRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    llm_model: str
    llm_api_key: Optional[str] = None
    llm_base_url: Optional[str] = None


class SendMessageRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    message: Union[str, dict]


class SecretCreateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str
    value: str


class MCPConfigRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str
    command: str
    args: Optional[List[str]] = None
    env: Optional[dict] = None


class SettingSetRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    key: str
    value: Any


class WebhookRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    event_type: str
    url: str


class APIKeyCreateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: Optional[str] = "default"


class LLMBYORRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    model: str
    api_key: str
    base_url: Optional[str] = None


class AcceptTOSRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    accepted: bool = True


class CompleteOnboardingRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')


class SecurityPolicyRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    policy: dict


# Pydantic model namespace - used by create_app to assign to local variables
_pydantic_ns: dict[str, Any] = {
    "ConversationCreateRequest": ConversationCreateRequest,
    "ConversationUpdateRequest": ConversationUpdateRequest,
    "AgentCreateRequest": AgentCreateRequest,
    "AgentLLMRequest": AgentLLMRequest,
    "SendMessageRequest": SendMessageRequest,
    "SecretCreateRequest": SecretCreateRequest,
    "MCPConfigRequest": MCPConfigRequest,
    "SettingSetRequest": SettingSetRequest,
    "WebhookRequest": WebhookRequest,
    "APIKeyCreateRequest": APIKeyCreateRequest,
    "LLMBYORRequest": LLMBYORRequest,
    "AcceptTOSRequest": AcceptTOSRequest,
    "CompleteOnboardingRequest": CompleteOnboardingRequest,
    "SecurityPolicyRequest": SecurityPolicyRequest,
}


def _ensure_pydantic_models() -> None:
    """No-op - models are now defined at module level."""
    pass


# ============================================================================
# SKILL LOADER
# ============================================================================

class SkillLoader:
    """Load and match skills using the SDK 1.19.1 Skill / Trigger API."""

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def _make_trigger(
        self, triggers: list[str] | None
    ) -> KeywordTrigger | TaskTrigger | None:
        if not triggers:
            return None
        if any(t.startswith("/") for t in triggers):
            return TaskTrigger(triggers=triggers)
        return KeywordTrigger(keywords=triggers)

    def load_skill(
        self,
        name:     str,
        content:  str,
        triggers: list[str] | None = None,
        source:   str = "local",
    ) -> Skill:
        # P1-C: guard against SDK Skill not accepting `source` kwarg
        try:
            skill = Skill(
                name=name,
                content=content,
                trigger=self._make_trigger(triggers),
                source=source,
            )
        except TypeError:
            skill = Skill(
                name=name,
                content=content,
                trigger=self._make_trigger(triggers),
            )
            # Attach source as a plain attribute for our own use
            try:
                skill.source = source  # type: ignore[attr-defined]
            except (AttributeError, TypeError):
                pass
        self._skills[name] = skill
        logger.debug("Loaded skill '%s' from '%s'", name, source)
        return skill

    async def load_skills_from_directory(self, skills_dir: str) -> list[Skill]:
        dir_path = Path(skills_dir)
        if not dir_path.exists():
            logger.warning("Skills directory does not exist: %s", skills_dir)
            return []

        loaded: list[Skill] = []
        for skill_file in dir_path.rglob("*.md"):
            if skill_file.name.upper() == "README.MD":
                continue
            try:
                raw = skill_file.read_text(encoding="utf-8")
                name = skill_file.stem
                triggers: list[str] = []

                if raw.startswith("---"):
                    parts = raw.split("---", 2)
                    if len(parts) >= 3:
                        fm = yaml.safe_load(parts[1]) or {}
                        if isinstance(fm, dict):
                            triggers = fm.get("triggers", [])
                        raw = parts[2].strip()

                skill = self.load_skill(
                    name=name,
                    content=raw,
                    triggers=triggers,
                    source=str(skills_dir),
                )
                loaded.append(skill)
            except Exception as exc:
                logger.warning("Failed to load skill %s: %s", skill_file, exc)

        return loaded

    async def load_all_skills(
        self,
        load_public:  bool = True,
        load_user:    bool = True,
        load_project: bool = True,
        project_dir:  str | None = None,
        repo_root:    str | None = None,
    ) -> list[Skill]:
        """Load skills from all standard locations."""
        all_skills: list[Skill] = []

        if repo_root is None:
            repo_root = str(Path(__file__).resolve().parent)

        if load_public:
            for candidate in [
                Path(repo_root) / "skills",
                Path(repo_root) / "microagents",
            ]:
                if candidate.exists():
                    loaded = await self.load_skills_from_directory(str(candidate))
                    all_skills.extend(loaded)
                    logger.info("Loaded %d public skills from %s", len(loaded), candidate)

        if load_user:
            for user_dir in [
                Path.home() / ".openhands" / "microagents",
                Path.home() / ".clawd"    / "skills",
            ]:
                if user_dir.exists():
                    loaded = await self.load_skills_from_directory(str(user_dir))
                    all_skills.extend(loaded)
                    logger.info("Loaded %d user skills from %s", len(loaded), user_dir)

        if load_project and project_dir:
            proj_dir = Path(project_dir) / ".openhands" / "microagents"
            if proj_dir.exists():
                loaded = await self.load_skills_from_directory(str(proj_dir))
                all_skills.extend(loaded)
                logger.info("Loaded %d project skills from %s", len(loaded), proj_dir)

        logger.info("Total skills loaded: %d", len(all_skills))
        return all_skills

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def list(self) -> list[Skill]:
        return list(self._skills.values())

    def match(self, message: str) -> list[Skill]:
        matched: list[Skill] = []
        msg_lower = message.lower()
        for skill in self._skills.values():
            trigger = getattr(skill, "trigger", None)
            if trigger is None:
                continue
            keywords: list[str] = (
                getattr(trigger, "keywords", None)
                or getattr(trigger, "triggers", None)
                or []
            )
            if any(kw.lower() in msg_lower for kw in keywords):
                matched.append(skill)
        return matched

    def match_first(self, message: str) -> Skill | None:
        results = self.match(message)
        return results[0] if results else None


# ============================================================================
# HOOKS MANAGER
# ============================================================================

class HooksManager:
    def __init__(self) -> None:
        try:
            self._hooks = HookConfig()
        except TypeError:
            try:
                self._hooks = HookConfig(pre_tool_use=[], post_tool_use=[])
            except Exception:
                self._hooks = None  # type: ignore[assignment]

    def set_hooks(self, hooks: HookConfig) -> None:
        self._hooks = hooks

    def get_hooks(self) -> HookConfig | None:
        return self._hooks

    async def trigger_pre_tool_use(self, tool_name: str, tool_input: dict) -> bool:
        """P2-B: hooks now wired into send_message loop."""
        if not self._hooks:
            return True
        for matcher in (self._hooks.pre_tool_use or []):
            if matcher.matcher and matcher.matcher.lower() not in tool_name.lower():
                continue
            for hook in (matcher.hooks or []):
                if not hook.command:
                    continue
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *hook.command,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    payload = json.dumps({"tool": tool_name, "input": tool_input}).encode()
                    _, _ = await asyncio.wait_for(
                        proc.communicate(payload),
                        timeout=getattr(hook, "timeout", None) or 30,
                    )
                    if proc.returncode != 0:
                        logger.warning("Pre-hook blocked tool '%s'", tool_name)
                        return False
                except Exception as exc:
                    logger.error("Pre-hook error for '%s': %s", tool_name, exc)
                    return False
        return True

    async def trigger_post_tool_use(
        self, tool_name: str, tool_input: dict, output: str
    ) -> None:
        if not self._hooks:
            return
        for matcher in (self._hooks.post_tool_use or []):
            if matcher.matcher and matcher.matcher.lower() not in tool_name.lower():
                continue
            for hook in (matcher.hooks or []):
                if not hook.command:
                    continue
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *hook.command,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    payload = json.dumps(
                        {"tool": tool_name, "input": tool_input, "output": output}
                    ).encode()
                    await asyncio.wait_for(
                        proc.communicate(payload),
                        timeout=getattr(hook, "timeout", None) or 30,
                    )
                except Exception as exc:
                    logger.error("Post-hook error for '%s': %s", tool_name, exc)


# ============================================================================
# GIT PROVIDER
# ============================================================================

class GitProvider:
    def __init__(self, provider_type: ProviderType, token: str | None = None):
        self.provider_type = provider_type
        self.token = token

    def _require_github(self) -> None:
        """P2-C: raise clearly for unimplemented providers."""
        if self.provider_type != ProviderType.GITHUB:
            raise NotImplementedError(
                f"Provider '{self.provider_type.value}' is not yet implemented. "
                "Only GitHub is currently supported."
            )

    async def _gh(self, *args: str) -> dict | list | None:
        env = {**os.environ}
        if self.token:
            env["GITHUB_TOKEN"] = self.token
        try:
            proc = await asyncio.create_subprocess_exec(
                "gh", *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode != 0:
                logger.warning("gh command failed: %s", stderr.decode())
                return None
            return json.loads(stdout.decode())
        except Exception as exc:
            logger.error("gh command error: %s", exc)
            return None

    async def get_user(self) -> dict:
        self._require_github()
        data = await self._gh("api", "user")
        if data:
            return {
                "login": data.get("login"),
                "email": data.get("email"),
                "name":  data.get("name"),
            }
        return {}

    async def list_repos(self, page: int = 1) -> list[dict]:
        self._require_github()
        data = await self._gh(
            "repo", "list",
            "--limit", "30",
            "--json", "name,owner,url",
        )
        if data:
            return [
                {"name": r["name"], "owner": r["owner"]["login"], "url": r["url"]}
                for r in data
            ]
        return []

    async def get_repo(self, repo: str) -> dict:
        self._require_github()
        data = await self._gh(
            "repo", "view", repo,
            "--json", "name,owner,url,defaultBranchRef,description",
        )
        return data or {}

    async def list_branches(self, repo: str) -> list[dict]:
        """Use gh api instead of broken --json refs flag."""
        self._require_github()
        data = await self._gh(
            "api", f"repos/{repo}/branches",
            "--paginate",
        )
        if isinstance(data, list):
            return [
                {"name": b.get("name", ""), "sha": b.get("commit", {}).get("sha", "")}
                for b in data
            ]
        return []


# ============================================================================
# EVENT EMITTER
# ============================================================================

class RuntimeEventEmitter:
    def __init__(self) -> None:
        self._subscribers: list[Any] = []

    def subscribe(self, callback: Any) -> None:
        self._subscribers.append(callback)

    def unsubscribe(self, callback: Any) -> None:
        try:
            self._subscribers.remove(callback)
        except ValueError:
            pass

    async def emit(self, event: Event) -> None:
        for cb in list(self._subscribers):
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(event)
                else:
                    cb(event)
            except Exception as exc:
                logger.warning("Event subscriber error: %s", exc)


# ============================================================================
# MCP CONFIGURATION
# ============================================================================

@dataclass
class MCPServerConfig:
    id:      str
    name:    str
    command: str
    args:    list[str]      = field(default_factory=list)
    env:     dict[str, str] = field(default_factory=dict)
    enabled: bool           = True


class MCPManager:
    def __init__(self, db: DatabaseManager) -> None:
        self._db = db
        self._servers: dict[str, MCPServerConfig] = {}

    async def load(self) -> None:
        def _load(sess: Session):
            return [
                MCPServerConfig(
                    id=row.id, name=row.name, command=row.command,
                    args=row.args or [], env=row.env or {}, enabled=row.enabled,
                )
                for row in sess.execute(select(DBMCPConfig)).scalars()
            ]
        rows = await self._db.run(_load)
        self._servers = {r.id: r for r in rows}

    async def add(
        self, name: str, command: str,
        args: list[str] | None = None, env: dict | None = None,
    ) -> str:
        sid = str(uuid4())
        cfg = MCPServerConfig(id=sid, name=name, command=command,
                              args=args or [], env=env or {})
        self._servers[sid] = cfg

        def _insert(sess: Session):
            sess.add(DBMCPConfig(id=sid, name=name, command=command,
                                 args=args, env=env, enabled=True))
        await self._db.run(_insert)
        return sid

    def get(self, sid: str) -> MCPServerConfig | None:
        return self._servers.get(sid)

    def list(self) -> list[MCPServerConfig]:
        return [s for s in self._servers.values() if s.enabled]

    async def remove(self, sid: str) -> bool:
        if sid not in self._servers:
            return False
        del self._servers[sid]

        def _delete(sess: Session):
            sess.execute(delete(DBMCPConfig).where(DBMCPConfig.id == sid))
        await self._db.run(_delete)
        return True


# ============================================================================
# SETTINGS MANAGER
# ============================================================================

class SettingsManager:
    def __init__(self, db: DatabaseManager) -> None:
        self._db = db
        self._data: dict[str, Any] = {}

    async def load(self) -> None:
        def _load(sess: Session):
            rows = list(sess.execute(select(DBSetting)).scalars())
            # P0-F: Extract values INSIDE session to avoid detached instance error
            return [{"key": row.key, "value": row.value} for row in rows]
        
        data = await self._db.run(_load)
        for row in data:
            try:
                self._data[row["key"]] = json.loads(row["value"])
            except json.JSONDecodeError:
                self._data[row["key"]] = row["value"]

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    async def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        serialized = json.dumps(value) if not isinstance(value, str) else value

        def _upsert(sess: Session):
            existing = sess.execute(
                select(DBSetting).where(DBSetting.key == key)
            ).scalar_one_or_none()
            if existing:
                existing.value = serialized
                existing.updated_at = _utcnow()
            else:
                sess.add(DBSetting(key=key, value=serialized))
        await self._db.run(_upsert)

    async def delete(self, key: str) -> bool:
        if key not in self._data:
            return False
        del self._data[key]

        def _del(sess: Session):
            sess.execute(delete(DBSetting).where(DBSetting.key == key))
        await self._db.run(_del)
        return True

    @property
    def all(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)


# ============================================================================
# SECRETS MANAGER
# ============================================================================

class SecretsManager:
    def __init__(self, db: DatabaseManager) -> None:
        self._db = db
        self._secrets: dict[str, StaticSecret] = {}

    async def load(self) -> None:
        def _load(sess: Session):
            rows = list(sess.execute(select(DBSecret)).scalars())
            # P0-F: Extract values INSIDE session to avoid detached instance error
            return [{"name": row.name, "value": row.value} for row in rows]
        
        data = await self._db.run(_load)
        for row in data:
            self._secrets[row["name"]] = StaticSecret(value=row["value"])

    def _raw(self, name: str) -> str | None:
        secret = self._secrets.get(name)
        if secret is None:
            return None
        v = getattr(secret, "value", None)
        if v is None:
            return None
        if hasattr(v, "get_secret_value"):
            return v.get_secret_value()
        return str(v)

    async def set(self, name: str, value: str) -> None:
        self._secrets[name] = StaticSecret(value=value)

        def _upsert(sess: Session):
            existing = sess.execute(
                select(DBSecret).where(DBSecret.name == name)
            ).scalar_one_or_none()
            if existing:
                existing.value = value
            else:
                sess.add(DBSecret(name=name, value=value))
        await self._db.run(_upsert)

    def get(self, name: str) -> str | None:
        return self._raw(name)

    async def delete(self, name: str) -> bool:
        if name not in self._secrets:
            return False
        del self._secrets[name]

        def _del(sess: Session):
            sess.execute(delete(DBSecret).where(DBSecret.name == name))
        await self._db.run(_del)
        return True

    def list_names(self) -> list[str]:
        return list(self._secrets.keys())


# ============================================================================
# EVENT STORE  (P0-D, P4-B)
# ============================================================================

class EventStore:
    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    async def add(
        self,
        conversation_id: str,
        event_type:      str,
        content:         str,
        action_type:     str | None = None,
        source:          str | None = None,
        thought:         str | None = None,   # P4-B: stored for unified replay
        meta_data:       dict | None = None,
    ) -> str:
        eid = str(uuid4())
        now = _utcnow()

        def _insert(sess: Session):
            sess.add(DBEvent(
                id=eid,
                conversation_id=conversation_id,
                event_type=event_type,
                timestamp=now,
                content=content,
                action_type=action_type,
                source=source,
                thought=thought,
                meta_data=meta_data,
            ))
            row = sess.execute(
                select(DBConversation).where(DBConversation.id == conversation_id)
            ).scalar_one_or_none()
            if row:
                row.updated_at = now
        await self._db.run(_insert)
        return eid

    async def get(
        self,
        conversation_id: str,
        since: datetime | None = None,
        limit: int = 500,          # P0-D: raised default; since applied before limit
    ) -> list[dict]:
        def _query(sess: Session):
            # P0-D: build WHERE clauses FIRST, THEN limit
            stmt = (
                select(DBEvent)
                .where(DBEvent.conversation_id == conversation_id)
            )
            if since:
                stmt = stmt.where(DBEvent.timestamp > since)
            stmt = stmt.order_by(DBEvent.timestamp).limit(limit)
            rows = sess.execute(stmt).scalars().all()
            # Extract values INSIDE session to avoid DetachedInstanceError
            return [
                {
                    "id": r.id,
                    "conversation_id": r.conversation_id,
                    "event_type": r.event_type,
                    "timestamp": r.timestamp,
                    "content": r.content,
                    "action_type": r.action_type,
                    "source": r.source,
                    "thought": r.thought,
                    "meta_data": r.meta_data,
                }
                for r in rows
            ]

        # Now extract to dicts AFTER session closes
        rows = await self._db.run(_query)
        return [
            _event_to_wire_dict(
                event_id        = r["id"],
                conversation_id = r["conversation_id"],
                event_type      = r["event_type"],
                timestamp       = r["timestamp"].isoformat() if r["timestamp"] else "",
                content         = r["content"] or "",
                action_type     = r["action_type"] or "",
                source          = r["source"] or "",
                thought         = r["thought"] or "",
            )
            for r in rows
        ]

    async def get_all_recent(self, limit: int = 500) -> list[dict]:
        """
        P3-E: single JOIN query to fetch recent events across all conversations.
        Replaces the O(N*M) loop in export_security_trace.
        """
        def _query(sess: Session):
            stmt = (
                select(DBEvent)
                .order_by(DBEvent.timestamp.desc())
                .limit(limit)
            )
            return sess.execute(stmt).scalars().all()

        rows = await self._db.run(_query)
        return [
            _event_to_wire_dict(
                event_id        = r.id,
                conversation_id = r.conversation_id,
                event_type      = r.event_type,
                timestamp       = r.timestamp.isoformat() if r.timestamp else "",
                content         = r.content or "",
                action_type     = r.action_type or "",
                source          = r.source or "",
                thought         = r.thought or "",
            )
            for r in rows
        ]


# ============================================================================
# WEBHOOK MANAGER  (P2-A: no longer uses RuntimeEventEmitter; called directly)
# ============================================================================

class WebhookManager:
    def __init__(self) -> None:
        self._webhooks: dict[str, str] = {}

    async def on_event(self, event: Event) -> None:
        """P2-A: called directly from _fire_sse, not via RuntimeEventEmitter."""
        event_type = type(event).__name__.lower()
        url = self._webhooks.get(event_type)
        if not url:
            return
        try:
            # P5-F: httpx imported at module level
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(url, json={
                    "event_type":      event_type,
                    "conversation_id": event.conversation_id,
                    "timestamp":       event.timestamp.isoformat(),
                    "data": {
                        "content":     getattr(event, "content", ""),
                        "action_type": getattr(event, "action_type", ""),
                    },
                })
        except Exception as exc:
            logger.error("Webhook error for '%s': %s", event_type, exc)

    def register(self, event_type: str, url: str) -> None:
        self._webhooks[event_type] = url

    def unregister(self, event_type: str) -> bool:
        return bool(self._webhooks.pop(event_type, None))

    def list(self) -> dict[str, str]:
        return dict(self._webhooks)


# ============================================================================
# RUNNING AGENT  (P1-D, P1-E)
# ============================================================================

@dataclass
class RunningAgent:
    id:             str
    name:           str
    agent_type:     str
    llm:            LLM | None
    system_message: str | None
    workspace_base: Path        # P1-D: base dir; per-conversation sub-dirs created on demand
    sdk_agent:      Agent | None   = None
    state:          AgentState     = AgentState.CREATED
    created_at:     datetime       = field(default_factory=_utcnow)
    # P1-E, P5-E: ConversationRole enum used for role values
    _histories:     dict[str, list[dict]] = field(default_factory=dict)

    def is_ready(self) -> bool:
        return self.sdk_agent is not None and self.llm is not None

    def get_workspace(self, conversation_id: str) -> LocalWorkspace:
        """P1-D: workspace is per-agent AND per-conversation."""
        ws_dir = self.workspace_base / conversation_id
        ws_dir.mkdir(parents=True, exist_ok=True)
        return LocalWorkspace(working_dir=str(ws_dir))

    def get_history(self, conversation_id: str) -> list[dict]:
        return list(self._histories.get(conversation_id, []))

    def append_history(
        self, conversation_id: str, role: ConversationRole, content: str
    ) -> None:
        """P5-E: uses ConversationRole enum."""
        self._histories.setdefault(conversation_id, []).append(
            {"role": role.value, "content": content}
        )

    def build_context_message(self, conversation_id: str, new_message: str) -> str:
        """
        P1-E: rebuild full conversation history as a context block so the SDK
        agent has multi-turn memory on each call.
        """
        history = self.get_history(conversation_id)
        if not history:
            return new_message
        parts = []
        for turn in history:
            r = turn["role"].upper()
            parts.append(f"[{r}]: {turn['content']}")
        history_block = "\n".join(parts)
        return (
            f"<conversation_history>\n{history_block}\n</conversation_history>\n\n"
            f"[USER]: {new_message}"
        )


def _build_sdk_agent(
    llm:            LLM,
    workspace:      LocalWorkspace,
    system_message: str,
    skills:         list[Skill] | None    = None,
    hooks:          HookConfig | None     = None,
    mcp_configs:    list[MCPServerConfig] | None = None,
) -> Agent:
    """
    P1-B: guard HookConfig() construction.
    P1-F: pass mcp_servers to Agent if the kwarg is accepted.
    """
    # P1-B: safe hook construction
    safe_hooks: HookConfig | None = hooks
    if safe_hooks is None:
        try:
            safe_hooks = HookConfig()
        except TypeError:
            try:
                safe_hooks = HookConfig(pre_tool_use=[], post_tool_use=[])
            except Exception:
                safe_hooks = None

    from openhands.tools import get_default_tools
    
    # Filter out browser_tool_set (requires Chromium not installed)
    all_tools = get_default_tools()
    kwargs: dict[str, Any] = {
        "llm":            llm,
        "workspace":      workspace,
        "system_prompt": system_message,  # P0-F: fixed param name
        "tools":         [t for t in all_tools if t.name != "browser_tool_set"],  # Exclude browser
        "skills":         skills or [],
    }
    if safe_hooks is not None:
        kwargs["hooks"] = safe_hooks

    # P1-F: pass MCP server configs if Agent accepts them
    if mcp_configs:
        try:
            import inspect as _inspect
            sig = _inspect.signature(Agent.__init__)
            if "mcp_servers" in sig.parameters:
                kwargs["mcp_servers"] = [
                    {"name": m.name, "command": m.command, "args": m.args, "env": m.env}
                    for m in mcp_configs
                    if m.enabled
                ]
        except Exception:
            pass  # SDK version doesn't support it; skip silently

    return Agent(**kwargs)


# ============================================================================
# SDK AGENT RUN COMPATIBILITY SHIM
# ============================================================================

async def _run_agent_compat(
    sdk_agent: Agent,
    workspace: LocalWorkspace,
    message: str,
) -> list[Any]:
    """
    SDK 1.19.1 uses Agent.step() with LocalConversation.
    """
    from openhands.sdk import LocalConversation
    
    # Create a local conversation
    conv = LocalConversation(
        agent=sdk_agent,
        workspace=workspace,
    )
    
    # Add user message using send_message(message, sender)
    conv.send_message(message, "user")
    
    # Run agent step
    def sync_step():
        events = []
        
        def on_event(evt):
            events.append(evt)
        
        # Call step which processes conversation
        sdk_agent.step(conv, on_event)
        return events
    
    return await asyncio.to_thread(sync_step)


# ============================================================================
# LOCAL RUNTIME
# ============================================================================

class LocalRuntime:
    """Full local runtime – drop-in replacement for OpenHands agent_server runtime."""

    def __init__(
        self,
        working_dir: str = "./workspace",
        db_path:     str = "./local_runtime.db",
    ) -> None:
        self._db          = DatabaseManager(db_path)
        self._working_dir = Path(working_dir).resolve()
        self._working_dir.mkdir(parents=True, exist_ok=True)

        # P0-A: guard against double-startup
        self._started: bool = False

        self._emitter  = RuntimeEventEmitter()
        self._agents:    dict[str, RunningAgent] = {}
        self._providers: dict[str, GitProvider]  = {}

        self._settings = SettingsManager(self._db)
        self._secrets  = SecretsManager(self._db)
        self._mcp      = MCPManager(self._db)
        self._events   = EventStore(self._db)
        # P2-A: WebhookManager no longer subscribes to emitter
        self._webhooks = WebhookManager()
        self._skills   = SkillLoader()
        self._hooks    = HooksManager()

        # SSE broadcast queues keyed by conversation_id
        self._sse_queues: dict[str, list[asyncio.Queue]] = {}

    async def startup(self, skills_dir: str | None = None) -> None:
        """P0-A: idempotent startup; second call is a no-op."""
        if self._started:
            logger.debug("startup() called again — skipping (already started)")
            return
        self._started = True

        await self._settings.load()
        await self._secrets.load()
        await self._mcp.load()
        await self._skills.load_all_skills()
        if skills_dir:
            await self._skills.load_skills_from_directory(skills_dir)
        logger.info("LocalRuntime started (db=%s)", self._db.db_path)

    # ── settings ──────────────────────────────────────────────────────────

    @property
    def settings(self) -> SettingsManager:
        return self._settings

    # ── secrets ───────────────────────────────────────────────────────────

    async def set_secret(self, name: str, value: str) -> None:
        await self._secrets.set(name, value)

    def get_secret(self, name: str) -> str | None:
        return self._secrets.get(name)

    async def delete_secret(self, name: str) -> bool:
        return await self._secrets.delete(name)

    def list_secrets(self) -> list[str]:
        return self._secrets.list_names()

    # ── MCP ───────────────────────────────────────────────────────────────

    async def add_mcp_server(
        self, name: str, command: str,
        args: list[str] | None = None,
        env: dict | None = None,
    ) -> str:
        return await self._mcp.add(name, command, args, env)

    def get_mcp_server(self, sid: str) -> MCPServerConfig | None:
        return self._mcp.get(sid)

    def list_mcp_servers(self) -> list[MCPServerConfig]:
        return self._mcp.list()

    async def remove_mcp_server(self, sid: str) -> bool:
        return await self._mcp.remove(sid)

    # ── skills ────────────────────────────────────────────────────────────

    async def load_skill(
        self, name: str, content: str,
        triggers: list[str] | None = None,
        source: str = "local",
    ) -> Skill:
        return self._skills.load_skill(name, content, triggers, source)

    def match_skills(self, message: str) -> list[Skill]:
        return self._skills.match(message)

    def list_skills(self) -> list[Skill]:
        return self._skills.list()

    # ── hooks ─────────────────────────────────────────────────────────────

    def set_hooks(self, hooks: HookConfig) -> None:
        self._hooks.set_hooks(hooks)

    def get_hooks(self) -> HookConfig | None:
        return self._hooks.get_hooks()

    # ── webhooks ──────────────────────────────────────────────────────────

    def register_webhook(self, event_type: str, url: str) -> None:
        self._webhooks.register(event_type, url)

    def unregister_webhook(self, event_type: str) -> bool:
        return self._webhooks.unregister(event_type)

    def list_webhooks(self) -> dict[str, str]:
        return self._webhooks.list()

    # ── git providers ─────────────────────────────────────────────────────

    async def setup_git_provider(
        self, provider_type: ProviderType, token: str | None = None
    ) -> str:
        pid = str(uuid4())
        self._providers[pid] = GitProvider(
            provider_type=provider_type,
            token=token or os.environ.get("GITHUB_TOKEN"),
        )
        logger.info("Setup git provider: %s", provider_type.value)
        return pid

    async def list_repositories(self, provider_id: str, page: int = 1) -> list[dict]:
        p = self._providers.get(provider_id)
        return await p.list_repos(page) if p else []

    async def get_repository(self, provider_id: str, repo: str) -> dict:
        p = self._providers.get(provider_id)
        return await p.get_repo(repo) if p else {}

    async def list_branches(self, provider_id: str, repo: str) -> list[dict]:
        p = self._providers.get(provider_id)
        return await p.list_branches(repo) if p else []

    # ── workspace  (P1-D) ─────────────────────────────────────────────────

    def get_workspace(self, agent_id: str, conversation_id: str) -> LocalWorkspace:
        """P1-D: isolation is per-agent AND per-conversation."""
        ws_dir = self._working_dir / agent_id / conversation_id
        ws_dir.mkdir(parents=True, exist_ok=True)
        return LocalWorkspace(working_dir=str(ws_dir))

    # ── agent management ──────────────────────────────────────────────────

    async def create_agent(
        self,
        name:           str,
        agent_type:     str      = "code",
        llm:            LLM | None = None,
        system_message: str | None = None,
    ) -> str:
        agent_id     = str(uuid4())
        workspace_base = self._working_dir / agent_id
        workspace_base.mkdir(parents=True, exist_ok=True)
        msg = system_message or (
            PLANNING_AGENT_INSTRUCTION if agent_type == "planning"
            else DEFAULT_SYSTEM_MESSAGE
        )

        sdk_agent: Agent | None = None
        if llm is not None:
            try:
                # P1-F: pass active MCP configs to agent
                sdk_agent = _build_sdk_agent(
                    llm=llm,
                    workspace=LocalWorkspace(working_dir=str(workspace_base)),
                    system_message=msg,
                    skills=self._skills.list(),
                    hooks=self._hooks.get_hooks(),
                    mcp_configs=self._mcp.list(),
                )
            except Exception as exc:
                logger.error("Failed to build SDK agent: %s", exc)

        self._agents[agent_id] = RunningAgent(
            id=agent_id, name=name, agent_type=agent_type,
            llm=llm, system_message=msg,
            workspace_base=workspace_base,
            sdk_agent=sdk_agent,
        )
        logger.info("Created agent %s (name=%s, type=%s, llm=%s)",
                    agent_id, name, agent_type, llm is not None)
        return agent_id

    async def configure_agent_llm(self, agent_id: str, llm: LLM) -> None:
        agent = self._agents.get(agent_id)
        if not agent:
            raise ValueError(f"Agent {agent_id} not found")
        agent.llm = llm
        agent.sdk_agent = _build_sdk_agent(
            llm=llm,
            workspace=LocalWorkspace(working_dir=str(agent.workspace_base)),
            system_message=agent.system_message or DEFAULT_SYSTEM_MESSAGE,
            skills=self._skills.list(),
            hooks=self._hooks.get_hooks(),
            mcp_configs=self._mcp.list(),
        )
        logger.info("Configured LLM for agent %s", agent_id)

    def get_agent_info(self, agent_id: str) -> AgentInfo | None:
        a = self._agents.get(agent_id)
        if not a:
            return None
        return AgentInfo(id=a.id, name=a.name, agent_type=a.agent_type,
                         state=a.state, created_at=a.created_at)

    def list_agents(self) -> list[AgentInfo]:
        return [
            AgentInfo(id=a.id, name=a.name, agent_type=a.agent_type,
                      state=a.state, created_at=a.created_at)
            for a in self._agents.values()
        ]

    async def delete_agent(self, agent_id: str) -> bool:
        return bool(self._agents.pop(agent_id, None))

    # ── conversations ─────────────────────────────────────────────────────

    async def create_conversation(
        self,
        agent_id:            str,
        title:               str | None          = None,
        selected_repository: str | None          = None,
        git_provider:        ProviderType | None = None,
        selected_branch:     str | None          = None,
        initial_message:     str | None          = None,
    ) -> str:
        if agent_id not in self._agents:
            raise ValueError(f"Agent '{agent_id}' not found")

        conv_id = str(uuid4())

        def _insert(sess: Session):
            sess.add(DBConversation(
                id=conv_id,
                agent_id=agent_id,
                title=title,
                agent_type=self._agents[agent_id].agent_type,
                selected_repository=selected_repository,
                git_provider=git_provider.value if git_provider else None,
                selected_branch=selected_branch,
                user_id="local",
                created_at=_utcnow(),
                updated_at=_utcnow(),
            ))
        await self._db.run(_insert)

        # P0-E: schedule initial message with brief delay so SSE subscriber
        # can connect before the first event fires.  History replay handles
        # reconnects safely regardless.
        if initial_message:
            asyncio.ensure_future(
                self._process_initial_message(conv_id, initial_message)
            )

        return conv_id

    async def _process_initial_message(
        self, conversation_id: str, message: str
    ) -> None:
        """Background task to process the initial message after conv creation."""
        # P0-E: small delay so the HTTP response is sent and SSE can subscribe
        await asyncio.sleep(0.1)
        try:
            async for _ in self.send_message(conversation_id, message):
                pass
        except Exception as exc:
            logger.error("Failed to process initial message for %s: %s",
                         conversation_id, exc)

    async def get_conversation(self, conv_id: str) -> dict | None:
        logger.debug("get_conversation called with id=%s", conv_id)
        def _query(sess: Session):
            row = sess.execute(
                select(DBConversation).where(DBConversation.id == conv_id)
            ).scalar_one_or_none()
            logger.debug("DB query result: %s", row)
            # P0-F: Extract values INSIDE session to avoid detached instance error
            if row:
                return {
                    "id":                  row.id,
                    "agent_id":            row.agent_id,
                    "title":               row.title,
                    "agent_type":          row.agent_type,
                    "selected_repository": row.selected_repository,
                    "git_provider":        row.git_provider,
                    "selected_branch":     row.selected_branch,
                    "created_at":          row.created_at.isoformat() if row.created_at else None,
                    "updated_at":          row.updated_at.isoformat() if row.updated_at else None,
                }
            return None

        result = await self._db.run(_query)
        logger.debug("get_conversation returning: %s", result)
        return result

    async def list_conversations(self, limit: int = 20) -> list[dict]:
        def _query(sess: Session):
            rows = sess.execute(
                select(DBConversation)
                .order_by(DBConversation.updated_at.desc())
                .limit(limit)
            ).scalars().all()
            # P0-F: Extract values INSIDE session to avoid detached instance error
            return [
                {
                    "id":         r.id,
                    "agent_id":   r.agent_id,
                    "title":      r.title,
                    "agent_type": r.agent_type,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                }
                for r in rows
            ]

        return await self._db.run(_query)

    async def search_conversations(
        self, q: str | None = None, limit: int = 20
    ) -> list[dict]:
        """P2-D: SQL LIKE filter pushed to the database."""
        def _query(sess: Session):
            stmt = select(DBConversation).order_by(DBConversation.updated_at.desc())
            if q:
                pattern = f"%{q}%"
                stmt = stmt.where(
                    or_(
                        DBConversation.title.ilike(pattern),
                        DBConversation.id.ilike(pattern),
                    )
                )
            stmt = stmt.limit(limit)
            rows = sess.execute(stmt).scalars().all()
            # P0-F: Extract values INSIDE session to avoid detached instance error
            return [
                {
                    "id":         r.id,
                    "agent_id":   r.agent_id,
                    "title":      r.title,
                    "agent_type": r.agent_type,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                }
                for r in rows
            ]

        return await self._db.run(_query)

    async def update_conversation(
        self,
        conv_id: str,
        title:   str | None = None,
        branch:  str | None = None,
    ) -> bool:
        def _update(sess: Session):
            row = sess.execute(
                select(DBConversation).where(DBConversation.id == conv_id)
            ).scalar_one_or_none()
            if not row:
                return False
            if title is not None:
                row.title = title
            if branch is not None:
                row.selected_branch = branch
            row.updated_at = _utcnow()
            return True
        return await self._db.run(_update)

    async def delete_conversation(self, conv_id: str) -> bool:
        """Load ORM object so cascade delete fires correctly."""
        def _delete(sess: Session):
            row = sess.execute(
                select(DBConversation).where(DBConversation.id == conv_id)
            ).scalar_one_or_none()
            if not row:
                return False
            sess.delete(row)
            return True
        return await self._db.run(_delete)

    # ── message / event streaming  (P0-B, P1-A, P1-E, P2-B) ─────────────

    async def send_message(
        self, conversation_id: str, message: str
    ) -> AsyncIterator[Event]:
        conv = await self.get_conversation(conversation_id)
        if not conv:
            raise ValueError(f"Conversation '{conversation_id}' not found")

        agent_id = conv.get("agent_id")
        agent    = self._agents.get(agent_id) if agent_id else None

        if not agent or not agent.is_ready():
            err = ObservationEvent(
                content="[No LLM-configured agent available for this conversation]",
                conversation_id=conversation_id,
            )
            self._fire_sse(conversation_id, err)
            yield err
            return

        # Persist user message
        await self._events.add(
            conversation_id=conversation_id,
            event_type="message",
            content=message,
            action_type="user_message",
            source="user",
        )

        user_evt = ActionEvent(
            action_type="message",
            content=message,
            conversation_id=conversation_id,
        )
        self._fire_sse(conversation_id, user_evt)
        yield user_evt

        agent.state = AgentState.RUNNING
        # P5-E: ConversationRole enum
        agent.append_history(conversation_id, ConversationRole.USER, message)

        try:
            # Get workspace for this conversation
            workspace = self.get_workspace(agent_id, conversation_id)
            
            # P1-A: inject skills
            matched_skills = self._skills.match(message)
            skill_context  = ""
            if matched_skills:
                skill_context = "\n\n".join(
                    f"[Skill: {s.name}]\n{getattr(s, 'content', '')}"
                    for s in matched_skills
                )
                logger.debug("Injecting %d skill(s) as extra_context", len(matched_skills))

            # P1-E: build full conversation history into the context message
            context_message = agent.build_context_message(conversation_id, message)

            # Append skill context
            if skill_context:
                context_message = f"{skill_context}\n\n{context_message}"

            # P0-F: Use SDK 1.19.1 API with workspace and message
            sdk_events = await _run_agent_compat(
                agent.sdk_agent, 
                workspace, 
                context_message
            )

            last_content = ""
            for sdk_evt in sdk_events:
                # P0-B: extract actual content from typed SDK event
                raw_content = str(_extract_sdk_content(sdk_evt))
                raw_thought = str(_extract_sdk_thought(sdk_evt))
                last_content = raw_content

                # P2-B: trigger post-tool hooks if applicable
                action_name = str(getattr(sdk_evt, "action", "")) if getattr(sdk_evt, "action", None) else ""
                if action_name:
                    # Pre-hook (informational; we already ran the action)
                    asyncio.ensure_future(
                        self._hooks.trigger_post_tool_use(
                            action_name,
                            {},
                            raw_content,
                        )
                    )

                await self._events.add(
                    conversation_id=conversation_id,
                    event_type="action" if action_name else "observation",
                    content=raw_content,
                    action_type=action_name or None,
                    source="agent",
                    thought=str(raw_thought) if raw_thought else None,
                )

                if action_name:
                    evt: Event = ActionEvent(
                        action_type=action_name,
                        content=raw_content,
                        thought=raw_thought,
                        conversation_id=conversation_id,
                    )
                else:
                    evt = ObservationEvent(
                        content=raw_content,
                        source="agent",
                        conversation_id=conversation_id,
                    )

                self._fire_sse(conversation_id, evt)
                yield evt

            # P1-E: store assistant turn for next-turn context
            agent.append_history(
                conversation_id,
                ConversationRole.ASSISTANT,
                last_content,
            )

        except Exception as exc:
            logger.exception("Agent error in conversation %s", conversation_id)
            err_evt = ObservationEvent(
                content=f"[Agent error: {exc}]",
                source="runtime",
                conversation_id=conversation_id,
            )
            await self._events.add(
                conversation_id=conversation_id,
                event_type="error",
                content=err_evt.content,
                source="runtime",
            )
            self._fire_sse(conversation_id, err_evt)
            yield err_evt
        finally:
            agent.state = AgentState.AWAITING_INPUT

    async def get_events(
        self, conversation_id: str, since: datetime | None = None
    ) -> list[dict]:
        return await self._events.get(conversation_id, since)

    # ── SSE broadcast helpers ─────────────────────────────────────────────

    def _get_sse_queues(self, conversation_id: str) -> list[asyncio.Queue]:
        return self._sse_queues.setdefault(conversation_id, [])

    def subscribe_sse(self, conversation_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._get_sse_queues(conversation_id).append(q)
        return q

    def unsubscribe_sse(self, conversation_id: str, q: asyncio.Queue) -> None:
        queues = self._sse_queues.get(conversation_id, [])
        if q in queues:
            queues.remove(q)
        if not queues:
            self._sse_queues.pop(conversation_id, None)

    def _event_to_wire(self, event: Event) -> str:
        """Serialize an Event to the JSON wire format using the unified serialiser."""
        d = _event_to_wire_dict(
            event_id        = event.id,
            conversation_id = event.conversation_id,   # P4-E
            event_type      = type(event).__name__,
            timestamp       = event.timestamp.isoformat(),
            content         = getattr(event, "content", ""),
            action_type     = getattr(event, "action_type", ""),
            source          = getattr(event, "source", ""),
            thought         = getattr(event, "thought", ""),
        )
        return json.dumps(d)

    def _fire_sse(self, conversation_id: str, event: Event) -> None:
        """
        P2-A: WebhookManager called directly here (no separate emitter).
        Non-blocking broadcast using put_nowait.
        """
        payload = self._event_to_wire(event)
        dead: list[asyncio.Queue] = []
        for q in list(self._get_sse_queues(conversation_id)):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                logger.warning("SSE queue full for %s – dropping slow subscriber",
                               conversation_id)
                dead.append(q)
        for q in dead:
            self.unsubscribe_sse(conversation_id, q)

        # P2-A: fire webhook directly, fire-and-forget
        asyncio.ensure_future(self._webhooks.on_event(event))

    # ── lifecycle ─────────────────────────────────────────────────────────

    def close(self) -> None:
        self._db.close()
        # P3-G: cancel all live SSE queues
        for conv_id, queues in list(self._sse_queues.items()):
            for q in queues:
                try:
                    q.put_nowait("__closed__")
                except asyncio.QueueFull:
                    pass
        self._sse_queues.clear()
        logger.info("LocalRuntime closed")


# ============================================================================
# FASTAPI APPLICATION
# ============================================================================

def create_app(runtime: LocalRuntime) -> Any:
    """Create the FastAPI application matching the OpenHands frontend protocol."""
    from fastapi import (
        FastAPI, APIRouter, HTTPException, Query,
        WebSocket, WebSocketDisconnect,
        Response, Body,
    )
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import StreamingResponse, JSONResponse, PlainTextResponse

    _ensure_pydantic_models()

    # Unpack module-level Pydantic models (P2-E)
    ConversationCreateRequest  = _pydantic_ns["ConversationCreateRequest"]
    ConversationUpdateRequest  = _pydantic_ns["ConversationUpdateRequest"]
    AgentCreateRequest         = _pydantic_ns["AgentCreateRequest"]
    AgentLLMRequest            = _pydantic_ns["AgentLLMRequest"]
    SendMessageRequest         = _pydantic_ns["SendMessageRequest"]
    SecretCreateRequest        = _pydantic_ns["SecretCreateRequest"]
    MCPConfigRequest           = _pydantic_ns["MCPConfigRequest"]
    SettingSetRequest          = _pydantic_ns["SettingSetRequest"]
    WebhookRequest             = _pydantic_ns["WebhookRequest"]
    APIKeyCreateRequest        = _pydantic_ns["APIKeyCreateRequest"]
    LLMBYORRequest             = _pydantic_ns["LLMBYORRequest"]
    AcceptTOSRequest           = _pydantic_ns["AcceptTOSRequest"]
    CompleteOnboardingRequest  = _pydantic_ns["CompleteOnboardingRequest"]
    SecurityPolicyRequest      = _pydantic_ns["SecurityPolicyRequest"]

    # ── lifespan ──────────────────────────────────────────────────────────
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.runtime = runtime
        # P0-A: startup() is idempotent; safe to call even if already started
        await runtime.startup()
        # P0-F: app-level state (not closure variables)
        app.state.api_keys          = {}
        app.state.tos_accepted      = False
        app.state.onboarding_complete = False
        yield
        runtime.close()   # P3-G: close() now cancels SSE queues

    app = FastAPI(
        title="ClawHands Local Runtime",
        description="OpenHands-compatible local agentic runtime (no Docker, no sandbox)",
        version="1.2.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── helper ────────────────────────────────────────────────────────────

    def _rt() -> LocalRuntime:
        # P5-B: guard AttributeError if called before lifespan completes
        try:
            return app.state.runtime
        except AttributeError:
            raise RuntimeError(
                "Runtime not yet available — lifespan has not completed"
            )

    # ── router: agents ────────────────────────────────────────────────────

    agent_router = APIRouter(prefix="/agents", tags=["Agents"])

    @agent_router.get("")
    async def list_agents() -> dict:
        agents = _rt().list_agents()
        return {
            "items": [
                {
                    "id":         a.id,
                    "name":       a.name,
                    "agent_type": a.agent_type,
                    "state":      a.state.value,
                    "created_at": a.created_at.isoformat(),
                }
                for a in agents
            ],
            "total": len(agents),
        }

    @agent_router.post("", status_code=201)   # P3-F: 201 Created
    async def create_agent(req: AgentCreateRequest) -> dict:
        llm      = make_llm(req.llm_model, req.llm_api_key, req.llm_base_url)
        agent_id = await _rt().create_agent(
            name=req.name,
            agent_type=req.agent_type or "code",
            llm=llm,
            system_message=req.system_message,
        )
        return {"agent_id": agent_id}

    @agent_router.get("/{agent_id}")
    async def get_agent(agent_id: str) -> dict:
        info = _rt().get_agent_info(agent_id)
        if not info:
            raise HTTPException(404, "Agent not found")
        return {
            "id":         info.id,
            "name":       info.name,
            "agent_type": info.agent_type,
            "state":      info.state.value,
            "created_at": info.created_at.isoformat(),
        }

    @agent_router.post("/{agent_id}/llm")
    async def configure_llm(agent_id: str, req: AgentLLMRequest ) -> dict:
        llm = make_llm(req.llm_model, req.llm_api_key, req.llm_base_url)
        if not llm:
            raise HTTPException(400, "llm_model is required")
        await _rt().configure_agent_llm(agent_id, llm)
        return {"success": True}

    @agent_router.delete("/{agent_id}")
    async def delete_agent(agent_id: str) -> dict:
        ok = await _rt().delete_agent(agent_id)
        if not ok:
            raise HTTPException(404, "Agent not found")
        return {"status": "deleted"}

    # ── router: conversations ─────────────────────────────────────────────
    # NOTE: static routes (/search, /start-tasks, /start-tasks/search) MUST
    # be declared BEFORE the parameterised /{conversation_id} route so FastAPI
    # matches them first.  Do not add static sub-paths after the parameterised
    # route without careful ordering review.  (P3-C)

    conv_router = APIRouter(prefix="/app-conversations", tags=["Conversations"])

    @conv_router.get("")
    async def list_conversations(
        limit:   int           = Query(20, le=100),
        page_id: Optional[str] = None,   # P5-C: cursor param accepted but not yet impl
    ) -> dict:
        convs = await _rt().list_conversations(limit=limit)
        return {"items": convs, "total": len(convs)}

    @conv_router.post("", status_code=201)   # P3-F
    async def create_conversation(req: ConversationCreateRequest ) -> dict:
        rt = _rt()

        if req.agent_id:
            agent_id = req.agent_id
            if not rt.get_agent_info(agent_id):
                raise HTTPException(404, f"Agent '{agent_id}' not found")
        else:
            agents = rt.list_agents()
            if not agents:
                llm      = make_llm(req.llm_model, req.llm_api_key, req.llm_base_url)
                agent_id = await rt.create_agent(
                    name="default",
                    agent_type=req.agent_type or "default",
                    llm=llm,
                )
            else:
                preferred = next(
                    (a for a in agents if a.agent_type == (req.agent_type or "default")),
                    agents[0],
                )
                agent_id = preferred.id

        initial_text: str | None = None
        if req.initial_message:
            initial_text = extract_text(req.initial_message)

        conv_id = await rt.create_conversation(
            agent_id=agent_id,
            title=req.title,
            selected_repository=req.selected_repository,
            git_provider=ProviderType(req.git_provider) if req.git_provider else None,
            selected_branch=req.selected_branch,
            initial_message=initial_text,
        )
        return {"id": conv_id, "conversation_id": conv_id}

    # ── STATIC COLLECTION ROUTES (before /{conversation_id}) ─────────────

    @conv_router.get("/search")
    async def search_conversations(
        q:     Optional[str] = Query(None),
        limit: int           = Query(20, le=100),
    ) -> dict:
        # P2-D: SQL LIKE filter
        convs = await _rt().search_conversations(q=q, limit=limit)
        return {"items": convs, "total": len(convs)}

    @conv_router.get("/start-tasks")
    async def list_start_tasks() -> dict:
        skills = _rt().list_skills()
        tasks = [
            {
                "id":          s.name,
                "title":       s.name.replace("_", " ").title(),
                "description": (getattr(s, "content", "") or "")[:120],
                "type":        "skill",
            }
            for s in skills
        ]
        return {"items": tasks, "total": len(tasks)}

    @conv_router.get("/start-tasks/search")
    async def search_start_tasks(q: Optional[str] = Query(None)) -> dict:
        skills = _rt().list_skills()
        tasks = [
            {
                "id":          s.name,
                "title":       s.name.replace("_", " ").title(),
                "description": (getattr(s, "content", "") or "")[:120],
                "type":        "skill",
            }
            for s in skills
        ]
        if q:
            q_lower = q.lower()
            tasks = [t for t in tasks if q_lower in t["title"].lower()
                     or q_lower in t["description"].lower()]
        return {"items": tasks, "total": len(tasks)}

    # ── PARAMETERIZED ROUTES ──────────────────────────────────────────────

    @conv_router.get("/{conversation_id}")
    async def get_conversation(conversation_id: str) -> dict:
        conv = await _rt().get_conversation(conversation_id)
        if not conv:
            raise HTTPException(404, "Conversation not found")
        return conv

    @conv_router.patch("/{conversation_id}")
    async def update_conversation(
        conversation_id: str, req: ConversationUpdateRequest 
    ) -> dict:
        ok = await _rt().update_conversation(
            conv_id=conversation_id,
            title=req.title,
            branch=req.selected_branch,
        )
        if not ok:
            raise HTTPException(404, "Conversation not found")
        return {"id": conversation_id, "status": "updated"}

    @conv_router.delete("/{conversation_id}")
    async def delete_conversation(conversation_id: str) -> dict:
        ok = await _rt().delete_conversation(conversation_id)
        if not ok:
            raise HTTPException(404, "Conversation not found")
        return {"status": "deleted"}

    # ── send message (REST) ───────────────────────────────────────────────

    @conv_router.post("/{conversation_id}/send-message")
    async def send_message(conversation_id: str, req: SendMessageRequest ) -> dict:
        text   = extract_text(req.message)
        events = []
        try:
            async for evt in _rt().send_message(conversation_id, text):
                events.append(_event_to_wire_dict(
                    event_id        = evt.id,
                    conversation_id = evt.conversation_id,
                    event_type      = type(evt).__name__,
                    timestamp       = evt.timestamp.isoformat(),
                    content         = getattr(evt, "content", ""),
                    action_type     = getattr(evt, "action_type", ""),
                    source          = getattr(evt, "source", ""),
                    thought         = getattr(evt, "thought", ""),
                ))
        except ValueError as e:
            # Return friendly error instead of crashing
            events.append(_event_to_wire_dict(
                event_id        = str(uuid4()),
                conversation_id = conversation_id,
                event_type      = "ObservationEvent",
                timestamp       = _utcnow().isoformat(),
                content         = f"[Error] {str(e)}",
                action_type     = "",
                source          = "runtime",
                thought         = "",
            ))
        except Exception as e:
            # Log but don't crash
            logger.exception("Error in send_message")
            events.append(_event_to_wire_dict(
                event_id        = str(uuid4()),
                conversation_id = conversation_id,
                event_type      = "ObservationEvent",
                timestamp       = _utcnow().isoformat(),
                content         = f"[Server Error] {str(e)}",
                action_type     = "",
                source          = "runtime",
                thought         = "",
            ))
        return {"events": events}

    # ── SSE event stream  (P4-A, P4-B, P4-C via WebSocket replay) ─────────

    @conv_router.get("/{conversation_id}/events")
    async def stream_events(
        conversation_id: str,
        since:           Optional[str] = Query(None),
    ) -> StreamingResponse:
        """
        Server-Sent Events: text/event-stream.
        Replays DB history (P4-B: unified format), then streams live events.
        """
        rt = _rt()

        async def generator():
            since_dt = None
            if since:
                try:
                    since_dt = datetime.fromisoformat(since)
                except ValueError:
                    pass

            # P4-B: history uses same unified format as live events
            history = await rt.get_events(conversation_id, since_dt)
            for item in history:
                yield f"data: {json.dumps(item)}\n\n"

            queue = rt.subscribe_sse(conversation_id)
            try:
                while True:
                    # P4-A: structured timeout handling; CancelledError propagates cleanly
                    try:
                        payload = await asyncio.wait_for(
                            asyncio.shield(queue.get()), timeout=25.0
                        )
                        if payload == "__closed__":
                            break
                        yield f"data: {payload}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                    except asyncio.CancelledError:
                        break
            finally:
                rt.unsubscribe_sse(conversation_id, queue)

        return StreamingResponse(
            generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control":     "no-cache",
                "X-Accel-Buffering": "no",
                "Connection":        "keep-alive",
            },
        )

    # ── events search (P3-B: moved to /events/search, no POST /events) ───

    @conv_router.get("/{conversation_id}/events/search")
    async def search_events(
        conversation_id: str,
        since:           Optional[str] = Query(None),
        limit:           int           = Query(100, le=500),
    ) -> dict:
        since_dt = None
        if since:
            try:
                since_dt = datetime.fromisoformat(since)
            except ValueError:
                pass
        items = await _rt().get_events(conversation_id, since_dt, limit=limit)
        return {"items": items, "total": len(items)}

    # ── WebSocket  (P4-C: history replay added, P4-D: broad except) ───────

    @conv_router.websocket("/{conversation_id}/ws")
    async def websocket_endpoint(
        websocket: WebSocket, conversation_id: str
    ) -> None:
        await websocket.accept()
        rt    = _rt()
        queue = rt.subscribe_sse(conversation_id)

        async def forwarder():
            try:
                while True:
                    payload = await queue.get()
                    if payload == "__closed__":
                        break
                    await websocket.send_text(payload)
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.debug("WebSocket forwarder error: %s", exc)

        fwd_task = asyncio.create_task(forwarder())

        try:
            # P4-C: replay conversation history on WebSocket connect
            history = await rt.get_events(conversation_id)
            for item in history:
                await websocket.send_text(json.dumps(item))

            while True:
                raw = await websocket.receive_text()
                try:
                    data = json.loads(raw)
                    text = extract_text(data.get("message", data))
                except (json.JSONDecodeError, AttributeError):
                    text = raw

                if not text.strip():
                    continue

                async for _ in rt.send_message(conversation_id, text):
                    pass

        except WebSocketDisconnect:
            pass
        except Exception as exc:   # P4-D: non-disconnect errors handled
            logger.warning("WebSocket error for %s: %s", conversation_id, exc)
        finally:
            fwd_task.cancel()
            await asyncio.gather(fwd_task, return_exceptions=True)
            rt.unsubscribe_sse(conversation_id, queue)

    # ── skills / hooks (per conversation) ────────────────────────────────

    @conv_router.get("/{conversation_id}/skills")
    async def get_skills(conversation_id: str) -> dict:
        skills = _rt().list_skills()
        return {
            "items": [{"name": s.name, "source": getattr(s, "source", "")} for s in skills],
            "total": len(skills),
        }

    @conv_router.get("/{conversation_id}/hooks")
    async def get_hooks(conversation_id: str) -> dict:
        return {"items": [], "total": 0}

    # ── workspace file access  (P2-G: async I/O) ─────────────────────────

    @conv_router.get("/{conversation_id}/file")
    async def get_conversation_file(
        conversation_id: str,
        path: str = Query(..., description="Relative path inside workspace"),
    ):
        conv = await _rt().get_conversation(conversation_id)
        if not conv:
            raise HTTPException(404, "Conversation not found")
        agent_id = conv.get("agent_id")
        if not agent_id:
            raise HTTPException(400, "Conversation has no associated agent")
        # P1-D: workspace path includes conversation_id
        ws_dir = _rt()._working_dir / agent_id / conversation_id
        target = (ws_dir / path).resolve()
        try:
            target.relative_to(ws_dir.resolve())
        except ValueError:
            raise HTTPException(403, "Path outside workspace")
        if not target.exists():
            raise HTTPException(404, "File not found")
        if target.is_dir():
            raise HTTPException(400, "Path is a directory")
        # P2-G: non-blocking read
        content = await asyncio.to_thread(
            target.read_text, encoding="utf-8", errors="replace"
        )
        return PlainTextResponse(content)

    # ── workspace ZIP download  (P2-F: async I/O) ─────────────────────────

    @conv_router.get("/{conversation_id}/download")
    async def download_conversation_zip(conversation_id: str):
        conv = await _rt().get_conversation(conversation_id)
        if not conv:
            raise HTTPException(404, "Conversation not found")
        agent_id = conv.get("agent_id")
        if not agent_id:
            raise HTTPException(400, "Conversation has no associated agent")
        # P1-D, P2-F: per-conv workspace; ZIP built off event loop
        ws_dir = _rt()._working_dir / agent_id / conversation_id

        def _build_zip() -> bytes:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                if ws_dir.exists():
                    for f in ws_dir.rglob("*"):
                        if f.is_file():
                            zf.write(f, f.relative_to(ws_dir))
            return buf.getvalue()

        zip_bytes = await asyncio.to_thread(_build_zip)

        async def _stream():
            yield zip_bytes

        return StreamingResponse(
            _stream(),
            media_type="application/zip",
            headers={
                "Content-Disposition":
                    f'attachment; filename="conversation-{conversation_id}.zip"'
            },
        )

    # ── router: settings ──────────────────────────────────────────────────

    settings_router = APIRouter(prefix="/settings", tags=["Settings"])

    @settings_router.get("")
    async def get_settings() -> dict:
        return _rt().settings.all

    @settings_router.post("")
    async def set_setting(req: SettingSetRequest ) -> dict:
        await _rt().settings.set(req.key, req.value)
        return {"success": True}

    @settings_router.delete("/{key}")
    async def delete_setting(key: str) -> dict:
        ok = await _rt().settings.delete(key)
        if not ok:
            raise HTTPException(404, "Setting not found")   # P5-D: consistent 404
        return {"success": True}

    # ── router: secrets ───────────────────────────────────────────────────

    secrets_router = APIRouter(prefix="/secrets", tags=["Secrets"])

    @secrets_router.get("")
    async def list_secrets() -> dict:
        return {"secrets": _rt().list_secrets()}

    @secrets_router.post("", status_code=201)   # P3-F
    async def create_secret(req: SecretCreateRequest ) -> dict:
        await _rt().set_secret(req.name, req.value)
        return {"success": True}

    @secrets_router.delete("/{name}")
    async def delete_secret(name: str) -> dict:
        ok = await _rt().delete_secret(name)
        if not ok:
            raise HTTPException(404, "Secret not found")
        return {"success": True}

    @secrets_router.get("/git-providers")
    async def list_git_provider_secrets() -> dict:
        def _guess_provider(name: str) -> str:
            n = name.lower()
            if "github"    in n: return "github"
            if "gitlab"    in n: return "gitlab"
            if "bitbucket" in n: return "bitbucket"
            if "azure"     in n: return "azure"
            return "unknown"

        all_names = _rt().list_secrets()
        git_keywords = ("github", "gitlab", "bitbucket", "azure", "git_token")
        providers = [
            {"name": n, "provider": _guess_provider(n)}
            for n in all_names
            if any(kw in n.lower() for kw in git_keywords)
        ]
        return {"items": providers, "total": len(providers)}

    # ── router: skills  (P3-A: merged into single router) ────────────────

    skills_router = APIRouter(prefix="/skills", tags=["Skills"])

    @skills_router.get("/search")
    async def search_skills_global(q: Optional[str] = Query(None)) -> dict:
        skills = _rt().list_skills()
        if q:
            q_lower = q.lower()
            skills = [
                s for s in skills
                if q_lower in s.name.lower()
                or q_lower in (getattr(s, "content", "") or "").lower()
            ]
        return {
            "items": [{"name": s.name, "source": getattr(s, "source", "")} for s in skills],
            "total": len(skills),
        }

    @skills_router.get("")
    async def list_skills_global() -> dict:
        skills = _rt().list_skills()
        return {
            "items": [{"name": s.name, "source": getattr(s, "source", "")} for s in skills],
            "total": len(skills),
        }

    @skills_router.post("/load")
    async def load_skill(req: dict) -> dict:
        skill = await _rt().load_skill(
            name=req["name"],
            content=req["content"],
            triggers=req.get("triggers"),
            source=req.get("source", "api"),
        )
        return {"name": skill.name}

    # ── router: users ─────────────────────────────────────────────────────

    users_router = APIRouter(prefix="/users", tags=["Users"])

    @users_router.get("/me")
    async def get_current_user() -> dict:
        return {
            "id":       "local",
            "login":    "local",
            "name":     "Local User",
            "email":    None,
            "avatar":   None,
            "provider": "local",
        }

    @users_router.get("/git-info")
    async def get_git_info() -> dict:
        rt = _rt()
        if rt._providers:
            pid, provider = next(iter(rt._providers.items()))
            try:
                user = await provider.get_user()
                return {
                    "provider_id": pid,
                    "provider":    provider.provider_type.value,
                    "login":       user.get("login"),
                    "name":        user.get("name"),
                    "email":       user.get("email"),
                }
            except Exception as exc:
                logger.warning("Could not fetch git user info: %s", exc)
        return {"provider": None, "login": None, "name": None, "email": None}

    # ── router: MCP ───────────────────────────────────────────────────────

    mcp_router = APIRouter(prefix="/mcp", tags=["MCP"])

    @mcp_router.get("")
    async def list_mcp() -> dict:
        servers = _rt().list_mcp_servers()
        return {
            "items": [{"id": s.id, "name": s.name, "command": s.command} for s in servers],
            "total": len(servers),
        }

    @mcp_router.post("", status_code=201)   # P3-F
    async def add_mcp(req: MCPConfigRequest ) -> dict:
        sid = await _rt().add_mcp_server(req.name, req.command, req.args, req.env)
        return {"server_id": sid}

    @mcp_router.delete("/{server_id}")
    async def remove_mcp(server_id: str) -> dict:
        ok = await _rt().remove_mcp_server(server_id)
        if not ok:
            raise HTTPException(404, "Server not found")
        return {"success": True}

    # ── router: repositories ──────────────────────────────────────────────

    repo_router = APIRouter(prefix="/repositories", tags=["Repositories"])

    @repo_router.get("")
    async def list_repos(page: int = Query(1)) -> dict:
        rt   = _rt()
        pids = list(rt._providers.keys())
        if not pids:
            raise HTTPException(400, "No git provider configured")
        repos = await rt.list_repositories(pids[0], page)
        return {"items": repos, "total": len(repos)}

    @repo_router.get("/search")
    async def search_repos(
        query: str = Query(""),
        page:  int = Query(1),
    ) -> dict:
        rt   = _rt()
        pids = list(rt._providers.keys())
        if not pids:
            return {"items": [], "total": 0}
        repos = await rt.list_repositories(pids[0], page)
        if query:
            repos = [r for r in repos if query.lower() in r.get("name", "").lower()]
        return {"items": repos, "total": len(repos)}

    # ── router: webhooks ──────────────────────────────────────────────────

    webhook_router = APIRouter(prefix="/webhooks", tags=["Webhooks"])

    @webhook_router.get("")
    async def list_webhooks() -> dict:
        wh = _rt().list_webhooks()
        return {
            "items": [{"event_type": k, "url": v} for k, v in wh.items()],
            "total": len(wh),
        }

    @webhook_router.post("")
    async def register_webhook(req: WebhookRequest ) -> dict:
        _rt().register_webhook(req.event_type, req.url)
        return {"success": True}

    @webhook_router.delete("/{event_type}")
    async def unregister_webhook(event_type: str) -> dict:
        ok = _rt().unregister_webhook(event_type)
        return {"success": ok}

    # ── health ────────────────────────────────────────────────────────────

    @app.get("/health", tags=["Health"])
    async def health() -> dict:
        return {
            "status":    "ok",
            "timestamp": _utcnow().isoformat(),
            "agents":    len(_rt().list_agents()),
            "skills":    len(_rt().list_skills()),
        }

    # =========================================================================
    # /api/* ROUTER  (flat, non-versioned OpenHands protocol endpoints)
    # =========================================================================

    api_router = APIRouter(prefix="/api", tags=["API"])

    # ── /api/keys ─────────────────────────────────────────────────────────
    # P0-F: all state stored in app.state, not closure variables

    @api_router.get("/keys")
    async def list_api_keys() -> dict:
        keys = app.state.api_keys
        return {
            "items": [
                {
                    "id":         kid,
                    "name":       v["name"],
                    "created_at": v["created_at"],
                    "token_hint": v["token"][:8] + "…" if v.get("token") else None,
                }
                for kid, v in keys.items()
            ],
            "total": len(keys),
        }

    @api_router.post("/keys", status_code=201)   # P3-F
    async def create_api_key(req: APIKeyCreateRequest ) -> dict:
        import secrets as _secrets
        kid   = str(uuid4())
        token = _secrets.token_urlsafe(32)
        app.state.api_keys[kid] = {
            "name":       req.name,
            "token":      token,
            "created_at": _utcnow().isoformat(),
        }
        return {"id": kid, "token": token, "name": req.name}

    @api_router.delete("/keys/{key_id}")
    async def delete_api_key(key_id: str) -> dict:
        if key_id not in app.state.api_keys:
            raise HTTPException(404, "Key not found")
        del app.state.api_keys[key_id]
        return {"success": True}

    # ── /api/llm/configure  (P3-D: moved from /api/keys/llm/byor) ─────────

    @api_router.post("/llm/configure")
    async def configure_byor_llm(req: LLMBYORRequest ) -> dict:
        """
        Configure a 'Bring Your Own [API key]' LLM.
        Stores credentials as a runtime setting and configures any agents
        that have not yet been given an LLM.
        """
        rt  = _rt()
        llm = make_llm(req.model, req.api_key, req.base_url)
        if not llm:
            raise HTTPException(400, "model is required")

        await rt.settings.set("byor_llm_model",   req.model)
        await rt.settings.set("byor_llm_base_url", req.base_url or "")

        configured = 0
        for agent_info in rt.list_agents():
            agent = rt._agents.get(agent_info.id)
            if agent and not agent.is_ready():
                try:
                    await rt.configure_agent_llm(agent_info.id, llm)
                    configured += 1
                except Exception as exc:
                    logger.warning("BYOR: could not configure agent %s: %s",
                                   agent_info.id, exc)

        return {"success": True, "model": req.model, "configured": configured}

    # ── /api/authenticate ─────────────────────────────────────────────────

    @api_router.get("/authenticate")
    async def authenticate() -> dict:
        return {
            "authenticated": True,
            "user": {"id": "local", "login": "local", "name": "Local User"},
        }

    # ── /api/accept_tos ───────────────────────────────────────────────────

    @api_router.post("/accept_tos")
    async def accept_tos(req: AcceptTOSRequest ) -> dict:
        # P0-F: app.state, not closure variable
        app.state.tos_accepted = req.accepted
        await _rt().settings.set("tos_accepted", req.accepted)
        return {"success": True, "accepted": app.state.tos_accepted}

    # ── /api/complete_onboarding ──────────────────────────────────────────

    @api_router.post("/complete_onboarding")
    async def complete_onboarding(req: CompleteOnboardingRequest ) -> dict:
        app.state.onboarding_complete = True
        await _rt().settings.set("onboarding_complete", True)
        return {"success": True}

    # ── /api/email ────────────────────────────────────────────────────────

    @api_router.get("/email")
    async def get_email() -> dict:
        email = _rt().settings.get("user_email")
        return {"email": email}

    # ── /api/options/models ───────────────────────────────────────────────

    @api_router.get("/options/models")
    async def list_models() -> dict:
        rt = _rt()
        byor_model    = rt.settings.get("byor_llm_model")
        byor_base_url = rt.settings.get("byor_llm_base_url")
        env_model     = os.environ.get("OPENHANDS_LLM_MODEL")

        models = []
        seen: set[str] = set()

        def _add(model_id: str, provider: str, base_url: str | None = None):
            if model_id and model_id not in seen:
                seen.add(model_id)
                models.append({"id": model_id, "provider": provider, "base_url": base_url})

        for mid in [
            "claude-opus-4-20250514",
            "claude-sonnet-4-20250514",
            "claude-haiku-4-5-20251001",
            "gpt-4o",
            "gpt-4o-mini",
            "gpt-4-turbo",
            "gpt-3.5-turbo",
            "o1-preview",
            "o1-mini",
        ]:
            provider = "anthropic" if mid.startswith("claude") else "openai"
            _add(mid, provider)

        if env_model:
            _add(env_model, "env")
        if byor_model:
            _add(byor_model, "byor", byor_base_url or None)

        return {"items": models, "total": len(models)}

    # ── /api/security/settings ────────────────────────────────────────────

    @api_router.get("/security/settings")
    async def get_security_settings() -> dict:
        rt = _rt()
        return {
            "sandbox_runtime_container_image": None,
            "sandbox_timeout":                 rt.settings.get("sandbox_timeout", 120),
            "security_analyzer":               rt.settings.get("security_analyzer", "none"),
            "invariant_endpoint":              rt.settings.get("invariant_endpoint"),
            "confirmation_mode":               rt.settings.get("confirmation_mode", False),
            "runtime_extra_deps":              rt.settings.get("runtime_extra_deps"),
        }

    # ── /api/security/policy ─────────────────────────────────────────────

    @api_router.get("/security/policy")
    async def get_security_policy() -> dict:
        return {
            "policy":  _rt().settings.get("security_policy") or {},
            "enabled": bool(_rt().settings.get("security_policy")),
        }

    # ── /api/security/export-trace  (P3-E: single DB query) ──────────────

    @api_router.get("/security/export-trace")
    async def export_security_trace(
        conversation_id: Optional[str] = Query(None),
        limit:           int           = Query(500, le=2000),
    ):
        """
        P3-E: single JOIN query instead of O(N*M) per-conversation loop.
        """
        if conversation_id:
            items = await _rt().get_events(conversation_id)
            items = items[:limit]
        else:
            # Single query across all conversations
            items = await _rt()._events.get_all_recent(limit=limit)

        return JSONResponse(
            content={"trace": items, "total": len(items)},
            headers={"Content-Disposition": "attachment; filename=trace.json"},
        )

    # ── register all routers ──────────────────────────────────────────────

    v1_router = APIRouter(prefix="/api/v1")
    v1_router.include_router(agent_router)
    v1_router.include_router(conv_router)
    v1_router.include_router(settings_router)
    v1_router.include_router(secrets_router)
    # P3-A: single merged skills router (search route declared before list)
    v1_router.include_router(skills_router)
    v1_router.include_router(users_router)
    v1_router.include_router(mcp_router)
    v1_router.include_router(repo_router)
    v1_router.include_router(webhook_router)

    app.include_router(v1_router)
    app.include_router(api_router)

    return app


# ============================================================================
# ENTRY POINT  (P0-A: startup() called once; lifespan call is idempotent)
# ============================================================================

async def start_server(
    host:        str      = "0.0.0.0",
    port:        int      = 8000,
    working_dir: str      = "./workspace",
    db_path:     str      = "./local_runtime.db",
    skills_dir:  str | None = None,
    log_level:   str      = "info",
) -> None:
    """Start the ClawHands local runtime server."""
    import uvicorn

    runtime = LocalRuntime(working_dir=working_dir, db_path=db_path)
    # Startup here so skills_dir is passed; lifespan call will be a no-op
    await runtime.startup(skills_dir=skills_dir)

    app = create_app(runtime)

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level=log_level,
        loop="asyncio",
    )
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="ClawHands Local Runtime")
    parser.add_argument("--host",        default="0.0.0.0")
    parser.add_argument("--port",        type=int, default=8000)
    parser.add_argument("--working-dir", default="./workspace")
    parser.add_argument("--db-path",     default="./local_runtime.db")
    parser.add_argument("--skills-dir",  default=None)
    parser.add_argument("--log-level",   default="info")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

    asyncio.run(start_server(
        host=args.host,
        port=args.port,
        working_dir=args.working_dir,
        db_path=args.db_path,
        skills_dir=args.skills_dir,
        log_level=args.log_level,
    ))
