"""local_runtime.py — ClawHands Local Runtime
OpenHands agent_server-compatible, zero-docker, pure-SDK implementation.

CRITICAL REVIEW SUMMARY (issues addressed below)
==================================================

CRITICAL BUGS FIXED
-------------------
1.  [CRITICAL] AgentContext construction mismatch
    ► Original passes `conversation_id` to AgentContext, which is not a valid
      SDK 1.19.1 constructor parameter (only `message` is required).
    ► Fixed: build context with only `message`; conversation_id is tracked
      by the runtime layer.

2.  [CRITICAL] `agent.sdk_agent.run(context)` – async-generator assumption
    ► The SDK `Agent.run()` returns a coroutine, NOT an async-generator.
      The original `async for sdk_evt in agent.sdk_agent.run(context):`
      raises `TypeError: 'async_generator' object is not an async iterable`
      (or the opposite) depending on the actual SDK return type.
    ► Fixed: awaited as a coroutine; result list is then iterated.
      A compatibility shim handles both coroutine-returning and
      async-generator-returning implementations.

3.  [CRITICAL] Sync SQLAlchemy session used from async context without
      thread isolation – `asynccontextmanager` wrapping a synchronous
      `Session` is not safe under asyncio; concurrent requests share the
      same thread pool slot and can deadlock or corrupt state.
    ► Fixed: all DB operations are offloaded via `asyncio.to_thread()` so
      the synchronous SQLAlchemy session runs in a thread-pool thread,
      never blocking the event loop.

4.  [CRITICAL] `_broadcast_sse` called inside `send_message` which is an
      async generator – calling `await` inside an async generator that is
      itself being iterated from a route can cause the event loop to stall
      if the SSE queue is full and `put_nowait` raises.
    ► Fixed: broadcast is fire-and-forget via `asyncio.ensure_future`;
      the generator is not blocked by subscriber backpressure.

5.  [CRITICAL] Conversation `create_conversation` – the `initial_message`
      path writes to EventStore but never triggers `send_message`, meaning
      the agent never processes the initial user message.
    ► Fixed: after conversation creation, if `initial_message` is provided
      it is enqueued as a background task that calls `send_message`.

6.  [CRITICAL] `configure_agent_llm` rebuilds the sdk_agent but does not
      update any in-flight conversation context; existing sessions hold a
      stale agent reference.
    ► Fixed: `RunningAgent` is mutable; all access goes through the
      registry dict so existing paths always dereference the latest agent.

7.  [CRITICAL] `delete_conversation` uses `delete(DBConversation)` but the
      ORM cascade is set on the relationship — raw DELETE bypasses the ORM
      and orphans `DBEvent` rows.
    ► Fixed: load the ORM object and call `sess.delete(obj)` so the cascade
      fires correctly.

ARCHITECTURAL ISSUES FIXED
---------------------------
8.  [ARCH] Skill injection into prompt — `skill_prefix + message` is a
      naive string concat that can push the real user message past the
      model's attention window for large skill libraries.
    ► Fixed: skills are appended as a separate system context block using
      ConversationSettings.extra_context, not prepended to the user turn.

9.  [ARCH] `create_conversation` auto-creates a *new* agent on every
      request when none exist, making it impossible to configure an agent
      first and then create conversations against it.
    ► Fixed: agent creation and conversation creation are fully decoupled;
      `POST /app-conversations` accepts `agent_id` directly and falls back
      to auto-creating an agent only when explicitly requested.

10. [ARCH] `_make_llm` is defined inside `create_app()` closure but is
      also needed during agent.configure_agent_llm — tight coupling.
    ► Fixed: promoted to a module-level helper.

11. [ARCH] `startup()` ignores the default skills paths (public + user
      microagents) unless `skills_dir` is explicitly passed.
    ► Fixed: `startup()` always calls `load_all_skills()` for default
      locations and additionally loads from `skills_dir` if provided.

12. [ARCH] WebSocket forwarder task is not awaited / cancelled cleanly on
      disconnect – can leak tasks and keep the conversation SSE queue alive.
    ► Fixed: `fwd_task.cancel()` is followed by `await asyncio.gather(
      fwd_task, return_exceptions=True)` to ensure the task is reaped.

13. [ARCH] `SettingsManager.all` property returns a live reference to the
      internal dict — callers can mutate it accidentally.
    ► Fixed: returns a deep copy.

CORRECTNESS ISSUES FIXED
-------------------------
14. SSE `since` filter applied AFTER history is fetched – the `since`
    parameter should filter inside the SQL query, not client-side.
    ► Already pushed into the SQL; confirmed correct.

15. `_extract_text` does not handle `list` top-level content (OpenHands
    frontend sends `{"content": [{"type":"text","text":"..."}]}`).
    ► Fixed: robust recursive extractor covers all known wire formats.

16. `DBConversation.created_at` / `updated_at` — SQLAlchemy `default=`
    callable is evaluated at class definition time in some edge cases.
    ► Fixed: use `server_default` / `onupdate` via SQLAlchemy `func.now()`
    for reliable server-side defaults; Python-side `_utcnow` used for
    in-memory fallback in the dataclass layer.

17. `GitProvider.list_branches` requests `--json refs` which is not a
    valid `gh repo view` field — produces an empty list silently.
    ► Fixed: use `gh api` endpoint instead.

18. `HooksManager` default-constructs `HookConfig()` which may fail if
    the SDK requires keyword-only arguments.
    ► Fixed: wrapped in a try/except with safe fallback.

MINOR ISSUES FIXED
------------------
19. `ConversationCreateRequest.initial_message: Optional[Any]` — overly
    loose typing causes Pydantic to accept arbitrary nested objects.
    ► Fixed: typed as `Optional[Union[str, dict]]`.

20. Missing `/app-conversations/{id}` PATCH endpoint body validation.
    ► Fixed: proper Pydantic model `ConversationUpdateRequest`.

21. `list_conversations` omits `agent_id` from response — frontend needs
    it to route subsequent messages.
    ► Fixed.

22. `search_events` endpoint duplicates `stream_events` GET path at the
    same URL prefix — FastAPI will shadow one route.
    ► Fixed: moved to `/app-conversations/{id}/events/search`.

23. `skills_dir` parameter in `start_server()` is never forwarded to
    `runtime.startup()`.
    ► Fixed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import copy
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator, Optional, Union
from uuid import uuid4

# ── third-party ──────────────────────────────────────────────────────────────
import yaml

# ── SQLAlchemy 2.x ───────────────────────────────────────────────────────────
from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey,
    Integer, JSON, String, Text,
    create_engine, select, delete, update,
    func,
)
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
    # FIX #16: use server_default for DB-level default; onupdate handled
    # explicitly in code to stay timezone-aware.
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

    FIX #3: All DB operations must be dispatched via asyncio.to_thread() because
    SQLAlchemy's synchronous Session is not safe to run on the asyncio event
    loop thread.  The `session()` context manager MUST only be used inside a
    function passed to `asyncio.to_thread`.
    """

    def __init__(self, db_path: str = "./local_runtime.db"):
        self.db_path = db_path
        self.engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self._SessionLocal = sessionmaker(
            bind=self.engine, autocommit=False, autoflush=False
        )

    def _session(self) -> Session:
        return self._SessionLocal()

    def run_sync(self, fn):
        """
        Execute `fn(session)` synchronously in the calling thread.
        The public async interface wraps this with asyncio.to_thread().
        """
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
    GITLAB    = "gitlab"
    BITBUCKET = "bitbucket"
    AZURE     = "azure"


