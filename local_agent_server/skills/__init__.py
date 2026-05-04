"""Skills system for Local Agent Server - Replicates OpenHands Cloud skills."""

import os
import re
import logging
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class SkillMetadata:
    """Metadata extracted from skill frontmatter."""
    name: str
    description: str
    triggers: list[str] = field(default_factory=list)
    version: str = "1.0.0"
    author: Optional[str] = None
    tags: list[str] = field(default_factory=list)


@dataclass
class Skill:
    """A skill that can be invoked by the agent."""
    name: str
    content: str
    metadata: SkillMetadata
    
    @classmethod
    def from_file(cls, path: Path) -> 'Skill':
        """Load a skill from a markdown file."""
        content = path.read_text(encoding='utf-8')
        metadata = cls._parse_frontmatter(content, path.stem)
        return cls(
            name=metadata.name,
            content=content,
            metadata=metadata
        )
    
    @classmethod
    def _parse_frontmatter(cls, content: str, fallback_name: str) -> SkillMetadata:
        """Parse YAML frontmatter from skill content."""
        # Match frontmatter block
        match = re.match(r'^---\n(.*?)\n---\n', content, re.DOTALL)
        
        if match:
            frontmatter = match.group(1)
            name = fallback_name.replace('-', ' ').title()
            description = ""
            triggers = []
            version = "1.0.0"
            author = None
            tags = []
            
            for line in frontmatter.split('\n'):
                if ':' in line:
                    key, value = line.split(':', 1)
                    key = key.strip().lower()
                    value = value.strip()
                    
                    if key == 'name':
                        name = value
                    elif key == 'description':
                        description = value
                    elif key == 'triggers':
                        triggers = [t.strip() for t in value.lstrip('[').rstrip(']').split(',')]
                    elif key == 'version':
                        version = value
                    elif key == 'author':
                        author = value
                    elif key == 'tags':
                        tags = [t.strip() for t in value.lstrip('[').rstrip(']').split(',')]
            
            return SkillMetadata(
                name=name,
                description=description,
                triggers=triggers,
                version=version,
                author=author,
                tags=tags
            )
        
        # No frontmatter - just use fallback
        return SkillMetadata(
            name=fallback_name.replace('-', ' ').title(),
            description="Custom skill"
        )
    
    def get_prompt(self) -> str:
        """Get the skill content as a prompt (without frontmatter)."""
        # Remove frontmatter for the prompt
        match = re.match(r'^---\n.*?\n---\n', self.content, re.DOTALL)
        if match:
            return self.content[match.end():]
        return self.content


class SkillRegistry:
    """Registry for loading and managing skills."""
    
    def __init__(self, skills_dir: Optional[str] = None):
        self.skills_dir = Path(skills_dir) if skills_dir else None
        self._skills: dict[str, Skill] = {}
        self._triggers: dict[str, str] = {}  # trigger -> skill name
    
    def load_skills(self, directory: Optional[Path] = None) -> None:
        """Load all skills from a directory."""
        skills_path = directory or self.skills_dir
        if not skills_path:
            return
        
        if not skills_path.exists():
            logger.warning(f"Skills directory does not exist: {skills_path}")
            return
        
        for md_file in skills_path.glob("*.md"):
            if md_file.name == "README.md":
                continue
            
            try:
                skill = Skill.from_file(md_file)
                self._skills[skill.name.lower()] = skill
                
                # Register triggers
                for trigger in skill.metadata.triggers:
                    self._triggers[trigger.lower()] = skill.name.lower()
                
                logger.info(f"Loaded skill: {skill.name}")
            except Exception as e:
                logger.error(f"Failed to load skill {md_file.name}: {e}")
    
    def get_skill(self, name: str) -> Optional[Skill]:
        """Get a skill by name."""
        return self._skills.get(name.lower())
    
    def find_by_trigger(self, trigger: str) -> Optional[Skill]:
        """Find a skill by trigger keyword."""
        skill_name = self._triggers.get(trigger.lower())
        if skill_name:
            return self._skills.get(skill_name)
        return None
    
    def list_skills(self) -> list[SkillMetadata]:
        """List all available skills."""
        return [s.metadata for s in self._skills.values()]
    
    def search(self, query: str) -> list[Skill]:
        """Search skills by name or description."""
        query = query.lower()
        results = []
        for skill in self._skills.values():
            if query in skill.name.lower() or query in skill.metadata.description.lower():
                results.append(skill)
        return results
    
    @property
    def count(self) -> int:
        """Number of loaded skills."""
        return len(self._skills)


# Global skill registry
_registry: Optional[SkillRegistry] = None


def get_skill_registry() -> SkillRegistry:
    """Get the global skill registry."""
    global _registry
    if _registry is None:
        _registry = SkillRegistry()
    return _registry


def load_skills_from_directory(directory: str) -> int:
    """Load skills from a directory."""
    registry = get_skill_registry()
    registry.load_skills(Path(directory))
    return registry.count


def get_skill(name: str) -> Optional[Skill]:
    """Get a skill by name."""
    return get_skill_registry().get_skill(name)


def invoke_skill(name: str) -> Optional[str]:
    """Invoke a skill by name and get its prompt content."""
    skill = get_skill(name)
    if skill:
        return skill.get_prompt()
    return None