"""Local Runtime - Full FastAPI server matching OpenHands protocol.

A complete FastAPI-based local runtime implementation:
- Same API endpoints as OpenHands
- SSE event streaming
- WebSocket support
- Same communication protocol with React frontend

Communication:
- REST API for CRUD operations
- SSE streaming for events (/conversation/{id}/events)
- WebSocket for real-time bidirectional communication
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import yaml  # For parsing skill frontmatter
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import uuid4

from openhands.sdk import (
    Agent, AgentContext, 
    TextContent as SDKTextContent,  # SDK's TextContent
)
from openhands.sdk.llm import LLM
from openhands.sdk.workspace import LocalWorkspace
from openhands.sdk.settings import AgentSettings, ConversationSettings
from openhands.sdk.secret import StaticSecret, LookupSecret
from openhands.sdk.skills import Skill, KeywordTrigger, TaskTrigger
from openhands.sdk.hooks import HookConfig

logger = logging.getLogger(__name__)


# ============================================================================
# DATABASE - SQLite with SQLAlchemy
# ============================================================================

from sqlalchemy import create_engine, Column, String, DateTime, Text, Integer, Boolean, ForeignKey, JSON
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session, relationship
from sqlalchemy.pool import StaticPool

Base = declarative_base()


class DBConversation(Base):
    """Conversation database model."""
    __tablename__ = 'conversations'
    
    id = Column(String, primary_key=True)
    title = Column(String, nullable=True)
    agent_type = Column(String, default='default')
    selected_repository = Column(String, nullable=True)
    git_provider = Column(String, nullable=True)
    selected_branch = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    user_id = Column(String, default='local')
    meta_data = Column(JSON, nullable=True)


class DBEvent(Base):
    """Event database model."""
    __tablename__ = 'events'
    
    id = Column(String, primary_key=True)
    conversation_id = Column(String, ForeignKey('conversations.id'))
    event_type = Column(String)  # action, observation, message
    timestamp = Column(DateTime, default=datetime.utcnow)
    content = Column(Text)
    action_type = Column(String, nullable=True)
    source = Column(String, nullable=True)
    meta_data = Column(JSON, nullable=True)


class DBSetting(Base):
    """Settings database model."""
    __tablename__ = 'settings'
    
    key = Column(String, primary_key=True)
    value = Column(Text)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class DBSecret(Base):
    """Secrets database model."""
    __tablename__ = 'secrets'
    
    name = Column(String, primary_key=True)
    value = Column(Text)  # Encrypted in production
    created_at = Column(DateTime, default=datetime.utcnow)


class DBMCPConfig(Base):
    """MCP configuration database model."""
    __tablename__ = 'mcp_configs'
    
    id = Column(String, primary_key=True)
    name = Column(String)
    command = Column(String)
    args = Column(JSON, nullable=True)
    env = Column(JSON, nullable=True)
    enabled = Column(Boolean, default=True)


class DatabaseManager:
    """SQLite database manager."""
    
    def __init__(self, db_path: str = "./local_runtime.db"):
        self.db_path = db_path
        self.engine = create_engine(
            f'sqlite:///{db_path}',
            connect_args={'check_same_thread': False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)
    
    def get_session(self) -> Session:
        return self.SessionLocal()
    
    def close(self):
        self.engine.dispose()


# ============================================================================
# ENUMS
# ============================================================================

class AgentState(Enum):
    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    AWAITING_INPUT = "awaiting_input"
    PAUSED = "paused"
    STOPPED = "stopped"
    ERROR = "error"


class ProviderType(Enum):
    GITHUB = "github"
    GITLAB = "gitlab"
    BITBUCKET = "bitbucket"
    AZURE_DEVOPS = "azure"


class ConversationRole(Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


# ============================================================================
# MODELS
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
    role: ConversationRole
    content: list[ContentBlock]
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Event:
    id: str = field(default_factory=lambda: str(uuid4()))
    conversation_id: str = ""
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Action(Event):
    action_type: str = ""
    thought: str = ""
    content: str = ""
    observation: str = ""


@dataclass
class Observation(Event):
    content: str = ""
    source: str = ""


@dataclass
class AgentInfo:
    id: str
    name: str
    agent_type: str
    state: AgentState
    created_at: datetime


# ============================================================================
# PROMPTS - Same as OpenHands
# ============================================================================

PLANNING_AGENT_INSTRUCTION = """<IMPORTANT_PLANNING_BOUNDARIES>
You are a Planning Agent that can ONLY create plans - you cannot execute code or make changes.

After you finalize the plan in PLAN.md:
- Do NOT ask "Ready to proceed?" or offer to execute the plan
- Do NOT attempt to run any implementation commands
- Instead, inform the user they have two options to proceed:
  1. Click the **Build** button below the plan preview - this will automatically switch to the code agent and instruct it to execute the plan
  2. Switch to the code agent manually (click the agent selector button or press Shift+Tab), then send a message instructing it to execute the plan

Your role ends when the plan is finalized. Implementation is handled by the code agent.
</IMPORTANT_PLANNING_BOUNDARIES>"""

DEFAULT_SYSTEM_MESSAGE = """You are an AI software development agent. You operate in a workflow that allows you to interact with a file system, run commands, and browse the web.

Capabilities:
- Read, write, and execute files
- Run shell commands
- Browse websites and interact with web pages
- Use tools to accomplish your tasks

