---
name: Code Review
description: Rigorous code review focusing on data structures, simplicity, security, pragmatism, and risk/safety evaluation. Provides brutally honest, actionable feedback.
triggers:
- code review
- review code
- review pr
- review pull request
version: 1.0.0
tags:
- code review
- quality
- security
---

# Code Review Skill

Rigorous code review focusing on data structures, simplicity, security, pragmatism, and risk/safety evaluation. Provides brutally honest, actionable feedback.

## Review Focus Areas

### 1. Data Structures
- Are appropriate data structures used?
- Is time/space complexity acceptable?
- Are there better alternatives?

### 2. Simplicity
- Is this the simplest solution?
- Could a senior developer understand this in 30 seconds?
- Are there hidden complexities?

### 3. Security
- Input validation?
- SQL injection vectors?
- XSS/CSRF vulnerabilities?
-_SECRET_ handling?

### 4. Pragmatism
- Does this solve the actual problem?
- Is there unnecessary code?
- Are there unused imports/dependencies?

### 5. Risk/Safety
- What can go wrong?
- Edge cases handling?
- Error handling?
- Race conditions?

## Review Checklist

- [ ] Correctness - Does it work?
- [ ] Security - Is it safe?
- [ ] Performance - Is it fast enough?
- [ ] Maintainability - Can others understand?
- [ ] Testability - Can we test it?
- [ ] Error handling - Are errors caught?

## Risk Assessment Levels

| Level | Description |
|-------|------------|
| LOW | Read-only, local changes |
| MEDIUM | Local edits, moderate risk |
| HIGH | External calls, sensitive data |

## Output Format

When reviewing, provide:

```markdown
## Code Review: {PR/branch}

### Files Changed
- src/file.py

### Issues Found
1. **Security**: Line 42 - No input validation
2. **Performance**: O(n²) loop - consider using dict

### Recommendations
- Add input sanitization
- Cache the lookup

### Risk: MEDIUM
```