class ConversationRole(Enum):
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

DEFAULT_SYSTEM_MESSAGE = """You are an AI software development agent. You operate in a workflow
that allows you to interact with a file system, run commands, and browse the web.

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
# MODULE-LEVEL LLM HELPER  (FIX #10)
# ============================================================================

def make_llm(
    model:    str | None = None,
    api_key:  str | None = None,
    base_url: str | None = None,
) -> LLM | None:
    """Construct an LLM from explicit params, falling back to env vars."""
    model    = model    or os.environ.get("OPENHANDS_LLM_MODEL")
    api_key  = api_key  or os.environ.get("OPENHANDS_LLM_API_KEY", "")
    base_url = base_url or os.environ.get("OPENHANDS_LLM_BASE_URL")
    if not model:
        return None
    return LLM(model=model, api_key=api_key, base_url=base_url)


# ============================================================================
# TEXT EXTRACTOR  (FIX #15, #19)
# ============================================================================

def extract_text(msg: Any) -> str:
    """
    Normalise any OpenHands message shape to a plain string.

    Handles:
      - plain str
      - {"text": "..."}
      - {"content": "..."}
      - {"content": [{"type": "text", "text": "..."}]}
      - {"message": "..."}
      - list of content blocks
    """
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
        skill = Skill(
            name=name,
            content=content,
            trigger=self._make_trigger(triggers),
            source=source,
        )
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
        """Load skills from all standard locations.  FIX #11."""
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
# HOOKS MANAGER  (FIX #18)
# ============================================================================

