# OpenCLAIR - Local OpenHands Implementation

An OpenHands-inspired local coding agent that works **without Docker/sandboxing** on your local filesystem.

## What is OpenCLAIR?

**OpenCLAIR** = OpenHands Local AI Coding Agent - a streamlined version of OpenHands that:
- Works directly on your current working directory (CWD)
- No Docker containers required
- No sandboxing - full filesystem access
- Lightweight and fast
- Full REST API + WebSocket interface

## Comparison

| Aspect | OpenHands (Original) | OpenCLAIR (Local) |
|--------|---------------------|------------------|
| Runtime | Docker container | Direct execution |
| Isolation | Full sandbox | Host filesystem |
| Database | PostgreSQL | In-memory |
| Cache | Redis | In-memory |
| State | Persistent | Ephemeral |
| Setup | Complex | Simple |
| Lines of code | ~6,700 | ~600 |

### Features Comparison

| Feature | OpenHands | OpenCLAIR | Notes |
|---------|----------|----------|-------|
| get_default_tools(enable_browser=True) | ✅ | ✅ | Exact same |
| AgentContext with skills | ✅ | ✅ | Custom skills |
| Planning agent support | ✅ | ✅ | Via AgentType |
| System message suffix | ✅ | ✅ | Via config |
| LLM configuration | ✅ | ✅ | env vars |
| Conversation management | ✅ | ✅ | In-memory |
| Workspace management | ✅ | ✅ | Local dirs |
| REST API | ✅ | ✅ | Similar |
| WebSocket streaming | ✅ | ✅ | Real-time |
| Admin endpoints | ✅ | ✅ | Health/stats |
| GitHub integration | ✅ | ✅ | Via PyGithub |
| Skills system | ✅ | ✅ | .md files |

### What OpenCLAIR Has

- **Agent Factory**: Creates OpenHands agents with exact same config
- **Conversation Manager**: In-memory conversation storage
- **Workspace Manager**: Creates local working directories
- **Skills**: Custom .md skill files with triggers
- **REST API**: Full CRUD for conversations + admin
- **WebSocket**: Real-time streaming
- **GitHub**: PyGithub integration
- **Server**: FastAPI entry point

### What OpenCLAIR Lacks (by design)

- No Docker/sandboxing
- No PostgreSQL persistence
- No Redis caching
- No user authentication
- No billing/subscriptions
- No V1 API compatibility

## Usage

```bash
# Install
cd local_agent_server
pip install -e .

# Run
OPENHANDS_API_KEY=sk-... python -m local_agent_server.server

# API available at http://localhost:8000
# Docs at http://localhost:8000/docs
```

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /` | Root info |
| `GET /health` | Health check |
| `POST /api/conversations` | Create conversation |
| `GET /api/conversations/{id}` | Get conversation |
| `POST /api/conversations/{id}/messages` | Send message |
| `POST /api/conversations/{id}/run` | Run conversation |
| `GET /api/conversations/{id}/events` | Get events |
| `DELETE /api/conversations/{id}` | Delete |
| `WS /ws/{id}` | Real-time streaming |
| `WS /chat/{id}` | Interactive chat |
| `GET /api/admin/stats` | Server statistics |
| `GET /api/skills/` | List skills |
| `GET /api/github/repos` | GitHub repos |

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `OPENHANDS_API_KEY` | LLM API key | Required |
| `LLM_MODEL` | Model to use | anthropic/claude-sonnet-4-5-20250929 |
| `LLM_BASE_URL` | Custom LLM endpoint | None |
| `ENABLE_BROWSER` | Enable browser tool | true |
| `WORKSPACE_BASE_DIR` | Working directory | ./workspace |
| `PORT` | Server port | 8000 |

## Architecture

```
local_agent_server/
├── api/                    # REST API
│   ├── routes/
│   │   ├── admin.py       # Admin endpoints
│   │   ├── conversations.py # Conversation CRUD
│   │   ├── github.py      # GitHub integration
│   │   ├── skills.py     # Skills API
│   │   └── workspaces.py  # Workspace ops
│   └── websocket.py     # WebSocket handlers
├── core/
│   └── config.py        # Configuration
├── models/
│   └── schemas.py     # Pydantic models
├── services/
│   ├── agent_factory.py     # Agent creation
│   ├── conversation_manager.py # Conversations
│   └── workspace_manager.py # Workspaces
├── skills/
│   ├── __init__.py        # Skills system
│   ├── code-review.md     # Code review skill
│   └── frontend-design.md  # Frontend skill
├── integrations/
│   └── github/         # GitHub integration
├── utils/
│   └── helpers.py      # Utility functions
└── server.py           # Entry point
```

## Use Cases

### 1. Personal Coding Assistant

```bash
# Start server with your API key
OPENHANDS_API_KEY=sk-... python -m local_agent_server.server

# In browser, go to http://localhost:8000
# Chat with the agent about your code
```

### 2. CI/CD Integration

```bash
# Create conversation via API
curl -X POST http://localhost:8000/api/conversations

# Send message
curl -X POST http://localhost:8000/api/conversations/{id}/messages \
  -d '{"message": "Run tests"}'

# Get events
curl http://localhost:8000/api/conversations/{id}/events
```

### 3. GitHub Automation

```bash
# List repos
curl http://localhost:8000/api/github/repos

# Create PR
curl -X POST "http://localhost:8000/api/github/repos/owner/repo/pulls?title=Fix&head=fix-branch&base=main"
```

## Extending OpenCLAIR

### Add New Skills

Create `skills/my-skill.md`:

```yaml
---
name: My Skill
description: What it does
triggers:
- trigger word
---
# Skill content
```

### Add Custom Tools

In `services/agent_factory.py`:

```python
def get_tools(self, agent_type, enable_browser):
    tools = get_default_tools(enable_browser=enable_browser)
    # Add custom tools
    tools.append(MyCustomTool())
    return tools
```

### Add API Routes

Create `api/routes/my_route.py`:

```python
from fastapi import APIRouter

router = APIRouter()

@router.get("/my-endpoint")
async def my_endpoint():
    return {"result": "ok"}
```

Then register in `server.py`.

## License

MIT - Modify freely for your needs!