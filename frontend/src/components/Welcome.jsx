import { useState, useRef, useEffect } from 'react'

function Welcome({ onSendMessage }) {
  const [value, setValue] = useState('')
  const inputRef = useRef(null)

  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  const handleSubmit = (e) => {
    e.preventDefault()
    if (value.trim()) {
      onSendMessage(value)
    }
  }

  const quickActions = [
    "Help me write a function to process data",
    "Create a simple REST API",
    "Explain this codebase",
    "Find and fix bugs"
  ]

  return (
    <div className="welcome">
      <div className="welcome__icon">LA</div>
      <h1 className="welcome__title">Local Agent</h1>
      <p className="welcome__subtitle">
        Your personal AI coding assistant. Works directly on your files.
      </p>
      
      <form onSubmit={handleSubmit} style={{ width: '100%', maxWidth: '480px', marginTop: 'var(--space-xl)' }}>
        <div className="input-wrapper">
          <input
            ref={inputRef}
            type="text"
            className="input"
            placeholder="Ask anything..."
            value={value}
            onChange={(e) => setValue(e.target.value)}
            style={{ width: '100%' }}
          />
          <button 
            type="submit" 
            className="send-button"
            disabled={!value.trim()}
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
          </button>
        </div>
      </form>
      
      <div style={{ 
        display: 'flex', 
        flexWrap: 'wrap', 
        gap: 'var(--space-sm)', 
        justifyContent: 'center',
        marginTop: 'var(--space-lg)'
      }}>
        {quickActions.map((action) => (
          <button
            key={action}
            className="btn btn--secondary"
            onClick={() => {
              setValue(action)
              onSendMessage(action)
            }}
          >
            {action}
          </button>
        ))}
      </div>
    </div>
  )
}

export default Welcome