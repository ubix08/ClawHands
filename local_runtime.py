"""Local runtime - ENTIRELY based on OpenHands SDK.

This implementation is BUILT ON TOP OF the openhands-sdk:
- Uses SDK's Agent, LLM, Workspace, AgentSettings
- Uses SDK's Conversation for conversation management  
- Uses SDK's agent_server for models/endpoints
- Same API endpoints as OpenHands

No fallback for SDK components - they are REQUIRED.
Only fallback: external tools like gh CLI for GitHub operations.
"""

import asyncio
import logging
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import AsyncIterator
from uuid import uuid4

# ============================================================================
# CORE: OpenHands SDK imports - REQUIRED
# ============================================================================
# Agent and core
from openhands.sdk import Agent, AgentContext
from openhands.sdk.llm import LLM
from openhands.sdk.workspace import LocalWorkspace
from openhands.sdk.settings import AgentSettings, ConversationSettings
from openhands.sdk.secret import StaticSecret

# Agent server - conversation, models, events (INCLUDED IN SDK)
from openhands.agent_server.models import (
    ConversationInfo,
    StartConversationRequest,
    SendMessageRequest,
    SendMessageResponse,
    TextContent as SDKTextContent,
)

logger = logging.getLogger(__name__)


# ============================================================================
# ENUMS - Same as OpenHands
# ============================================================================

class AgentState(Enum):
    """State of an agent in the runtime."""
    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    AWAITING_INPUT = "awaiting_input"
    PAUSED = "paused"
    STOPPED = "stopped"
    ERROR = "error"


class ProviderType(Enum):
    """Git provider types."""
    GITHUB = "github"
    GITLAB = "gitlab"
    BITBUCKET = "bitbucket"
    AZURE_DEVOPS = "azure"


class ConversationRole(Enum):
    """Role in a conversation."""
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


# ============================================================================
# MODELS - Use SDK agent_server models where possible, custom only where needed
# ============================================================================

@dataclass
class ContentBlock:
    """Content block for messages - uses SDK's TextContent."""
    type: str = "text"
    text: str = ""

    @classmethod
    def from_sdk(cls, sdk_content: SDKTextContent) -> "ContentBlock":
        return cls(type="text", text=sdk_content.text)


@dataclass  
class Message:
    """A message in the conversation."""
    role: ConversationRole
    content: list[ContentBlock]
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Event:
    """Base event - wrapper around SDK events."""
    id: str = field(default_factory=lambda: str(uuid4()))
    conversation_id: str = ""
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Action(Event):
    """An action taken by the agent."""
    action_type: str = ""
    thought: str = ""
    content: str = ""
    observation: str = ""


@dataclass
class Observation(Event):
    """An observation from the agent."""
    content: str = ""
    source: str = ""


# Use SDK's StartConversationRequest in API
# The SDK provides: openhands.agent_server.models.StartConversationRequest


# ============================================================================
# PROMPTS - Same as OpenHands system prompts
# ============================================================================

