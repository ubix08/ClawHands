# Default Skills

Skills in this directory will be loaded by the agent.

## Structure

Skills can be:
- Repo skills (always active): `repo.md`, `AGENTS.md`, `.cursorrules`
- Knowledge skills (triggered): `*.md` files with triggers in frontmatter
- AgentSkills: `SKILL.md` files in subdirectories

## Example Skills

### Always-On Skill (repo.md)

```markdown
---
name: coding-standards
trigger: null
---

# Coding Standards

Always follow these rules:
1. Use type hints
2. Add docstrings
3. Write tests
```

### Keyword-Triggered Skill

```markdown
---
name: security-expert
triggers:
- security
- auth
- password
---

# Security Guidelines

When handling authentication:
1. Never store passwords in plain text
2. Use parameterized queries
3. Validate all inputs
```