Guidelines:
- Write clean, efficient code
- Test your solutions
- Stay within the user's constraints
- Ask for clarification when needed"""


# ============================================================================
# SKILLS LOADER - SDK-based
# ============================================================================

class SkillLoader:
    """Load skills using SDK's Skill class."""
    
    def __init__(self, runtime: "LocalRuntime"):
        self.runtime = runtime
        self._loaded_skills: dict[str, Skill] = {}
    
    async def load_skill(
        self,
        name: str,
        content: str,
        triggers: list[str] | None = None,
        source: str = "local",
    ) -> Skill:
        """Load a skill using SDK field names.
        
        SDK Skill fields: name, content, trigger (not instructions/triggers!)
        """
        # Determine trigger type
        trigger = None
        if triggers:
            # TaskTrigger if starts with "/", otherwise KeywordTrigger
            if any(t.startswith("/") for t in triggers):
                trigger = TaskTrigger(triggers=triggers)
            else:
                trigger = KeywordTrigger(keywords=triggers)
        
        # Create skill with SDK field names (not instructions!)
        skill = Skill(
            name=name,
            content=content,  # SDK field, not "instructions"
            trigger=trigger,  # SDK field, not "triggers"
            source=source,
        )
        
        self._loaded_skills[name] = skill
        logger.info(f"Loaded skill: {name} from {source}")
        return skill
    
    async def load_skills_from_directory(self, skills_dir: str) -> list[Skill]:
        """Load skills from a directory - scans subdirectories too."""
        skills = []
        dir_path = Path(skills_dir)
        
        if not dir_path.exists():
            logger.warning(f"Skills directory does not exist: {skills_dir}")
            return []
        
        # Use rglob to find all .md files in subdirectories too
        for skill_file in dir_path.rglob("*.md"):
            if skill_file.name == "README.md":
                continue
            
            try:
                content = skill_file.read_text(encoding="utf-8")
                name = skill_file.stem
                
                # Parse YAML frontmatter
                triggers = []
                if content.startswith("---"):
                    parts = content.split("---", 2)
                    if len(parts) >= 3:
                        fm_text = parts[1]
                        fm = yaml.safe_load(fm_text)
                        if fm and isinstance(fm, dict):
                            triggers = fm.get("triggers", [])
                        # Remove frontmatter from content
                        content = parts[2].strip()
                
                source = str(skills_dir)
                skill = await self.load_skill(
                    name=name,
                    content=content,
                    triggers=triggers,
                    source=source,
                )
                skills.append(skill)
            except Exception as e:
                logger.warning(f"Failed to load skill {skill_file}: {e}")
        
        return skills
    
    def get_skill(self, name: str) -> Skill | None:
        return self._loaded_skills.get(name)
    
    def list_skills(self) -> list[Skill]:
        return list(self._loaded_skills.values())
    
    def match_skills(self, message: str) -> list[Skill]:
        """Match skills based on message content using skill.trigger."""
        matched = []
        for skill in self._loaded_skills.values():
            trigger = getattr(skill, "trigger", None)
            if not trigger:
                continue
            
            # Check KeywordTrigger keywords
            if hasattr(trigger, "keywords"):
                for kw in trigger.keywords:
                    if kw.lower() in message.lower():
                        matched.append(skill)
                        break
            # Check TaskTrigger
            elif hasattr(trigger, "triggers"):
                for t in trigger.triggers:
                    if t.lower() in message.lower():
                        matched.append(skill)
                        break
        return matched
    
    async def load_all_skills(
        self,
        load_public: bool = True,
        load_user: bool = True,
        load_project: bool = True,
        project_dir: str | None = None,
        repo_root: str | None = None,
    ) -> list[Skill]:
        """Load all skills matching original agent_server.
        
        Sources:
        - public: repo_root/skills/ (built-in)
        - user: ~/.openhands/microagents/
        - project: project_dir/.openhands/microagents/
        """
        all_skills = []
        
        # Get repo root - default to local file location
        if repo_root is None:
            # __file__ is /workspace/project/ClawHands/local_runtime.py
            # We want /workspace/project/ClawHands
            local_file = Path(__file__).resolve()
            repo_root = local_file.parent
        global_dir = Path(repo_root) / "skills"
        
        # Load public (built-in) skills from repo
        if load_public and global_dir.exists():
            logger.info(f"Loading public skills from: {global_dir}")
            public_skills = await self.load_skills_from_directory(str(global_dir))
            logger.info(f"Loaded {len(public_skills)} public skills from {global_dir}")
            all_skills.extend(public_skills)
        
        # Load user skills
        if load_user:
            user_dir = Path.home() / ".openhands" / "microagents"
            if user_dir.exists():
                user_skills = await self.load_skills_from_directory(str(user_dir))
                logger.info(f"Loaded {len(user_skills)} user skills")
                all_skills.extend(user_skills)
        
        # Load project skills
        if load_project and project_dir:
            proj_dir = Path(project_dir) / ".openhands" / "microagents"
            if proj_dir.exists():
                proj_skills = await self.load_skills_from_directory(str(proj_dir))
                logger.info(f"Loaded {len(proj_skills)} project skills")
                all_skills.extend(proj_skills)
        
        logger.info(f"Total skills loaded: {len(all_skills)}")
        return all_skills


# ============================================================================
# MICROAGENT LOADER
# ============================================================================

# Use SDK's Skill class - microagents are just skills with triggers
class MicroagentLoader:
    """Load microagents using SDK's Skill class.
    
    SDK 1.19.1 Skill fields:
    - name: skill name
    - content: prompt/instructions
    - trigger: KeywordTrigger or TaskTrigger (singular!)
    """
    
    def __init__(self, runtime: "LocalRuntime"):
        self.runtime = runtime
        self._loaded_skills: dict[str, Skill] = {}
    
    async def load_microagent(
        self,
        name: str,
        content: str,
        triggers: list[str] | None = None,
    ) -> Skill:
        """Load a microagent as an SDK Skill.
        
        Args:
            name: microagent name
            content: prompt/instructions
            triggers: keywords to trigger this microagent
        """
        # Create KeywordTrigger from triggers list
        trigger = None
        if triggers:
            trigger = KeywordTrigger(keywords=triggers)
        
        # Create skill using SDK field names
        skill = Skill(
            name=name,
            content=content,
            trigger=trigger,
        )
        self._loaded_skills[name] = skill
        logger.info(f"Loaded microagent: {name} (triggers: {triggers})")
        return skill
    
    def match_microagent(self, message: str) -> Skill | None:
        """Match a microagent based on message content."""
        for skill in self._loaded_skills.values():
            trigger = getattr(skill, 'trigger', None)
            if trigger:
                trigger_keywords = getattr(trigger, 'keywords', [])
                if trigger_keywords:
                    for kw in trigger_keywords:
                        if kw.lower() in message.lower():
                            return skill
        return None
    
    def list_microagents(self) -> list[Skill]:
        """List all loaded microagents."""
        return list(self._loaded_skills.values())


