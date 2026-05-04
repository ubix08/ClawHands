# Local Agent Server

A personal AI coding assistant built on OpenHands SDK - replicates original agent-server behavior exactly.

## Features

- **EXACT Replication** - Uses same `get_default_tools()`, `AgentContext`, skill loading as original
- **Full Toolset** - Terminal, FileEditor, Browser, TaskTracker, etc.
- **REST API** - Full conversation management
- **WebSocket** - Real-time event streaming  
- **Direct Workspace** - No sandboxing, work on your files directly
- **Skill Loading** - Load custom skills from files (same as original)
- **Planning Agent** - Supports "plan" agent type

## Quick Start

```bash
# Install
pip install openhands-sdk openhands-tools fastapi uvicorn websockets httpx pydantic

# Set API key
export ANTHROPIC_API_KEY=sk-ant-...

# Run
python -m local_agent_server.server

# Or with custom port
python -m local_agent_server.server --port 8080
```

## Environment Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | - | Required - your API key |
| `OPENHANDS_API_KEY` | - | Alternative to above |
| `LLM_MODEL` | anthropic/claude-sonnet-4-5-20250929 | Model to use |
| `LLM_BASE_URL` | - | Custom LLM endpoint |
| `WORKSPACE_BASE_DIR` | ~/agent-workspaces | Where workspaces are created |
| `ENABLE_BROWSER` | true | Enable browser tool |
| `WEB_URL` | - | Web host context |

## Agent Configuration (Matches Original)

The server replicates EXACT behavior from:
`openhands/app_server/app_conversation/live_status_app_conversation_service.py`

```python
# Key configurations that match original:
tools = get_default_tools(enable_browser=True)  # Line 1405

agent = Agent(
    llm=llm,
    tools=tools,
    agent_context=AgentContext(
        skills=all_skills,
        system_message_suffix=effective_suffix,
    ),
)
```

## Skills Loading (Matches Original)

Skills are loaded from multiple sources (same as original):

1. `./skills/` - Local skills directory
2. `~/.openhands/skills/` - User skills directory
3. Public skills from registry

Skill formats supported:
- `repo.md`, `AGENTS.md` - Always active
- `*.md` with triggers - Keyword triggered
- `SKILL.md` - AgentSkills format

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Health check |
| `POST` | `/api/conversations` | Create conversation |
| `GET` | `/api/conversations/{id}` | Get conversation |
| `POST` | `/api/conversations/{id}/messages` | Send message |
| `GET` | `/api/conversations/{id}/events` | Get events |
| `DELETE` | `/api/conversations/{id}` | Delete |
| `POST` | `/api/workspaces/{id}/execute` | Execute command |
| `WS` | `/ws/{id}` | WebSocket stream |

## Usage Examples

### Create Conversation

```bash
curl -X POST http://localhost:8000/api/conversations \
  -H "Content-Type: application/json" \
  -d '{"initial_message": "Create a hello.py file"}'
```

### Send Message

```bash
curl -X POST http://localhost:8000/api/conversations/{id}/messages \
  -H "Content-Type: application/json" \
  -d '{"message": "Add a function that says Hello World"}'
```

### Get Events

```bash
curl http://localhost:8000/api/conversations/{id}/events
```

## Skills

Add custom skills in the `skills/` directory (same as original):

```markdown
---
name: python-expert
triggers:
- python
- code
---

# Python Guidelines

Use type hints and docstrings...
```

## Architecture

This server replicates the original agent-server:

```
Original (agent-server)          Local Server
─────────────────────          ─────────────
get_default_tools()     ─────►  get_default_tools()
AgentContext(skills=)  ─────►  AgentContext(skills=)
load_skills_from_dir() ─────►  load_skills_from_dir()
SDK Conversation    ─────►  SDK Conversation
```

## License

MIT