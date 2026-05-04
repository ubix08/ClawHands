import { useEffect, useRef } from 'react'

function MessageList({ messages, isLoading }) {
  const bottomRef = useRef(null)

  // Scroll to bottom on new messages
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, isLoading])

  return (
    <div className="messages">
      {messages.map((msg, index) => (
        <Message key={index} {...msg} />
      ))}
      
      {isLoading && <TypingIndicator />}
      
      <div ref={bottomRef} />
    </div>
  )
}

function Message({ role, content, timestamp }) {
  return (
    <div className={`message message--${role}`}>
      <div className="message__avatar">
        {role === 'user' ? 'U' : 'LA'}
      </div>
      <div className="message__content">
        <ProcessContent content={content} />
      </div>
    </div>
  )
}

function ProcessContent({ content }) {
  // Simple code block rendering
  if (content.includes('```')) {
    const parts = content.split(/(```[\s\S]*?```)/g)
    return (
      <>
        {parts.map((part, i) => {
          if (part.startsWith('```')) {
            const code = part.slice(3, -3)
            const lines = code.split('\n')
            const lang = lines[0].trim()
            const codeContent = lang ? lines.slice(1).join('\n') : lines.join('\n')
            return (
              <pre key={i}>
                <code>{codeContent}</code>
              </pre>
            )
          }
          return <span key={i}>{part}</span>
        })}
      </>
    )
  }
  
  return <span>{content}</span>
}

function TypingIndicator() {
  return (
    <div className="typing">
      <div className="message__avatar">LA</div>
      <div className="typing__dots">
        <span className="typing__dot"></span>
        <span className="typing__dot"></span>
        <span className="typing__dot"></span>
      </div>
    </div>
  )
}

export default MessageList