---
name: Frontend Design
description: Create distinctive, production-grade frontend interfaces that avoid generic "AI slop" aesthetics. Implement real working code with exceptional attention to aesthetic details and creative choices.
triggers:
- build frontend
- create ui
- design interface
- make frontend
- build interface
- design web
version: 1.0.0
tags:
- frontend
- react
- ui
- ux
---

# Frontend Design Skill

This skill guides creation of distinctive, production-grade frontend interfaces that avoid generic "AI slop" aesthetics. Implement real working code with exceptional attention to aesthetic details and creative choices.

## Design Thinking

**CRITICAL**: Before coding, understand the context and commit to a BOLD aesthetic direction:

### Purpose
- What problem does this interface solve? Who uses it?
- Context: Chat UI for local coding agent

### Tone Direction
Pick an extreme that fits the chat interface context:
- **Refined Modern**: Clean, minimal, premium feel with subtle depth
- Focus on readability and professional aesthetics
- Dark theme preferred for developer tools

### Constraints
- Framework: React 18 with functional components
- Performance: Fast load, smooth interactions
- Accessibility: WCAG compliant
- Mobile-first responsive design

### Differentiation
- Unique typography (NOT Inter/Arial)
- Cohesive dark theme with warm accent color
- Smooth micro-interactions
- Professional, not "AI slop"

## Frontend Aesthetics Guidelines

### Typography
Choose fonts that are distinctive:
- **Display**: DM Sans, Satoshi, or Geist (not Inter!)
- **Mono**: JetBrains Mono, Fira Code (not Roboto mono!)

### Colors
Commit to a cohesive dark theme:
```css
:root {
  --bg-primary: #0a0a0b;
  --bg-secondary: #111113;
  --bg-tertiary: #18181b;
  --text-primary: #fafafa;
  --text-secondary: #a1a1aa;
  --accent: #f59e0b;  /* Warm amber */
  --border: #27272a;
}
```

### Motion
- Page load: staggered reveals
- Hover: subtle lift/shadow
- Typing indicator: bouncing dots
- Messages: slide-up animation

### Layout
- Header: fixed 56px
- Messages: scrollable with max-width
- Input: fixed bottom with safe area

## Implementation

### Create Components

```jsx
// App.jsx - Main entry
function App() {
  const [messages, setMessages] = useState([])
  
  return (
    <div className="app">
      <Header />
      <MessageList messages={messages} />
      <ChatInput />
    </div>
  )
}
```

### Style with CSS Variables

```css
.app {
  display: flex;
  flex-direction: column;
  height: 100vh;
  background: var(--bg-primary);
}

.message {
  animation: slideUp 0.3s ease;
}

@keyframes slideUp {
  from { opacity: 0; transform: translateY(8px); }
  to { opacity: 1; transform: translateY(0); }
}
```

### Mobile First

```css
@media (max-width: 640px) {
  .header { height: 48px; }
  .message__content { max-width: 100%; }
}
```

## Files to Create

For a complete chat interface:
- `index.html` - Entry HTML
- `src/main.jsx` - React entry
- `src/App.jsx` - Main component
- `src/styles/main.css` - All styles
- `src/components/Header.jsx` - Navigation
- `src/components/MessageList.jsx` - Messages
- `src/components/ChatInput.jsx` - Input
- `src/components/Welcome.jsx` - Welcome
- `vite.config.js` - Vite config

## Quality Checklist

- [ ] Use distinctive fonts (DM Sans + JetBrains Mono)
- [ ] Dark theme with warm accent
- [ ] CSS variables for consistency
- [ ] Responsive breakpoints
- [ ] Message animations
- [ ] Typing indicator
- [ ] Online status indicator
- [ ] Code block styling
- [ ] Mobile-friendly input