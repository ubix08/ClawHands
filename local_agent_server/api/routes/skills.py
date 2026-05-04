"""Skills API routes."""

from fastapi import APIRouter, HTTPException

from local_agent_server.skills import (
    get_skill_registry,
    get_skill,
    invoke_skill,
    load_skills_from_directory,
    Skill,
)

router = APIRouter(prefix="/api/skills", tags=["skills"])


@router.get("/")
async def list_skills():
    """List all available skills."""
    registry = get_skill_registry()
    skills = registry.list_skills()
    return {
        "skills": [s.name for s in skills],
        "count": len(skills),
    }


@router.get("/{skill_name}")
async def get_skill_info(skill_name: str):
    """Get skill details."""
    skill = get_skill(skill_name)
    if not skill:
        available = [s.name for s in get_skill_registry().list_skills()]
        raise HTTPException(
            status_code=404,
            detail=f"Skill not found. Available: {available}"
        )
    
    return {
        "name": skill.name,
        "description": skill.metadata.description,
        "triggers": skill.metadata.triggers,
        "version": skill.metadata.version,
    }


@router.post("/invoke/{skill_name}")
async def invoke_skill_endpoint(skill_name: str):
    """Invoke a skill and return its content."""
    content = invoke_skill(skill_name)
    if not content:
        raise HTTPException(status_code=404, detail="Skill not found")
    
    return {
        "skill": skill_name,
        "content": content,
    }


@router.post("/load")
async def load_skills(directory: str):
    """Load skills from a directory."""
    count = load_skills_from_directory(directory)
    return {
        "directory": directory,
        "loaded": count,
    }


@router.get("/trigger/{trigger}")
async def find_by_trigger(trigger: str):
    """Find skill by trigger keyword."""
    registry = get_skill_registry()
    skill = registry.find_by_trigger(trigger)
    
    if not skill:
        return {"found": False, "trigger": trigger}
    
    return {
        "found": True,
        "skill": skill.name,
        "description": skill.metadata.description,
    }