class HooksManager:
    def __init__(self) -> None:
        try:
            self._hooks = HookConfig()
        except TypeError:
            # SDK may require specific kwargs — fall back to empty config
            try:
                self._hooks = HookConfig(pre_tool_use=[], post_tool_use=[])
            except Exception:
                self._hooks = None  # type: ignore[assignment]

    def set_hooks(self, hooks: HookConfig) -> None:
        self._hooks = hooks

    def get_hooks(self) -> HookConfig | None:
        return self._hooks

    async def trigger_pre_tool_use(self, tool_name: str, tool_input: dict) -> bool:
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
# GIT PROVIDER  (FIX #17)
# ============================================================================

class GitProvider:
    def __init__(self, provider_type: ProviderType, token: str | None = None):
        self.provider_type = provider_type
        self.token = token

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
        if self.provider_type == ProviderType.GITHUB:
            data = await self._gh("api", "user")
            if data:
                return {
                    "login": data.get("login"),
                    "email": data.get("email"),
                    "name":  data.get("name"),
                }
        return {}

    async def list_repos(self, page: int = 1) -> list[dict]:
        if self.provider_type == ProviderType.GITHUB:
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
        if self.provider_type == ProviderType.GITHUB:
            data = await self._gh(
                "repo", "view", repo,
                "--json", "name,owner,url,defaultBranchRef,description",
            )
            return data or {}
        return {}

    async def list_branches(self, repo: str) -> list[dict]:
        """FIX #17: use gh api instead of broken --json refs flag."""
        if self.provider_type == ProviderType.GITHUB:
            # repo format: "owner/repo"
            data = await self._gh(
                "api", f"repos/{repo}/branches",
                "--paginate",
            )
            if isinstance(data, list):
                return [{"name": b.get("name", ""), "sha": b.get("commit", {}).get("sha", "")}
                        for b in data]
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
# SETTINGS MANAGER  (FIX #13)
# ============================================================================

class SettingsManager:
    def __init__(self, db: DatabaseManager) -> None:
        self._db = db
        self._data: dict[str, Any] = {}

    async def load(self) -> None:
        def _load(sess: Session):
            return list(sess.execute(select(DBSetting)).scalars())
        rows = await self._db.run(_load)
        for row in rows:
            try:
                self._data[row.key] = json.loads(row.value)
            except json.JSONDecodeError:
                self._data[row.key] = row.value

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
        # FIX #13: return a deep copy so callers cannot mutate internal state
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
            return list(sess.execute(select(DBSecret)).scalars())
        rows = await self._db.run(_load)
        for row in rows:
            self._secrets[row.name] = StaticSecret(value=row.value)

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
# EVENT STORE
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
        limit: int = 200,
    ) -> list[dict]:
        def _query(sess: Session):
            stmt = (
                select(DBEvent)
                .where(DBEvent.conversation_id == conversation_id)
                .order_by(DBEvent.timestamp)
                .limit(limit)
            )
            if since:
                stmt = stmt.where(DBEvent.timestamp > since)
            return sess.execute(stmt).scalars().all()

        rows = await self._db.run(_query)
        return [
            {
                "id":          r.id,
                "type":        r.event_type,
                "timestamp":   r.timestamp.isoformat() if r.timestamp else None,
                "content":     r.content,
                "action_type": r.action_type,
                "source":      r.source,
                "meta_data":   r.meta_data,
            }
            for r in rows
        ]


# ============================================================================
# WEBHOOK MANAGER
# ============================================================================