PLANNING_AGENT_INSTRUCTION = """<IMPORTANT_PLANNING_BOUNDARIES>
You are a Planning Agent that can ONLY create plans - you CANNOT execute code or make changes.

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
# SKILLS - Using SDK skills system
# ============================================================================

# SDK skills imports - all skill loading uses SDK
from openhands.sdk.context.skills import (
    Skill, 
    KeywordTrigger, 
    TaskTrigger,
)


class SkillLoader:
    """Load skills using SDK patterns.
    
    Supports loading from:
    - Local files (skills directory)
    - Repository (.openhands/skills/)
    - User skills (~/.openhands/skills/)
    - Global skills (OpenHands/skills/)
    """
    
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
        """Load a skill using SDK's Skill class.
        
        Args:
            name: Skill name
            content: Skill prompt/instructions
            triggers: List of trigger keywords or task descriptions
            source: Source of skill (local, repo, user, global)
            
        Returns:
            Loaded Skill instance
        """
        # Parse triggers
        keyword_triggers = []
        task_triggers = []
        
        if triggers:
            for t in triggers:
                if t.startswith("keyword:"):
                    keyword_triggers.append(KeywordTrigger(keyword=t[9:]))
                elif t.startswith("task:"):
                    task_triggers.append(TaskTrigger(description=t[5:]))
                else:
                    # Default to task trigger
                    task_triggers.append(TaskTrigger(description=t))
        
        skill = Skill(
            name=name,
            instructions=content,
            keyword_triggers=keyword_triggers,
            task_triggers=task_triggers,
            source=source,
        )
        
        self._loaded_skills[name] = skill
        logger.info(f"Loaded skill: {name} from {source}")
        
        return skill
    
    async def load_skills_from_directory(self, skills_dir: str) -> list[Skill]:
        """Load all skills from a directory.
        
        Skills are .md files with optional frontmatter:
        ---
        triggers:
        - keyword: trigger1
        - task: Do something
        ---
        # Skill content here...
        """
        import re
        
        skills = []
        dir_path = Path(skills_dir)
        
        if not dir_path.exists():
            return []
        
        for skill_file in dir_path.glob("*.md"):
            try:
                content = skill_file.read_text()
                
                # Parse frontmatter
                name = skill_file.stem
                triggers = []
                
                if content.startswith("---"):
                    parts = content.split("---", 2)
                    if len(parts) >= 3:
                        fm_text = parts[1]
                        # Extract triggers from frontmatter
                        trigger_match = re.search(r'triggers:\s*\n((?:\s+-\s+.*\n)*)', fm_text)
                        if trigger_match:
                            for line in trigger_match.group(1).strip().split("\n"):
                                if line.strip().startswith("-"):
                                    triggers.append(line.strip()[1:].strip())
                        
                        content = parts[2].strip()
                
                skill = await self.load_skill(
                    name=name,
                    content=content,
                    triggers=triggers,
                    source=str(skills_dir),
                )
                skills.append(skill)
                
            except Exception as e:
                logger.warning(f"Failed to load skill {skill_file}: {e}")
        
        return skills
    
    def get_skill(self, name: str) -> Skill | None:
        """Get a loaded skill by name."""
        return self._loaded_skills.get(name)
    
    def list_skills(self) -> list[Skill]:
        """List all loaded skills."""
        return list(self._loaded_skills.values())
    
    def match_skills(self, message: str) -> list[Skill]:
        """Match skills based on message content.
        
        Args:
            message: User message to match against
            
        Returns:
            List of matched skills
        """
        matched = []
        
        for skill in self._loaded_skills.values():
            # Check keyword triggers
            for kt in skill.keyword_triggers:
                if kt.keyword.lower() in message.lower():
                    matched.append(skill)
                    break
            
            # Check task triggers (match any word in description)
            for tt in skill.task_triggers:
                if any(word.lower() in message.lower() for word in tt.description.split()):
                    if skill not in matched:
                        matched.append(skill)
                    break
        
        return matched


# ============================================================================
# MICROAGENTS - Using SDK microagents
# ============================================================================

from openhands.sdk.context import Microagent


class MicroagentLoader:
    """Load microagents using SDK patterns.
    
    Microagents are specialized prompts for specific tasks.
    """
    
    def __init__(self, runtime: "LocalRuntime"):
        self.runtime = runtime
        self._loaded_microagents: dict[str, Microagent] = {}
    
    async def load_microagent(
        self,
        name: str,
        content: str,
        triggers: list[str] | None = None,
    ) -> Microagent:
        """Load a microagent using SDK's Microagent class."""
        microagent = Microagent(
            name=name,
            instructions=content,
            triggers=triggers or [],
        )
        
        self._loaded_microagents[name] = microagent
        logger.info(f"Loaded microagent: {name}")
        
        return microagent
    
    def match_microagent(self, message: str) -> Microagent | None:
        """Match a microagent based on message."""
        for ma in self._loaded_microagents.values():
            for trigger in ma.triggers:
                if trigger.lower() in message.lower():
                    return ma
        return None


# ============================================================================
# GIT PROVIDER - Using SDK secrets + gh CLI
# ============================================================================

