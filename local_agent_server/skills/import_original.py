#!/usr/bin/env python3
"""Import original OpenHands skills to local agent."""

import os
import shutil
from pathlib import Path

ORIGINAL_SKILLS_DIR = "/home/openhands/.openhands/cache/skills/public-skills/skills"

def get_skill_name(path: str) -> str:
    """Extract skill name from path."""
    return path.replace("-", "_").replace(" ", "_").lower()

def import_skills():
    """Import all skills from original location."""
    source = Path(ORIGINAL_SKILLS_DIR)
    target = Path(__file__).parent / "skills"
    
    if not source.exists():
        print(f"Original skills not found: {source}")
        return
    
    # Create target directory
    target.mkdir(exist_ok=True)
    
    # Count imported
    count = 0
    
    for skill_dir in sorted(source.iterdir()):
        if not skill_dir.is_dir():
            continue
            
        # Look for SKILL.md or commands
        skill_md = skill_dir / "SKILL.md"
        commands_dir = skill_dir / "commands"
        
        if skill_md.exists():
            # Copy main skill file
            dest = target / f"{skill_dir.name}.md"
            shutil.copy2(skill_md, dest)
            count += 1
            
            # Copy command files if present
            if commands_dir.exists():
                for cmd in commands_dir.glob("*.md"):
                    dest_cmd = target / f"{skill_dir.name}_{cmd.stem}.md"
                    shutil.copy2(cmd, dest_cmd)
                    count += 1
                    
        print(f"Imported: {skill_dir.name}")
    
    print(f"\nTotal skills imported: {count}")
    print(f"Location: {target}")

if __name__ == "__main__":
    import_skills()