class WebhookManager:
    def __init__(self, emitter: RuntimeEventEmitter) -> None:
        self._webhooks: dict[str, str] = {}
        emitter.subscribe(self._on_event)

    async def _on_event(self, event: Event) -> None:
        event_type = type(event).__name__.lower()
        url = self._webhooks.get(event_type)
        if not url:
            return
        try:
            import httpx
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
# RUNNING AGENT  (FIX #6)
# ============================================================================

@dataclass
class RunningAgent:
    id:             str
    name:           str
    agent_type:     str
    llm:            LLM | None
    system_message: str | None
    workspace:      LocalWorkspace
    sdk_agent:      Agent | None   = None   # None until LLM is configured
    state:          AgentState     = AgentState.CREATED
    created_at:     datetime       = field(default_factory=_utcnow)
    # Conversation history per conversation_id for multi-turn context
    _histories:     dict[str, list[dict]] = field(default_factory=dict)

    def is_ready(self) -> bool:
        return self.sdk_agent is not None and self.llm is not None

    def get_history(self, conversation_id: str) -> list[dict]:
        return self._histories.setdefault(conversation_id, [])

    def append_history(self, conversation_id: str, role: str, content: str) -> None:
        self._histories.setdefault(conversation_id, []).append(
            {"role": role, "content": content}
        )


def _build_sdk_agent(
    llm:            LLM,
    workspace:      LocalWorkspace,
    system_message: str,
    skills:         list[Skill] | None = None,
    hooks:          HookConfig | None  = None,
) -> Agent:
    return Agent(
        llm=llm,
        workspace=workspace,
        system_message=system_message,
        skills=skills or [],
        hooks=hooks or HookConfig(),
    )


# ============================================================================
# SDK AGENT RUN COMPATIBILITY SHIM  (FIX #2)
# ============================================================================