class GitProvider:
    """Git provider using SDK patterns + gh CLI."""
    
    def __init__(
        self,
        provider_type: ProviderType,
        token: str | None = None,
    ):
        self.provider_type = provider_type
        self.token = token
        self.host = self._get_default_host()
    
    def _get_default_host(self) -> str:
        return {
            ProviderType.GITHUB: "github.com",
            ProviderType.GITLAB: "gitlab.com",
            ProviderType.BITBUCKET: "bitbucket.org",
            ProviderType.AZURE_DEVOPS: "dev.azure.com",
        }.get(self.provider_type, "github.com")
    
    async def get_user(self) -> dict:
        """Get current user via gh CLI."""
        if self.provider_type == ProviderType.GITHUB:
            return await self._get_github_user()
        return {}
    
    async def _get_github_user(self) -> dict:
        """Get GitHub user using gh CLI."""
        import json
        result = subprocess.run(
            ["gh", "api", "user"],
            capture_output=True,
            text=True,
            env={**os.environ, "GITHUB_TOKEN": self.token or ""},
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            return {
                "login": data.get("login"),
                "email": data.get("email"),
                "name": data.get("name"),
            }
        return {}
    
    async def list_repos(self, page: int = 1) -> list[dict]:
        """List repositories."""
        if self.provider_type == ProviderType.GITHUB:
            return await self._list_github_repos()
        return []
    
    async def _list_github_repos(self) -> list[dict]:
        """List GitHub repos using gh CLI."""
        import json
        result = subprocess.run(
            ["gh", "repo", "list", "current", "--limit", "30", "--json", "name,owner,url"],
            capture_output=True,
            text=True,
            env={**os.environ, "GITHUB_TOKEN": self.token or ""},
        )
        if result.returncode == 0:
            repos = json.loads(result.stdout)
            return [
                {"name": r["name"], "owner": r["owner"]["login"], "url": r["url"]}
                for r in repos
            ]
        return []
    
    async def get_repo(self, repo: str) -> dict:
        """Get repository details."""
        if self.provider_type == ProviderType.GITHUB:
            return await self._get_github_repo(repo)
        return {}
    
    async def _get_github_repo(self, repo: str) -> dict:
        """Get GitHub repo via gh CLI."""
        import json
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
        """List branches."""
        if self.provider_type == ProviderType.GITHUB:
            return await self._list_github_branches(repo)
        return []
    
    async def _list_github_branches(self, repo: str) -> list[dict]:
        """List GitHub branches via gh CLI."""
        import json
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
# EVENT EMITTER - SDK event patterns
# ============================================================================

class RuntimeEventEmitter:
    """Emits events."""
    
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
# LOCAL RUNTIME - SDK-BASED
# ============================================================================

class LocalRuntime:
    """Local runtime BUILT ON THE SDK.
    
    This runtime is fully based on the OpenHands SDK:
    - Agents are SDK Agent instances
    - Workspaces are SDK LocalWorkspace
    - LLM configuration from SDK
    - Skills from SDK skill system
    - Microagents from SDK
    """
    
    def __init__(
        self,
        working_dir: str = "./workspace",
        skills_dir: str | None = None,
        event_emitter: RuntimeEventEmitter | None = None,
    ):
        self.working_dir = Path(working_dir).resolve()
        self.working_dir.mkdir(parents=True, exist_ok=True)
        
        self._event_emitter = event_emitter or RuntimeEventEmitter()
        self._agents: dict[str, "RunningAgent"] = {}
        self._conversations: dict[str, dict] = {}
        self._providers: dict[str, GitProvider] = {}
        self._secrets: dict[str, StaticSecret] = {}
        
        # Initialize skill loader
        self._skill_loader = SkillLoader(self)
        
        # Initialize microagent loader
        self._microagent_loader = MicroagentLoader(self)
        
        # Load skills from directory if provided
        if skills_dir:
            import asyncio
            asyncio.get_event_loop().run_until_complete(
                self._skill_loader.load_skills_from_directory(skills_dir)
            )
    
    # ========================================================================
    # SKILL MANAGEMENT - SDK skill system
    # ========================================================================
    
    def get_skill_loader(self) -> SkillLoader:
        """Get the skill loader."""
        return self._skill_loader
    
    def get_microagent_loader(self) -> MicroagentLoader:
        """Get the microagent loader."""
        return self._microagent_loader
    
    async def load_skill(
        self,
        name: str,
        content: str,
        triggers: list[str] | None = None,
        source: str = "local",
    ) -> Skill:
        """Load a skill using SDK's Skill class."""
        return await self._skill_loader.load_skill(name, content, triggers, source)
    
    def match_skills(self, message: str) -> list[Skill]:
        """Match skills based on message."""
        return self._skill_loader.match_skills(message)
    
    # ========================================================================
    # SECRET MANAGEMENT - SDK compatible
    # ========================================================================
    
    def set_secret(self, name: str, value: str) -> None:
        """Set a secret using SDK's StaticSecret."""
        self._secrets[name] = StaticSecret(value=value)
    
    def get_secret(self, name: str) -> str | None:
        """Get a secret value."""
        secret = self._secrets.get(name)
        return secret.get_secret_value() if secret else None
    
    def delete_secret(self, name: str) -> bool:
        if name in self._secrets:
            del self._secrets[name]
            return True
        return False
    
    # ========================================================================
    # GIT PROVIDER
    # ========================================================================
    
    async def setup_git_provider(
        self,
        provider_type: ProviderType,
        token: str | None = None,
    ) -> str:
        """Setup a git provider."""
        provider_id = str(uuid4())
        self._providers[provider_id] = GitProvider(
            provider_type=provider_type,
            token=token or os.environ.get("GITHUB_TOKEN"),
        )
        logger.info(f"Setup git provider: {provider_type.value}")
        return provider_id
    
    async def get_provider(self, provider_id: str) -> GitProvider | None:
        return self._providers.get(provider_id)
    
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
    # WORKSPACE - SDK LocalWorkspace
    # ========================================================================
    
    def get_workspace(self, agent_id: str) -> LocalWorkspace:
        """Get SDK workspace for an agent."""
        return LocalWorkspace(working_dir=str(self.working_dir / agent_id))
    
    # ========================================================================
    # AGENT LIFECYCLE - SDK Agent
    # ========================================================================
    
    async def create_agent(
        self,
        name: str,
        agent_type: str = "code",
        llm: LLM | None = None,
        system_message: str | None = None,
    ) -> str:
        """Create an agent using SDK.
        
        THIS uses the SDK's AgentSettings.create_agent().
        """
        agent_id = str(uuid4())
        
        # Get workspace
        workspace = self.get_workspace(agent_id)
        
        # Create SDK Agent using AgentSettings
        # This is the CORE SDK usage
        settings = AgentSettings(
            llm=llm,
            system_message=system_message or "You are a helpful software development assistant.",
            workspace=workspace,
        )
        sdk_agent = settings.create_agent()
        
        self._agents[agent_id] = RunningAgent(
            id=agent_id,
            name=name,
            agent_type=agent_type,
            llm=llm,
            system_message=system_message,
            workspace=workspace,
            sdk_agent=sdk_agent,
        )
        
        logger.info(f"Created SDK agent {agent_id} ({name}, type={agent_type})")
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
        request: AppConversationStartRequest,
    ) -> str:
        """Create a conversation."""
        if agent_id not in self._agents:
            raise ValueError(f"Agent {agent_id} not found")
        
        conv_id = str(uuid4())
        
        conv_data = {
            "id": conv_id,
            "agent_id": agent_id,
            "messages": [],
            "events": [],
            "state": AgentState.CREATED,
            "created_at": datetime.utcnow(),
            "selected_repository": request.selected_repository,
            "git_provider": request.git_provider,
            "selected_branch": request.selected_branch,
            "llm_model": request.llm_model,
        }
        
        if request.initial_message:
            conv_data["messages"].append(Message(
                role=ConversationRole.USER,
                content=request.initial_message,
            ))
        
        self._conversations[conv_id] = conv_data
        
        await self._event_emitter.emit(Action(
            action_type="conversation_created",
            content=f"Conversation {conv_id} created",
            conversation_id=conv_id,
        ))
        
        return conv_id
    
    async def get_conversation(self, conv_id: str) -> dict | None:
        return self._conversations.get(conv_id)
    
    async def send_message(
        self,
        conversation_id: str,
        message: str,
    ) -> AsyncIterator[Event]:
        """Send message - uses SDK agent.run()."""
        if conversation_id not in self._conversations:
            raise ValueError(f"Conversation {conversation_id} not found")
        
        conv_data = self._conversations[conversation_id]
        agent_id = conv_data["agent_id"]
        
        # Add user message
        user_msg = Message(
            role=ConversationRole.USER,
            content=[ContentBlock(text=message)],
        )
        conv_data["messages"].append(user_msg)
        
        # Emit user message event
        user_event = Action(
            action_type="message",
            content=message,
            conversation_id=conversation_id,
        )
        conv_data["events"].append(user_event)
        yield user_event
        await self._event_emitter.emit(user_event)
        
        # Get agent and run using SDK
        running_agent = self._agents.get(agent_id)
        if not running_agent or not running_agent.sdk_agent:
            yield Observation(
                content="[No agent configured]",
                conversation_id=conversation_id,
            )
            return
        
        running_agent.state = AgentState.RUNNING
        
        # Run SDK agent - THIS IS THE KEY
        # agent.run() yields events from the agent loop
        async for event in running_agent.sdk_agent.run(message):
            # Convert SDK event to our event type
            # SDK events have action/observation attributes
            if hasattr(event, 'action') and event.action:
                yield Action(
                    action_type=event.action,
                    content=str(event),
                    conversation_id=conversation_id,
                )
            elif hasattr(event, 'content'):
                yield Observation(
                    content=str(event),
                    source="agent",
                    conversation_id=conversation_id,
                )
        
        running_agent.state = AgentState.AWAITING_INPUT
    
    async def get_events(
        self,
        conversation_id: str,
        since: datetime | None = None,
    ) -> list[dict]:
        conv_data = self._conversations.get(conversation_id)
        if not conv_data:
            return []
        
        events = conv_data["events"]
        if since:
            events = [e for e in events if e.timestamp > since]
        
        return [
            {
                "id": e.id,
                "type": type(e).__name__,
                "timestamp": e.timestamp.isoformat(),
                "content": getattr(e, "content", ""),
            }
            for e in events
        ]
    
    async def delete_conversation(self, conv_id: str) -> bool:
        if conv_id in self._conversations:
            del self._conversations[conv_id]
            return True
        return False