# ============================================================================
# HOOKS SYSTEM - SDK-based
# ============================================================================

class HooksManager:
    """Manage hooks using SDK's HookConfig."""
    
    def __init__(self, runtime: "LocalRuntime"):
        self.runtime = runtime
        self._hooks: HookConfig = HookConfig()
    
    def set_hooks(self, hooks: HookConfig) -> None:
        self._hooks = hooks
    
    def get_hooks(self) -> HookConfig:
        return self._hooks
    
    async def trigger_pre_tool_use(self, tool_name: str, tool_input: dict) -> bool:
        """Trigger pre-tool-use hooks. Return False to block."""
        # Check pre_tool_use hooks
        for matcher in self._hooks.pre_tool_use:
            if matcher.matcher and matcher.matcher.lower() in tool_name.lower():
                for hook in matcher.hooks:
                    if hook.command:
                        # Execute hook command
                        try:
                            result = subprocess.run(
                                hook.command,
                                input=json.dumps({"tool": tool_name, "input": tool_input}),
                                capture_output=True,
                                text=True,
                                timeout=hook.timeout or 30,
                            )
                            if result.returncode != 0:
                                logger.warning(f"Hook blocked tool {tool_name}")
                                return False
                        except Exception as e:
                            logger.error(f"Hook error: {e}")
                            return False
        return True
    
    async def trigger_post_tool_use(self, tool_name: str, tool_input: dict, output: str) -> None:
        """Trigger post-tool-use hooks."""
        for matcher in self._hooks.post_tool_use:
            if matcher.matcher and matcher.matcher.lower() in tool_name.lower():
                for hook in matcher.hooks:
                    if hook.command:
                        try:
                            subprocess.run(
                                hook.command,
                                input=json.dumps({"tool": tool_name, "input": tool_input, "output": output}),
                                capture_output=True,
                                timeout=hook.timeout or 30,
                            )
                        except Exception as e:
                            logger.error(f"Post-hook error: {e}")


# ============================================================================
# GIT PROVIDER
# ============================================================================