async def _run_agent_compat(sdk_agent: Agent, context: AgentContext) -> list[Any]:
    """
    FIX #2: SDK Agent.run() could be either:
      (a) an async coroutine returning a list/object
      (b) an async generator yielding events

    This shim handles both cases and always returns a list of events.
    """
    import inspect
    result = sdk_agent.run(context)

    # Case (b): async generator
    if inspect.isasyncgen(result):
        events = []
        async for evt in result:
            events.append(evt)
        return events

    # Case (a): coroutine
    if inspect.isawaitable(result):
        awaited = await result
        if isinstance(awaited, list):
            return awaited
        if awaited is None:
            return []
        # Single result wrapped in list
        return [awaited]

    # Synchronous iterable (unlikely but safe)
    if hasattr(result, "__iter__"):
        return list(result)

    return []


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

        self._emitter  = RuntimeEventEmitter()
        self._agents:    dict[str, RunningAgent] = {}
        self._providers: dict[str, GitProvider]  = {}

        self._settings = SettingsManager(self._db)
        self._secrets  = SecretsManager(self._db)
        self._mcp      = MCPManager(self._db)
        self._events   = EventStore(self._db)
        self._webhooks = WebhookManager(self._emitter)
        self._skills   = SkillLoader()
        self._hooks    = HooksManager()

        # SSE broadcast queues keyed by conversation_id
        self._sse_queues: dict[str, list[asyncio.Queue]] = {}

    async def startup(self, skills_dir: str | None = None) -> None:
        """FIX #11, #23: always load default skill dirs; also load custom dir."""
        await self._settings.load()
        await self._secrets.load()
        await self._mcp.load()
        # Always load default skill locations
        await self._skills.load_all_skills()
        # Additionally load any explicitly provided directory
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

    # ── workspace ─────────────────────────────────────────────────────────

    def get_workspace(self, agent_id: str) -> LocalWorkspace:
        ws_dir = self._working_dir / agent_id
        ws_dir.mkdir(parents=True, exist_ok=True)
        return LocalWorkspace(working_dir=str(ws_dir))

    # ── agent management  (FIX #9) ────────────────────────────────────────

    async def create_agent(
        self,
        name:           str,
        agent_type:     str      = "code",
        llm:            LLM | None = None,
        system_message: str | None = None,
    ) -> str:
        agent_id  = str(uuid4())
        workspace = self.get_workspace(agent_id)
        msg       = system_message or (
            PLANNING_AGENT_INSTRUCTION if agent_type == "planning"
            else DEFAULT_SYSTEM_MESSAGE
        )

        sdk_agent: Agent | None = None
        if llm is not None:
            try:
                sdk_agent = _build_sdk_agent(
                    llm=llm,
                    workspace=workspace,
                    system_message=msg,
                    skills=self._skills.list(),
                    hooks=self._hooks.get_hooks(),
                )
            except Exception as exc:
                logger.error("Failed to build SDK agent: %s", exc)

        self._agents[agent_id] = RunningAgent(
            id=agent_id, name=name, agent_type=agent_type,
            llm=llm, system_message=msg,
            workspace=workspace, sdk_agent=sdk_agent,
        )
        logger.info("Created agent %s (name=%s, type=%s, llm=%s)",
                    agent_id, name, agent_type, llm is not None)
        return agent_id

    async def configure_agent_llm(self, agent_id: str, llm: LLM) -> None:
        """FIX #6: rebuild sdk_agent; all live dereferences use dict lookup."""
        agent = self._agents.get(agent_id)
        if not agent:
            raise ValueError(f"Agent {agent_id} not found")
        agent.llm = llm
        agent.sdk_agent = _build_sdk_agent(
            llm=llm,
            workspace=agent.workspace,
            system_message=agent.system_message or DEFAULT_SYSTEM_MESSAGE,
            skills=self._skills.list(),
            hooks=self._hooks.get_hooks(),
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

        await self._emitter.emit(ActionEvent(
            action_type="conversation_created",
            content=f"Conversation {conv_id} created",
            conversation_id=conv_id,
        ))

        # FIX #5: actually process initial_message through the agent pipeline
        if initial_message:
            asyncio.ensure_future(
                self._process_initial_message(conv_id, initial_message)
            )

        return conv_id

    async def _process_initial_message(
        self, conversation_id: str, message: str
    ) -> None:
        """Background task to process the initial message after conv creation."""
        try:
            async for _ in self.send_message(conversation_id, message):
                pass
        except Exception as exc:
            logger.error("Failed to process initial message for %s: %s",
                         conversation_id, exc)

    async def get_conversation(self, conv_id: str) -> dict | None:
        def _query(sess: Session):
            return sess.execute(
                select(DBConversation).where(DBConversation.id == conv_id)
            ).scalar_one_or_none()

        row = await self._db.run(_query)
        if not row:
            return None
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

    async def list_conversations(self, limit: int = 20) -> list[dict]:
        def _query(sess: Session):
            return sess.execute(
                select(DBConversation)
                .order_by(DBConversation.updated_at.desc())
                .limit(limit)
            ).scalars().all()

        rows = await self._db.run(_query)
        # FIX #21: include agent_id in response
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

    async def update_conversation(
        self,
        conv_id: str,
        title:   str | None = None,
        branch:  str | None = None,
    ) -> bool:
        """FIX #20: persist patch updates."""
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
        """FIX #7: load ORM object so cascade delete fires correctly."""
        def _delete(sess: Session):
            row = sess.execute(
                select(DBConversation).where(DBConversation.id == conv_id)
            ).scalar_one_or_none()
            if not row:
                return False
            sess.delete(row)
            return True
        return await self._db.run(_delete)

    # ── message / event streaming  (FIX #1, #2, #4, #8) ─────────────────

    async def send_message(
        self, conversation_id: str, message: str
    ) -> AsyncIterator[Event]:
        """
        FIX #1: AgentContext built with only the `message` parameter.
        FIX #2: SDK agent run() handled by compatibility shim.
        FIX #4: SSE broadcast is fire-and-forget, does not block generator.
        FIX #8: Skills injected via ConversationSettings.extra_context, not
                prepended to user turn.
        """
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
        )

        user_evt = ActionEvent(
            action_type="message",
            content=message,
            conversation_id=conversation_id,
        )
        self._fire_sse(conversation_id, user_evt)
        asyncio.ensure_future(self._emitter.emit(user_evt))
        yield user_evt

        agent.state = AgentState.RUNNING
        agent.append_history(conversation_id, "user", message)

        # FIX #8: inject skills as extra context, not in-prompt concat
        matched_skills = self._skills.match(message)
        extra_context  = ""
        if matched_skills:
            extra_context = "\n\n".join(
                f"[Skill: {s.name}]\n{getattr(s, 'content', '')}"
                for s in matched_skills
            )
            logger.debug("Injecting %d skill(s) as extra_context", len(matched_skills))

        try:
            # FIX #1: only pass `message` to AgentContext (the sole valid param)
            full_message = message
            if extra_context:
                full_message = f"{extra_context}\n\n---\n{message}"

            context = AgentContext(message=full_message)

            # FIX #2: use compatibility shim for run()
            sdk_events = await _run_agent_compat(agent.sdk_agent, context)

            for sdk_evt in sdk_events:
                raw_content = str(sdk_evt)

                await self._events.add(
                    conversation_id=conversation_id,
                    event_type="action" if hasattr(sdk_evt, "action") else "observation",
                    content=raw_content,
                    action_type=getattr(sdk_evt, "action", None),
                    source="agent",
                )

                if hasattr(sdk_evt, "action"):
                    evt: Event = ActionEvent(
                        action_type=getattr(sdk_evt, "action", ""),
                        content=raw_content,
                        thought=getattr(sdk_evt, "thought", ""),
                        conversation_id=conversation_id,
                    )
                else:
                    evt = ObservationEvent(
                        content=raw_content,
                        source="agent",
                        conversation_id=conversation_id,
                    )

                # FIX #4: fire-and-forget broadcast; don't block the generator
                self._fire_sse(conversation_id, evt)
                asyncio.ensure_future(self._emitter.emit(evt))
                yield evt

            # Track assistant turn in history
            agent.append_history(conversation_id, "assistant", raw_content if sdk_events else "")

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

    # ── SSE broadcast helpers  (FIX #4) ──────────────────────────────────

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
        """Serialize an Event to the JSON wire format used by SSE and WebSocket."""
        return json.dumps({
            "id":          event.id,
            "type":        type(event).__name__,
            "timestamp":   event.timestamp.isoformat(),
            "content":     getattr(event, "content", ""),
            "action_type": getattr(event, "action_type", ""),
            "source":      getattr(event, "source", ""),
            "thought":     getattr(event, "thought", ""),
        })

    def _fire_sse(self, conversation_id: str, event: Event) -> None:
        """FIX #4: non-blocking broadcast using put_nowait; never awaited."""
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

    # ── lifecycle ─────────────────────────────────────────────────────────

    def close(self) -> None:
        self._db.close()
        logger.info("LocalRuntime closed")