# ============================================================================
# RUNNING AGENT - Wraps SDK Agent
# ============================================================================

@dataclass
class RunningAgent:
    """Agent running with SDK Agent instance."""
    id: str
    name: str
    agent_type: str
    llm: LLM | None
    system_message: str | None
    workspace: LocalWorkspace
    sdk_agent: Agent  # SDK Agent instance - REQUIRED
    state: AgentState = AgentState.CREATED
    created_at: datetime = field(default_factory=datetime.utcnow)


# ============================================================================
# API SERVER - Uses SDK
# ============================================================================

async def start_api_server(
    runtime: LocalRuntime,
    host: str = "localhost",
    port: int = 8000,
) -> None:
    """Start API server that uses the SDK-based runtime."""
    from aiohttp import web
    
    app = web.Application()
    app["runtime"] = runtime
    
    # Conversation endpoints
    async def list_conversations(request: web.Request) -> web.Response:
        runtime = request.app["runtime"]
        conversations = [
            {"id": cid, **conv}
            for cid, conv in runtime._conversations.items()
        ]
        return web.json_response({
            "items": conversations,
            "total": len(conversations),
        })
    
    async def create_conversation(request: web.Request) -> web.Response:
        runtime = request.app["runtime"]
        data = await request.json()
        
        request_obj = AppConversationStartRequest(
            selected_repository=data.get("selected_repository"),
            git_provider=ProviderType(data.get("git_provider")) if data.get("git_provider") else None,
            selected_branch=data.get("selected_branch"),
            title=data.get("title"),
            initial_message=[ContentBlock(text=data.get("initial_message", ""))] if data.get("initial_message") else None,
            agent_type=data.get("agent_type", "default"),
        )
        
        # Get or create agent
        agents = runtime.list_agents()
        if not agents:
            agent_id = await runtime.create_agent(
                name="default",
                agent_type=request_obj.agent_type,
            )
        else:
            agent_id = agents[0].id
        
        conv_id = await runtime.create_conversation(agent_id, request_obj)
        return web.json_response({
            "conversation_id": conv_id,
            "status": "created",
        })
    
    async def get_conversation(request: web.Request) -> web.Response:
        runtime = request.app["runtime"]
        conv_id = request.match_info["conversation_id"]
        
        conv = await runtime.get_conversation(conv_id)
        if not conv:
            return web.json_response({"error": "Not found"}, status=404)
        
        return web.json_response({
            "id": conv["id"],
            "agent_id": conv["agent_id"],
            "created_at": conv["created_at"].isoformat(),
            "state": conv["state"].value,
        })
    
    async def send_message(request: web.Request) -> web.Response:
        runtime = request.app["runtime"]
        conv_id = request.match_info["conversation_id"]
        data = await request.json()
        
        message = data.get("message", {})
        content = message.get("content", [])
        text = content[0].get("text", "") if content else ""
        
        events = []
        async for event in runtime.send_message(conv_id, text):
            events.append({
                "id": event.id,
                "type": type(event).__name__,
                "timestamp": event.timestamp.isoformat(),
                "content": getattr(event, "content", ""),
            })
        
        return web.json_response({"events": events})
    
    async def get_events(request: web.Request) -> web.Response:
        runtime = request.app["runtime"]
        conv_id = request.match_info["conversation_id"]
        
        since_param = request.query.get("since")
        since = datetime.fromisoformat(since_param) if since_param else None
        
        events = await runtime.get_events(conv_id, since)
        return web.json_response({"events": events})
    
    # Repository endpoints
    async def list_repositories(request: web.Request) -> web.Response:
        runtime = request.app["runtime"]
        provider_id = list(runtime._providers.keys())[0] if runtime._providers else None
        if not provider_id:
            return web.json_response({"error": "No provider configured"}, status=400)
        
        repos = await runtime.list_repositories(provider_id)
        return web.json_response({"items": repos, "total": len(repos)})
    
    async def get_repository(request: web.Request) -> web.Response:
        runtime = request.app["runtime"]
        owner = request.match_info["owner"]
        repo_name = request.match_info["repo"]
        full_name = f"{owner}/{repo_name}"
        
        provider_id = list(runtime._providers.keys())[0] if runtime._providers else None
        if not provider_id:
            return web.json_response({"error": "No provider configured"}, status=400)
        
        repo = await runtime.get_repository(provider_id, full_name)
        return web.json_response(repo)
    
    async def list_branches(request: web.Request) -> web.Response:
        runtime = request.app["runtime"]
        owner = request.match_info["owner"]
        repo_name = request.match_info["repo"]
        full_name = f"{owner}/{repo_name}"
        
        provider_id = list(runtime._providers.keys())[0] if runtime._providers else None
        if not provider_id:
            return web.json_response({"error": "No provider configured"}, status=400)
        
        branches = await runtime.list_branches(provider_id, full_name)
        return web.json_response({"items": branches, "total": len(branches)})
    
    # Register routes
    app.router.add_get("/api/v1/app-conversations", list_conversations)
    app.router.add_post("/api/v1/app-conversations", create_conversation)
    app.router.add_get("/api/v1/app-conversations/{conversation_id}", get_conversation)
    app.router.add_post("/api/v1/app-conversations/{conversation_id}/events", send_message)
    app.router.add_get("/api/v1/app-conversations/{conversation_id}/events", get_events)
    
    app.router.add_get("/api/v1/repositories", list_repositories)
    app.router.add_get("/api/v1/repositories/{owner}/{repo}", get_repository)
    app.router.add_get("/api/v1/repositories/{owner}/{repo}/branches", list_branches)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    
    logger.info(f"SDK-based runtime API server started on http://{host}:{port}")