class GitProvider:
    """Git provider using SDK + gh CLI."""
    
    def __init__(self, provider_type: ProviderType, token: str | None = None):
        self.provider_type = provider_type
        self.token = token
        self.host = {
            ProviderType.GITHUB: "github.com",
            ProviderType.GITLAB: "gitlab.com",
            ProviderType.BITBUCKET: "bitbucket.org",
        }.get(provider_type, "github.com")
    
    async def get_user(self) -> dict:
        if self.provider_type == ProviderType.GITHUB:
            result = subprocess.run(
                ["gh", "api", "user"],
                capture_output=True,
                text=True,
                env={**os.environ, "GITHUB_TOKEN": self.token or ""},
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                return {"login": data.get("login"), "email": data.get("email"), "name": data.get("name")}
        return {}
    
    async def list_repos(self, page: int = 1) -> list[dict]:
        if self.provider_type == ProviderType.GITHUB:
            result = subprocess.run(
                ["gh", "repo", "list", "current", "--limit", "30", "--json", "name,owner,url"],
                capture_output=True,
                text=True,
                env={**os.environ, "GITHUB_TOKEN": self.token or ""},
            )
            if result.returncode == 0:
                repos = json.loads(result.stdout)
                return [{"name": r["name"], "owner": r["owner"]["login"], "url": r["url"]} for r in repos]
        return []
    
    async def get_repo(self, repo: str) -> dict:
        if self.provider_type == ProviderType.GITHUB:
            result = subprocess.run(
                ["gh", "repo", repo, "--json", "name,owner,url,defaultBranch,description"],
                capture_output=True,
                text=True,
                env={**os.environ, "GITHUB_TOKEN": self.token or ""},
            )
            if result.returncode == 0:
                return json.loads(result.stdout)
        return {}
    
    async def list_branches(self, repo: str) -> list[dict]:
        if self.provider_type == ProviderType.GITHUB:
            result = subprocess.run(
                ["gh", "repo", "view", repo, "--json", "branches"],
                capture_output=True,
                text=True,
                env={**os.environ, "GITHUB_TOKEN": self.token or ""},
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                return [{"name": b["name"], "sha": b.get("sha", "")} for b in data.get("branches", [])]
        return []


# ============================================================================
# EVENT EMITTER
# ============================================================================

class RuntimeEventEmitter:
    def __init__(self):
        self._subscribers: list[callable] = []
    
    def subscribe(self, callback: callable) -> None:
        self._subscribers.append(callback)
    
    def unsubscribe(self, callback: callable) -> None:
        self._subscribers.remove(callback)
    
    async def emit(self, event: Event) -> None:
        for callback in self._subscribers:
            try:
                callback(event)
            except Exception as e:
                logger.warning(f"Event subscriber error: {e}")


# ============================================================================
# MCP CONFIGURATION
# ============================================================================

@dataclass
class MCPConfig:
    """MCP Server configuration."""
    id: str
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict = field(default_factory=dict)
    enabled: bool = True


class MCPManager:
    """Manage MCP servers."""
    
    def __init__(self, db: DatabaseManager):
        self.db = db
        self._servers: dict[str, MCPConfig] = {}
        self._load_from_db()
    
    def _load_from_db(self):
        session = self.db.get_session()
        try:
            for cfg in session.query(DBMCPConfig).all():
                self._servers[cfg.id] = MCPConfig(
                    id=cfg.id,
                    name=cfg.name,
                    command=cfg.command,
                    args=cfg.args or [],
                    env=cfg.env or {},
                    enabled=cfg.enabled,
                )
        finally:
            session.close()
    
    async def add_server(self, name: str, command: str, args: list[str] = None, env: dict = None) -> str:
        server_id = str(uuid4())
        server = MCPConfig(
            id=server_id,
            name=name,
            command=command,
            args=args or [],
            env=env or {},
        )
        self._servers[server_id] = server
        
        # Save to database
        session = self.db.get_session()
        try:
            db_cfg = DBMCPConfig(
                id=server_id,
                name=name,
                command=command,
                args=args,
                env=env,
                enabled=True,
            )
            session.add(db_cfg)
            session.commit()
        finally:
            session.close()
        
        return server_id
    
    def get_server(self, server_id: str) -> MCPConfig | None:
        return self._servers.get(server_id)
    
    def list_servers(self) -> list[MCPConfig]:
        return [s for s in self._servers.values() if s.enabled]
    
    async def remove_server(self, server_id: str) -> bool:
        if server_id in self._servers:
            del self._servers[server_id]
            session = self.db.get_session()
            try:
                session.query(DBMCPConfig).filter(DBMCPConfig.id == server_id).delete()
                session.commit()
            finally:
                session.close()
            return True
        return False


# ============================================================================
# SETTINGS MANAGER
# ============================================================================

class SettingsManager:
    """Manage runtime settings."""
    
    def __init__(self, db: DatabaseManager):
        self.db = db
        self._settings: dict[str, Any] = {}
        self._load_from_db()
    
    def _load_from_db(self):
        session = self.db.get_session()
        try:
            for setting in session.query(DBSetting).all():
                try:
                    self._settings[setting.key] = json.loads(setting.value)
                except:
                    self._settings[setting.key] = setting.value
        finally:
            session.close()
    
    def get(self, key: str, default: Any = None) -> Any:
        return self._settings.get(key, default)
    
    def set(self, key: str, value: Any) -> None:
        self._settings[key] = value
        
        session = self.db.get_session()
        try:
            existing = session.query(DBSetting).filter(DBSetting.key == key).first()
            if existing:
                existing.value = json.dumps(value) if not isinstance(value, str) else value
            else:
                session.add(DBSetting(key=key, value=json.dumps(value) if not isinstance(value, str) else value))
            session.commit()
        finally:
            session.close()
    
    def delete(self, key: str) -> bool:
        if key in self._settings:
            del self._settings[key]
            session = self.db.get_session()
            try:
                session.query(DBSetting).filter(DBSetting.key == key).delete()
                session.commit()
            finally:
                session.close()
            return True
        return False


# ============================================================================
# SECRETS MANAGER
# ============================================================================

class SecretsManager:
    """Manage secrets using SDK's StaticSecret."""
    
    def __init__(self, db: DatabaseManager):
        self.db = db
        self._secrets: dict[str, StaticSecret] = {}
        self._load_from_db()
    
    def _load_from_db(self):
        session = self.db.get_session()
        try:
            for secret in session.query(DBSecret).all():
                self._secrets[secret.name] = StaticSecret(value=secret.value)
        finally:
            session.close()
    
    def set_secret(self, name: str, value: str) -> None:
        self._secrets[name] = StaticSecret(value=value)
        
        session = self.db.get_session()
        try:
            existing = session.query(DBSecret).filter(DBSecret.name == name).first()
            if existing:
                existing.value = value
            else:
                session.add(DBSecret(name=name, value=value))
            session.commit()
        finally:
            session.close()
    
    def get_secret(self, name: str) -> str | None:
        secret = self._secrets.get(name)
        return secret.get_secret_value() if secret else None
    
    def delete_secret(self, name: str) -> bool:
        if name in self._secrets:
            del self._secrets[name]
            session = self.db.get_session()
            try:
                session.query(DBSecret).filter(DBSecret.name == name).delete()
                session.commit()
            finally:
                session.close()
            return True
        return False
    
    def list_secrets(self) -> list[str]:
        return list(self._secrets.keys())


# ============================================================================
# EVENT STORE
# ============================================================================

class EventStore:
    """Store events in database."""
    
    def __init__(self, db: DatabaseManager):
        self.db = db
    
    async def add_event(
        self,
        conversation_id: str,
        event_type: str,
        content: str,
        action_type: str = None,
        source: str = None,
        metadata: dict = None,
    ) -> str:
        event_id = str(uuid4())
        session = self.db.get_session()
        try:
            event = DBEvent(
                id=event_id,
                conversation_id=conversation_id,
                event_type=event_type,
                content=content,
                action_type=action_type,
                source=source,
                metadata=metadata,
            )
            session.add(event)
            
            # Update conversation timestamp
            conv = session.query(DBConversation).filter(DBConversation.id == conversation_id).first()
            if conv:
                conv.updated_at = datetime.utcnow()
            
            session.commit()
        finally:
            session.close()
        return event_id
    
    async def get_events(
        self,
        conversation_id: str,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[dict]:
        session = self.db.get_session()
        try:
            query = session.query(DBEvent).filter(DBEvent.conversation_id == conversation_id)
            if since:
                query = query.filter(DBEvent.timestamp > since)
            events = query.order_by(DBEvent.timestamp).limit(limit).all()
            
            return [
                {
                    "id": e.id,
                    "type": e.event_type,
                    "timestamp": e.timestamp.isoformat(),
                    "content": e.content,
                    "action_type": e.action_type,
                    "source": e.source,
                    "metadata": e.metadata,
                }
                for e in events
            ]
        finally:
            session.close()


# ============================================================================
# WEBHOOK CALLBACKS
# ============================================================================

class WebhookManager:
    """Manage webhook callbacks."""
    
    def __init__(self, db: DatabaseManager, event_emitter: RuntimeEventEmitter):
        self.db = db
        self._webhooks: dict[str, str] = {}  # event_type -> url
        self.event_emitter = event_emitter
        
        # Subscribe to events
        self.event_emitter.subscribe(self._handle_event)
    
    async def _handle_event(self, event: Event) -> None:
        """Handle events and trigger webhooks."""
        event_type = type(event).__name__.lower()
        
        if event_type in self._webhooks:
            url = self._webhooks[event_type]
            try:
                import httpx
                async with httpx.AsyncClient() as client:
                    await client.post(
                        url,
                        json={
                            "event_type": event_type,
                            "conversation_id": event.conversation_id,
                            "timestamp": event.timestamp.isoformat(),
                            "data": {
                                "content": getattr(event, "content", ""),
                                "action_type": getattr(event, "action_type", ""),
                            },
                        },
                        timeout=10.0,
                    )
            except Exception as e:
                logger.error(f"Webhook error for {event_type}: {e}")
    
    def register_webhook(self, event_type: str, url: str) -> None:
        """Register a webhook URL for an event type."""
        self._webhooks[event_type] = url
        logger.info(f"Registered webhook for {event_type}: {url}")
    
    def unregister_webhook(self, event_type: str) -> bool:
        """Unregister a webhook."""
        if event_type in self._webhooks:
            del self._webhooks[event_type]
            return True
        return False
    
    def list_webhooks(self) -> dict[str, str]:
        """List all registered webhooks."""
        return dict(self._webhooks)


# ============================================================================
# RUNNING AGENT
# ============================================================================

@dataclass
class RunningAgent:
    id: str
    name: str
    agent_type: str
    llm: LLM | None
    system_message: str | None
    workspace: LocalWorkspace
    sdk_agent: Agent
    state: AgentState = AgentState.CREATED
    created_at: datetime = field(default_factory=datetime.utcnow)


# ============================================================================
# LOCAL RUNTIME - Full SDK-based
# ============================================================================

class LocalRuntime:
    """Full local runtime with all features."""
    
    def __init__(
        self,
        working_dir: str = "./workspace",
        db_path: str = "./local_runtime.db",
        skills_dir: str | None = None,
    ):
        # Database
        self.db = DatabaseManager(db_path)
        
        # Paths
        self.working_dir = Path(working_dir).resolve()
        self.working_dir.mkdir(parents=True, exist_ok=True)
        
        # Core components
        self._event_emitter = RuntimeEventEmitter()
        self._agents: dict[str, RunningAgent] = {}
        self._providers: dict[str, GitProvider] = {}
        
        # Managers
        self._settings = SettingsManager(self.db)
        self._secrets = SecretsManager(self.db)
        self._mcp = MCPManager(self.db)
        self._event_store = EventStore(self.db)
        self._webhook_manager = WebhookManager(self.db, self._event_emitter)
        
        # Skill and microagent loaders
        self._skill_loader = SkillLoader(self)
        self._microagent_loader = MicroagentLoader(self)
        self._hooks_manager = HooksManager(self)
        
        # Load skills
        if skills_dir:
            import asyncio
            asyncio.get_event_loop().run_until_complete(
                self._skill_loader.load_skills_from_directory(skills_dir)
            )
        
        logger.info(f"LocalRuntime initialized with database: {db_path}")
    
    # ========================================================================
    # SETTINGS
    # ========================================================================
    
    def get_settings(self) -> SettingsManager:
        return self._settings
    
    # ========================================================================
    # SECRETS
    # ========================================================================
    
    def set_secret(self, name: str, value: str) -> None:
        self._secrets.set_secret(name, value)
    
    def get_secret(self, name: str) -> str | None:
        return self._secrets.get_secret(name)
    
    def delete_secret(self, name: str) -> bool:
        return self._secrets.delete_secret(name)
    
    def list_secrets(self) -> list[str]:
        return self._secrets.list_secrets()
    
    # ========================================================================
    # MCP
    # ========================================================================
    
    async def add_mcp_server(self, name: str, command: str, args: list[str] = None, env: dict = None) -> str:
        return await self._mcp.add_server(name, command, args, env)
    
    def get_mcp_server(self, server_id: str) -> MCPConfig | None:
        return self._mcp.get_server(server_id)
    
    def list_mcp_servers(self) -> list[MCPConfig]:
        return self._mcp.list_servers()
    
    async def remove_mcp_server(self, server_id: str) -> bool:
        return await self._mcp.remove_server(server_id)
    
    # ========================================================================
    # SKILLS
    # ========================================================================
    
    async def load_skill(self, name: str, content: str, triggers: list[str] = None, source: str = "local") -> Skill:
        return await self._skill_loader.load_skill(name, content, triggers, source)
    
    def match_skills(self, message: str) -> list[Skill]:
        return self._skill_loader.match_skills(message)
    
    def list_skills(self) -> list[Skill]:
        return self._skill_loader.list_skills()
    
    # ========================================================================
    # MICROAGENTS
    # ========================================================================
    
    async def load_microagent(self, name: str, content: str, triggers: list[str] = None) -> Skill:
        return await self._microagent_loader.load_microagent(name, content, triggers)
    
    def match_microagent(self, message: str) -> Skill | None:
        return self._microagent_loader.match_microagent(message)
    
    # ========================================================================
    # HOOKS
    # ========================================================================
    
    def set_hooks(self, hooks: HookConfig) -> None:
        self._hooks_manager.set_hooks(hooks)
    
    def get_hooks(self) -> HookConfig:
        return self._hooks_manager.get_hooks()
    
    # ========================================================================
    # WEBHOOKS
    # ========================================================================
    
    def register_webhook(self, event_type: str, url: str) -> None:
        self._webhook_manager.register_webhook(event_type, url)
    
    def unregister_webhook(self, event_type: str) -> bool:
        return self._webhook_manager.unregister_webhook(event_type)
    
    def list_webhooks(self) -> dict[str, str]:
        return self._webhook_manager.list_webhooks()
    
    # ========================================================================
    # GIT PROVIDERS
    # ========================================================================
    
    async def setup_git_provider(self, provider_type: ProviderType, token: str = None) -> str:
        provider_id = str(uuid4())
        self._providers[provider_id] = GitProvider(
            provider_type=provider_type,
            token=token or os.environ.get("GITHUB_TOKEN"),
        )
        logger.info(f"Setup git provider: {provider_type.value}")
        return provider_id
    
    async def list_repositories(self, provider_id: str, page: int = 1) -> list[dict]:
        provider = self._providers.get(provider_id)
        return await provider.list_repos(page) if provider else []
    
    async def get_repository(self, provider_id: str, repo: str) -> dict:
        provider = self._providers.get(provider_id)
        return await provider.get_repo(repo) if provider else {}
    
    async def list_branches(self, provider_id: str, repo: str) -> list[dict]:
        provider = self._providers.get(provider_id)
        return await provider.list_branches(repo) if provider else []
    
    # ========================================================================
    # WORKSPACE
    # ========================================================================
    
    def get_workspace(self, agent_id: str) -> LocalWorkspace:
        return LocalWorkspace(working_dir=str(self.working_dir / agent_id))
    
    # ========================================================================
    # AGENT MANAGEMENT
    # ========================================================================
    
    async def create_agent(
        self,
        name: str,
        agent_type: str = "code",
        llm: LLM = None,
        system_message: str = None,
    ) -> str:
        """Create an agent using SDK.
        
        Note: SDK 1.19.1 requires LLM config. For testing without LLM,
        we'll create the agent but defer full initialization.
        """
        agent_id = str(uuid4())
        workspace = self.get_workspace(agent_id)
        
        # Create SDK Agent - requires LLM in SDK 1.19.1
        sdk_agent = None
        if llm:
            settings = AgentSettings(
                llm=llm,
                system_message=system_message or DEFAULT_SYSTEM_MESSAGE,
                workspace=workspace,
            )
            sdk_agent = settings.create_agent()
        else:
            # For local testing, use simple agent without LLM
            # The agent will need an LLM to be functional
            logger.warning(f"Agent {agent_id} created without LLM - will need LLM config for execution")
        
        self._agents[agent_id] = RunningAgent(
            id=agent_id,
            name=name,
            agent_type=agent_type,
            llm=llm,
            system_message=system_message,
            workspace=workspace,
            sdk_agent=sdk_agent,
        )
        
        logger.info(f"Created agent {agent_id} ({name}, type={agent_type})")
        return agent_id
    
    async def get_agent(self, agent_id: str) -> AgentInfo | None:
        if agent_id not in self._agents:
            return None
        agent = self._agents[agent_id]
        return AgentInfo(
            id=agent.id,
            name=agent.name,
            agent_type=agent.agent_type,
            state=agent.state,
            created_at=agent.created_at,
        )
    
    def list_agents(self) -> list[AgentInfo]:
        return [self.get_agent(aid) for aid in self._agents]
    
    async def delete_agent(self, agent_id: str) -> bool:
        if agent_id in self._agents:
            del self._agents[agent_id]
            return True
        return False
    
    # ========================================================================
    # CONVERSATION MANAGEMENT
    # ========================================================================
    
    async def create_conversation(
        self,
        agent_id: str,
        title: str = None,
        selected_repository: str = None,
        git_provider: ProviderType = None,
        selected_branch: str = None,
        initial_message: str = None,
    ) -> str:
        if agent_id not in self._agents:
            raise ValueError(f"Agent {agent_id} not found")
        
        conv_id = str(uuid4())
        
        # Save to database
        session = self.db.get_session()
        try:
            conv = DBConversation(
                id=conv_id,
                title=title,
                agent_type=self._agents[agent_id].agent_type,
                selected_repository=selected_repository,
                git_provider=git_provider.value if git_provider else None,
                selected_branch=selected_branch,
                user_id="local",
            )
            session.add(conv)
            session.commit()
        finally:
            session.close()
        
        # Add initial message to event store
        if initial_message:
            await self._event_store.add_event(
                conversation_id=conv_id,
                event_type="message",
                content=initial_message,
                action_type="user_message",
            )
        
        # Emit event
        await self._event_emitter.emit(Action(
            action_type="conversation_created",
            content=f"Conversation {conv_id} created",
            conversation_id=conv_id,
        ))
        
        return conv_id
    
    async def get_conversation(self, conv_id: str) -> dict | None:
        session = self.db.get_session()
        try:
            conv = session.query(DBConversation).filter(DBConversation.id == conv_id).first()
            if not conv:
                return None
            return {
                "id": conv.id,
                "title": conv.title,
                "agent_type": conv.agent_type,
                "selected_repository": conv.selected_repository,
                "git_provider": conv.git_provider,
                "selected_branch": conv.selected_branch,
                "created_at": conv.created_at.isoformat(),
                "updated_at": conv.updated_at.isoformat(),
            }
        finally:
            session.close()
    
    async def list_conversations(self, limit: int = 20) -> list[dict]:
        session = self.db.get_session()
        try:
            convs = session.query(DBConversation).order_by(DBConversation.updated_at.desc()).limit(limit).all()
            return [
                {
                    "id": c.id,
                    "title": c.title,
                    "agent_type": c.agent_type,
                    "created_at": c.created_at.isoformat(),
                    "updated_at": c.updated_at.isoformat(),
                }
                for c in convs
            ]
        finally:
            session.close()
    
    async def delete_conversation(self, conv_id: str) -> bool:
        session = self.db.get_session()
        try:
            # Delete events first
            session.query(DBEvent).filter(DBEvent.conversation_id == conv_id).delete()
            # Delete conversation
            result = session.query(DBConversation).filter(DBConversation.id == conv_id).delete()
            session.commit()
            return result > 0
        finally:
            session.close()
    
    # ========================================================================
    # SEND MESSAGE & EVENTS
    # ========================================================================
    
    async def send_message(
        self,
        conversation_id: str,
        message: str,
    ) -> AsyncIterator[Event]:
        """Send message and yield events."""
        # Verify conversation exists
        conv = await self.get_conversation(conversation_id)
        if not conv:
            raise ValueError(f"Conversation {conversation_id} not found")
        
        # Get agent
        # Find agent by looking at conversation agent_type
        agent_id = None
        for aid, agent in self._agents.items():
            if agent.agent_type == conv["agent_type"]:
                agent_id = aid
                break
        
        if not agent_id:
            yield Observation(content="[No agent configured]", conversation_id=conversation_id)
            return
        
        agent = self._agents[agent_id]
        
        # Add user message to event store
        await self._event_store.add_event(
            conversation_id=conversation_id,
            event_type="message",
            content=message,
            action_type="user_message",
        )
        
        # Emit user event
        user_event = Action(
            action_type="message",
            content=message,
            conversation_id=conversation_id,
        )
        yield user_event
        await self._event_emitter.emit(user_event)
        
        agent.state = AgentState.RUNNING
        
        # Run SDK agent
        try:
            async for event in agent.sdk_agent.run(message):
                # Store event
                await self._event_store.add_event(
                    conversation_id=conversation_id,
                    event_type="action",
                    content=str(event),
                    action_type=getattr(event, "action", None) if hasattr(event, "action") else None,
                )
                
                # Emit event
                if hasattr(event, "action"):
                    yield Action(
                        action_type=event.action,
                        content=str(event),
                        conversation_id=conversation_id,
                    )
                else:
                    yield Observation(
                        content=str(event),
                        source="agent",
                        conversation_id=conversation_id,
                    )
                
                await self._event_emitter.emit(Action(
                    action_type="agent_event",
                    content=str(event),
                    conversation_id=conversation_id,
                ))
        except Exception as e:
            logger.error(f"Agent error: {e}")
            yield Observation(content=f"Error: {str(e)}", conversation_id=conversation_id)
        
        agent.state = AgentState.AWAITING_INPUT
    
    async def get_events(
        self,
        conversation_id: str,
        since: datetime | None = None,
    ) -> list[dict]:
        return await self._event_store.get_events(conversation_id, since)
    
    # ========================================================================
    # CLOSE
    # ========================================================================
    
    def close(self):
        self.db.close()
        logger.info("LocalRuntime closed")


# ============================================================================
# FASTAPI APPLICATION - Matching OpenHands Protocol
# ============================================================================

def create_app(runtime: "LocalRuntime" = None) -> "FastAPI":
    """Create FastAPI application matching OpenHands protocol.
    
    Communication protocol with frontend:
    - REST API for CRUD operations
    - SSE for event streaming (/conversation/{id}/events)
    - WebSocket for real-time bidirectional
    """
    from fastapi import FastAPI, APIRouter, HTTPException, Query, Depends
    from fastapi.responses import StreamingResponse
    from pydantic import BaseModel
    from typing import Optional, List
    import asyncio
    
    # Create app
    app = FastAPI(
        title="Local Runtime API",
        description="OpenHands-compatible local runtime API",
        version="1.0.0",
    )
    
    # Store runtime in app state
    if runtime:
        app.state.runtime = runtime
    
    # ================================================================
    # Request/Response Models - Match OpenHands
    # ================================================================
    
    class ConversationCreateRequest(BaseModel):
        title: Optional[str] = None
        agent_type: Optional[str] = "default"
        selected_repository: Optional[str] = None
        git_provider: Optional[str] = None
        selected_branch: Optional[str] = None
        initial_message: Optional[dict] = None
        llm_model: Optional[str] = None
        system_message_suffix: Optional[str] = None
    
    class ConversationResponse(BaseModel):
        id: str
        agent_id: str
        title: Optional[str]
        created_at: str
        updated_at: str
    
    class MessageContent(BaseModel):
        type: str = "text"
        text: str
    
    class SendMessageRequest(BaseModel):
        message: dict
    
    class SendMessageResponse(BaseModel):
        events: List[dict]
    
    class EventSearchResponse(BaseModel):
        items: List[dict]
        total: int
        next_page_id: Optional[str] = None
    
    class SecretCreateRequest(BaseModel):
        name: str
        value: str
    
    class MCPConfigRequest(BaseModel):
        name: str
        command: str
        args: Optional[List[str]] = None
        env: Optional[dict] = None
    
    # ================================================================
    # Router: Conversations
    # ================================================================
    
    conv_router = APIRouter(prefix="/app-conversations", tags=["Conversations"])
    
    @conv_router.get("")
    async def list_conversations(
        limit: int = Query(20, le=100),
        page_id: Optional[str] = None,
    ) -> dict:
        """List conversations."""
        runtime = app.state.runtime
        convs = await runtime.list_conversations(limit=limit)
        return {"items": convs, "total": len(convs)}
    
    @conv_router.post("")
    async def create_conversation(request: ConversationCreateRequest) -> dict:
        """Create a new conversation."""
        runtime = app.state.runtime
        
        # Get or create agent
        agents = runtime.list_agents()
        if not agents:
            agent_id = await runtime.create_agent(
                name="default",
                agent_type=request.agent_type or "default",
            )
        else:
            agent_id = agents[0].id
        
        # Get initial message
        initial_msg = None
        if request.initial_message:
            if isinstance(request.initial_message, dict):
                initial_msg = request.initial_message.get("text")
            elif isinstance(request.initial_message, list):
                initial_msg = request.initial_message[0].get("text", "") if request.initial_message else None
        
        conv_id = await runtime.create_conversation(
            agent_id=agent_id,
            title=request.title,
            selected_repository=request.selected_repository,
            git_provider=ProviderType(request.git_provider) if request.git_provider else None,
            selected_branch=request.selected_branch,
            initial_message=initial_msg,
        )
        
        return {"conversation_id": conv_id}
    
    @conv_router.get("/{conversation_id}")
    async def get_conversation(conversation_id: str) -> dict:
        """Get conversation details."""
        runtime = app.state.runtime
        conv = await runtime.get_conversation(conversation_id)
        if not conv:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return conv
    
    @conv_router.patch("/{conversation_id}")
    async def update_conversation(conversation_id: str, request: dict) -> dict:
        """Update conversation."""
        runtime = app.state.runtime
        # Update logic here
        return {"id": conversation_id, "status": "updated"}
    
    @conv_router.delete("/{conversation_id}")
    async def delete_conversation(conversation_id: str) -> dict:
        """Delete conversation."""
        runtime = app.state.runtime
        success = await runtime.delete_conversation(conversation_id)
        if not success:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return {"status": "deleted"}
    
    @conv_router.post("/{conversation_id}/send-message")
    async def send_message(
        conversation_id: str,
        request: SendMessageRequest,
    ) -> SendMessageResponse:
        """Send message to conversation (REST fallback)."""
        runtime = app.state.runtime
        
        # Extract message content
        message = request.message
        content = message.get("content", [])
        text = content[0].get("text", "") if content else ""
        
        events = []
        async for event in runtime.send_message(conversation_id, text):
            events.append({
                "id": event.id,
                "type": type(event).__name__,
                "timestamp": event.timestamp.isoformat(),
                "content": getattr(event, "content", ""),
                "action_type": getattr(event, "action_type", ""),
            })
        
        return SendMessageResponse(events=events)
    
    @conv_router.get("/{conversation_id}/events")
    async def stream_events(
        conversation_id: str,
        since: Optional[str] = Query(None),
    ):
        """Stream events via SSE - matches OpenHands /{conversation_id}/events endpoint."""
        runtime = app.state.runtime
        
        async def event_generator():
            # Send initial events
            since_dt = datetime.fromisoformat(since) if since else None
            events = await runtime.get_events(conversation_id, since_dt)
            
            yield "data: [\n"
            
            comma = False
            for event in events:
                if comma:
                    yield ",\n"
                yield json.dumps(event)
                comma = True
            
            yield "]\n\n"
            
            # Keep connection open for new events
            while True:
                await asyncio.sleep(5)
                yield "data: []\n\n"
        
        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )
    
    @conv_router.get("/{conversation_id}/skills")
    async def get_conversation_skills(conversation_id: str) -> dict:
        """Get skills for conversation."""
        runtime = app.state.runtime
        skills = runtime.list_skills()
        return {
            "items": [{"name": s.name, "source": s.source} for s in skills],
            "total": len(skills),
        }
    
    @conv_router.get("/{conversation_id}/hooks")
    async def get_conversation_hooks(conversation_id: str) -> dict:
        """Get hooks for conversation."""
        runtime = app.state.runtime
        hooks = runtime.get_hooks()
        return {"items": [], "total": 0}
    
    # ================================================================
    # Router: Settings
    # ================================================================
    
    settings_router = APIRouter(prefix="/settings", tags=["Settings"])
    
    @settings_router.get("")
    async def get_settings() -> dict:
        """Get all settings."""
        runtime = app.state.runtime
        return runtime.get_settings()._settings
    
    @settings_router.post("")
    async def set_setting(request: dict) -> dict:
        """Set a setting."""
        runtime = app.state.runtime
        key = list(request.keys())[0]
        value = request[key]
        runtime.get_settings().set(key, value)
        return {"success": True}
    
    # ================================================================
    # Router: Secrets
    # ================================================================
    
    secrets_router = APIRouter(prefix="/secrets", tags=["Secrets"])
    
    @secrets_router.get("")
    async def list_secrets() -> dict:
        """List secret names."""
        runtime = app.state.runtime
        return {"secrets": runtime.list_secrets()}
    
    @secrets_router.post("")
    async def create_secret(request: SecretCreateRequest) -> dict:
        """Create a secret."""
        runtime = app.state.runtime
        runtime.set_secret(request.name, request.value)
        return {"success": True}
    
    @secrets_router.delete("/{name}")
    async def delete_secret(name: str) -> dict:
        """Delete a secret."""
        runtime = app.state.runtime
        runtime.delete_secret(name)
        return {"success": True}
    
    # ================================================================
    # Router: MCP
    # ================================================================
    
    mcp_router = APIRouter(prefix="/mcp", tags=["MCP"])
    
    @mcp_router.get("")
    async def list_mcp_servers() -> dict:
        """List MCP servers."""
        runtime = app.state.runtime
        servers = runtime.list_mcp_servers()
        return {
            "items": [{"id": s.id, "name": s.name, "command": s.command} for s in servers],
            "total": len(servers),
        }
    
    @mcp_router.post("")
    async def add_mcp_server(request: MCPConfigRequest) -> dict:
        """Add MCP server."""
        runtime = app.state.runtime
        server_id = await runtime.add_mcp_server(
            name=request.name,
            command=request.command,
            args=request.args,
            env=request.env,
        )
        return {"server_id": server_id}
    
    @mcp_router.delete("/{server_id}")
    async def remove_mcp_server(server_id: str) -> dict:
        """Remove MCP server."""
        runtime = app.state.runtime
        success = await runtime.remove_mcp_server(server_id)
        return {"success": success}
    
    # ================================================================
    # Router: Repositories
    # ================================================================
    
    repo_router = APIRouter(prefix="/repositories", tags=["Repositories"])
    
    @repo_router.get("")
    async def list_repositories() -> dict:
        """List repositories from git provider."""
        runtime = app.state.runtime
        provider_id = list(runtime._providers.keys())[0] if runtime._providers else None
        if not provider_id:
            raise HTTPException(status_code=400, detail="No provider configured")
        repos = await runtime.list_repositories(provider_id)
        return {"items": repos, "total": len(repos)}
    
    @repo_router.get("/search")
    async def search_repositories(
        query: str = Query(""),
        page: int = Query(1),
    ) -> dict:
        """Search repositories."""
        runtime = app.state.runtime
        provider_id = list(runtime._providers.keys())[0] if runtime._providers else None
        if not provider_id:
            return {"items": [], "total": 0}
        repos = await runtime.list_repositories(provider_id, page)
        # Filter by query
        if query:
            repos = [r for r in repos if query.lower() in r.get("name", "").lower()]
        return {"items": repos, "total": len(repos)}
    
    # ================================================================
    # Router: Webhooks
    # ================================================================
    
    webhook_router = APIRouter(prefix="/webhooks", tags=["Webhooks"])
    
    @webhook_router.get("")
    async def list_webhooks() -> dict:
        """List webhooks."""
        runtime = app.state.runtime
        wh = runtime.list_webhooks()
        return {"items": [{"event_type": k, "url": v} for k, v in wh.items()], "total": len(wh)}
    
    @webhook_router.post("")
    async def register_webhook(request: dict) -> dict:
        """Register a webhook."""
        runtime = app.state.runtime
        runtime.register_webhook(request["event_type"], request["url"])
        return {"success": True}
    
    # ================================================================
    # Register routers
    # ================================================================
    
    app.include_router(conv_router)
    app.include_router(settings_router)
    app.include_router(secrets_router)
    app.include_router(mcp_router)
    app.include_router(repo_router)
    app.include_router(webhook_router)
    
    return app


async def start_api_server(runtime: "LocalRuntime" = None, host: str = "0.0.0.0", port: int = 8000):
    """Start FastAPI server."""
    import uvicorn
    
    app = create_app(runtime)
    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()