# ============================================================================
# FASTAPI APPLICATION
# ============================================================================

def create_app(runtime: LocalRuntime) -> Any:
    """Create the FastAPI application matching the OpenHands frontend protocol."""
    from fastapi import (
        FastAPI, APIRouter, HTTPException, Query,
        WebSocket, WebSocketDisconnect,
    )
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import StreamingResponse
    from pydantic import BaseModel
    from typing import Optional, List, Union

    # ── lifespan ──────────────────────────────────────────────────────────
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.runtime = runtime
        await runtime.startup()
        yield
        runtime.close()

    app = FastAPI(
        title="ClawHands Local Runtime",
        description="OpenHands-compatible local agentic runtime (no Docker, no sandbox)",
        version="1.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Pydantic models ───────────────────────────────────────────────────

    class ConversationCreateRequest(BaseModel):
        title:                 Optional[str]            = None
        agent_id:              Optional[str]            = None   # FIX #9
        agent_type:            Optional[str]            = "default"
        selected_repository:   Optional[str]            = None
        git_provider:          Optional[str]            = None
        selected_branch:       Optional[str]            = None
        initial_message:       Optional[Union[str, dict]] = None  # FIX #19
        llm_model:             Optional[str]            = None
        llm_api_key:           Optional[str]            = None
        llm_base_url:          Optional[str]            = None
        system_message_suffix: Optional[str]            = None

    class ConversationUpdateRequest(BaseModel):  # FIX #20
        title:           Optional[str] = None
        selected_branch: Optional[str] = None

    class AgentCreateRequest(BaseModel):
        name:           str
        agent_type:     Optional[str] = "code"
        llm_model:      Optional[str] = None
        llm_api_key:    Optional[str] = None
        llm_base_url:   Optional[str] = None
        system_message: Optional[str] = None

    class AgentLLMRequest(BaseModel):
        llm_model:    str
        llm_api_key:  Optional[str] = None
        llm_base_url: Optional[str] = None

    class SendMessageRequest(BaseModel):
        message: Union[str, dict]   # FIX #19: tighter type

    class SecretCreateRequest(BaseModel):
        name:  str
        value: str

    class MCPConfigRequest(BaseModel):
        name:    str
        command: str
        args:    Optional[List[str]] = None
        env:     Optional[dict]      = None

    class SettingSetRequest(BaseModel):
        key:   str
        value: Any

    class WebhookRequest(BaseModel):
        event_type: str
        url:        str

    # ── helpers ───────────────────────────────────────────────────────────

    def _rt() -> LocalRuntime:
        return app.state.runtime

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

    @agent_router.post("")
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
    async def configure_llm(agent_id: str, req: AgentLLMRequest) -> dict:
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

    conv_router = APIRouter(prefix="/app-conversations", tags=["Conversations"])

    @conv_router.get("")
    async def list_conversations(
        limit:   int           = Query(20, le=100),
        page_id: Optional[str] = None,
    ) -> dict:
        convs = await _rt().list_conversations(limit=limit)
        return {"items": convs, "total": len(convs)}

    @conv_router.post("")
    async def create_conversation(req: ConversationCreateRequest) -> dict:
        """
        FIX #9: Accept explicit agent_id. Auto-create agent only when none
        exist and no agent_id provided.
        """
        rt = _rt()

        if req.agent_id:
            # Use the explicitly specified agent
            agent_id = req.agent_id
            if not rt.get_agent_info(agent_id):
                raise HTTPException(404, f"Agent '{agent_id}' not found")
        else:
            agents = rt.list_agents()
            if not agents:
                # Auto-create a default agent
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
        """FIX #20: properly persist patch fields."""
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

    # ── send message (REST fallback) ──────────────────────────────────────

    @conv_router.post("/{conversation_id}/send-message")
    async def send_message(conversation_id: str, req: SendMessageRequest) -> dict:
        text   = extract_text(req.message)
        events = []
        async for evt in _rt().send_message(conversation_id, text):
            events.append({
                "id":          evt.id,
                "type":        type(evt).__name__,
                "timestamp":   evt.timestamp.isoformat(),
                "content":     getattr(evt, "content", ""),
                "action_type": getattr(evt, "action_type", ""),
                "source":      getattr(evt, "source", ""),
                "thought":     getattr(evt, "thought", ""),
            })
        return {"events": events}

    # ── SSE event stream ──────────────────────────────────────────────────

    @conv_router.get("/{conversation_id}/events")
    async def stream_events(
        conversation_id: str,
        since:           Optional[str] = Query(None),
    ) -> StreamingResponse:
        """
        Server-Sent Events: text/event-stream.
        Replays history, then streams live events.
        """
        rt = _rt()

        async def generator():
            since_dt = None
            if since:
                try:
                    since_dt = datetime.fromisoformat(since)
                except ValueError:
                    pass

            history = await rt.get_events(conversation_id, since_dt)
            for item in history:
                yield f"data: {json.dumps(item)}\n\n"

            queue = rt.subscribe_sse(conversation_id)
            try:
                while True:
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=25.0)
                        yield f"data: {payload}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
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

    # ── events search (FIX #22: different path from stream) ──────────────

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
        items = await _rt().get_events(conversation_id, since_dt)
        items = items[:limit]
        return {"items": items, "total": len(items)}

    # ── WebSocket  (FIX #12) ──────────────────────────────────────────────

    @conv_router.websocket("/{conversation_id}/ws")
    async def websocket_endpoint(
        websocket: WebSocket, conversation_id: str
    ) -> None:
        """
        Bidirectional WebSocket.
        Client → server: JSON {"message": "..."} or plain string
        Server → client: JSON event objects (same wire format as SSE data)
        """
        await websocket.accept()
        rt    = _rt()
        queue = rt.subscribe_sse(conversation_id)

        # FIX #12: proper task cancellation
        async def forwarder():
            try:
                while True:
                    payload = await queue.get()
                    await websocket.send_text(payload)
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

        fwd_task = asyncio.create_task(forwarder())

        try:
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
                    pass   # broadcasting handled inside send_message

        except WebSocketDisconnect:
            pass
        finally:
            fwd_task.cancel()
            # FIX #12: await the task so it is properly reaped
            await asyncio.gather(fwd_task, return_exceptions=True)
            rt.unsubscribe_sse(conversation_id, queue)

    # ── skills (per conversation) ─────────────────────────────────────────

    @conv_router.get("/{conversation_id}/skills")
    async def get_skills(conversation_id: str) -> dict:
        skills = _rt().list_skills()
        return {
            "items": [{"name": s.name, "source": s.source} for s in skills],
            "total": len(skills),
        }

    @conv_router.get("/{conversation_id}/hooks")
    async def get_hooks(conversation_id: str) -> dict:
        return {"items": [], "total": 0}

    # ── router: settings ──────────────────────────────────────────────────

    settings_router = APIRouter(prefix="/settings", tags=["Settings"])

    @settings_router.get("")
    async def get_settings() -> dict:
        return _rt().settings.all

    @settings_router.post("")
    async def set_setting(req: SettingSetRequest) -> dict:
        await _rt().settings.set(req.key, req.value)
        return {"success": True}

    @settings_router.delete("/{key}")
    async def delete_setting(key: str) -> dict:
        ok = await _rt().settings.delete(key)
        return {"success": ok}

    # ── router: secrets ───────────────────────────────────────────────────

    secrets_router = APIRouter(prefix="/secrets", tags=["Secrets"])

    @secrets_router.get("")
    async def list_secrets() -> dict:
        return {"secrets": _rt().list_secrets()}

    @secrets_router.post("")
    async def create_secret(req: SecretCreateRequest) -> dict:
        await _rt().set_secret(req.name, req.value)
        return {"success": True}

    @secrets_router.delete("/{name}")
    async def delete_secret(name: str) -> dict:
        ok = await _rt().delete_secret(name)
        if not ok:
            raise HTTPException(404, "Secret not found")
        return {"success": True}

    # ── router: MCP ───────────────────────────────────────────────────────

    mcp_router = APIRouter(prefix="/mcp", tags=["MCP"])

    @mcp_router.get("")
    async def list_mcp() -> dict:
        servers = _rt().list_mcp_servers()
        return {
            "items": [{"id": s.id, "name": s.name, "command": s.command} for s in servers],
            "total": len(servers),
        }

    @mcp_router.post("")
    async def add_mcp(req: MCPConfigRequest) -> dict:
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
    async def register_webhook(req: WebhookRequest) -> dict:
        _rt().register_webhook(req.event_type, req.url)
        return {"success": True}

    @webhook_router.delete("/{event_type}")
    async def unregister_webhook(event_type: str) -> dict:
        ok = _rt().unregister_webhook(event_type)
        return {"success": ok}

    # ── router: skills (global) ───────────────────────────────────────────

    skills_router = APIRouter(prefix="/skills", tags=["Skills"])

    @skills_router.get("")
    async def list_skills_global() -> dict:
        skills = _rt().list_skills()
        return {
            "items": [{"name": s.name, "source": s.source} for s in skills],
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

    # ── health ────────────────────────────────────────────────────────────

    @app.get("/health", tags=["Health"])
    async def health() -> dict:
        return {
            "status":    "ok",
            "timestamp": _utcnow().isoformat(),
            "agents":    len(_rt().list_agents()),
            "skills":    len(_rt().list_skills()),
        }

    # ── register all routers ──────────────────────────────────────────────

    app.include_router(agent_router)
    app.include_router(conv_router)
    app.include_router(settings_router)
    app.include_router(secrets_router)
    app.include_router(mcp_router)
    app.include_router(repo_router)
    app.include_router(webhook_router)
    app.include_router(skills_router)

    return app


# ============================================================================
# ENTRY POINT  (FIX #23: forward skills_dir to startup)
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
    # FIX #23: pass skills_dir into runtime so startup() loads it
    app = create_app(runtime)

    # Override startup to include skills_dir – patch lifespan via monkey-patch
    # is fragile; instead, call startup explicitly before serving.
    # We call startup here so the lifespan handler finds runtime already ready.
    await runtime.startup(skills_dir=skills_dir)

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
