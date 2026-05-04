# Local Agent Server - Comparison with Original Agent Server

## Overview

This document compares the Local Agent Server implementation with the original OpenHands agent-server.

## Architecture Comparison

| Aspect | Original Agent Server | Local Agent Server |
|--------|-------------------|-----------------|
| **Runtime** | Docker sandboxed | Direct execution |
| **API** | FastAPI + V1 routes | FastAPI |
| **State** | PostgreSQL + Redis | In-memory |
| **Integrations** | Extensible | GitHub (PyGithub) |

## Feature Comparison

### Core Features

| Feature | Original | Local Agent |
|---------|---------|------------|
| `get_default_tools(enable_browser=True)` | ✅ | ✅ |
| `AgentContext` with skills | ✅ | ✅ |
| Planning agent support | ✅ | ✅ |
| System message suffix | ✅ | ✅ |
| LLM configuration | ✅ | ✅ |

### API Endpoints

| Original Route | Local Route |
|---------------|------------|
| `/api/v1/conversations` | `/api/conversations` |
| `/api/v1/conversations/{id}/messages` | `/api/conversations/{id}/messages` |
| `/api/v1/conversations/{id}/events` | `/api/conversations/{id}/events` |
| `/health` | `/health` |
| `/api/v1/settings` | `/api/admin/settings` |

### Admin Endpoints

| Feature | Original | Local Agent |
|---------|---------|------------|
| Health check | ✅ | ✅ |
| Statistics | ✅ | ✅ |
| Settings management | ✅ | ✅ |
| Reset server | ✅ | ✅ |
| List workspaces | ✅ | ✅ |

### WebSocket

| Feature | Original | Local Agent |
|---------|---------|------------|
| Real-time streaming | ✅ | ✅ |
| `/ws/{id}` endpoint | ✅ | ✅ |
| `/chat/{id}` endpoint | ✅ | ✅ |

### GitHub Integration

The Local Agent Server includes a PyGithub-based integration mirroring the original:

| Feature | Original | Local Agent |
|---------|---------|------------|
| List repos | ✅ | ✅ |
| Get repo | ✅ | ✅ |
| Create repo | ✅ | ✅ |
| Branch operations | ✅ | ✅ |
| Pull requests | ✅ | ✅ |
| Issues | ✅ | ✅ |
| Commits | ✅ | ✅ |
| File contents | ✅ | ✅ |

## Configuration

### Environment Variables

| Variable | Original | Local Agent |
|----------|---------|------------|
| `OPENHANDS_API_KEY` | ✅ | ✅ |
| `LLM_MODEL` | ✅ | ✅ |
| `LLM_BASE_URL` | ✅ | ✅ |
| `ENABLE_BROWSER` | ✅ | ✅ |
| `WORKSPACE_BASE_DIR` | ✅ | ✅ |
| `GITHUB_TOKEN` | - | ✅ |

## Differences

1. **No sandboxing**: Local Agent Server works directly on the host filesystem
2. **In-memory state**: Unlike original's PostgreSQL/Redis
3. **Simpler deployment**: No Docker required
4. **GitHub via PyGithub**: Simplified vs original's custom service

## Extensibility

The Local Agent Server is modular:
- `services/` - Pluggable service layer
- `integrations/` - GitHub, GitLab, etc.
- `api/routes/` - Custom route handlers