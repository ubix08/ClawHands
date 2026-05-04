import { useState, useRef, useEffect } from 'react'
import Header from './components/Header'
import MessageList from './components/MessageList'
import ChatInput from './components/ChatInput'
import Welcome from './components/Welcome'
import SettingsModal from './components/SettingsModal'
import GitHubPanel from './components/GitHubPanel'

const API_BASE = '/api'

function App() {
  const [conversationId, setConversationId] = useState(null)
  const [messages, setMessages] = useState([])
  const [isLoading, setIsLoading] = useState(false)
  const [connected, setConnected] = useState(false)
  const [showSettings, setShowSettings] = useState(false)
  const [showGitHub, setShowGitHub] = useState(false)
  const wsRef = useRef(null)

  // Create conversation on mount
  useEffect(() => {
    createConversation()
    return () => {
      if (wsRef.current) {
        wsRef.current.close()
      }
    }
  }, [])

  // Connect WebSocket when conversation is created
  useEffect(() => {
    if (conversationId) {
      connectWebSocket(conversationId)
    }
  }, [conversationId])

  const createConversation = async () => {
    try {
      const res = await fetch(`${API_BASE}/conversations`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
      })
      const data = await res.json()
      setConversationId(data.id)
    } catch (err) {
      console.error('Failed to create conversation:', err)
    }
  }

  const connectWebSocket = (convId) => {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(`${protocol}//${window.location.host}/chat/${convId}`)
    
    ws.onopen = () => {
      setConnected(true)
    }
    
    ws.onmessage = (event) => {
      const data = JSON.parse(event.data)
      handleWebSocketMessage(data)
    }
    
    ws.onclose = () => {
      setConnected(false)
    }
    
    ws.onerror = (err) => {
      console.error('WebSocket error:', err)
    }
    
    wsRef.current = ws
  }

  const handleWebSocketMessage = (data) => {
    switch (data.type) {
      case 'status':
        setIsLoading(data.status === 'running')
        break
      case 'event':
        if (data.event_type === 'agent_thought' || data.event_type === 'agent_action') {
          setMessages(prev => [...prev, {
            role: 'assistant',
            content: data.content || data.action,
            timestamp: new Date().toISOString()
          }])
        }
        break
      case 'complete':
      case 'done':
        setIsLoading(false)
        break
      case 'error':
        setMessages(prev => [...prev, {
          role: 'assistant',
          content: `Error: ${data.error}`,
          timestamp: new Date().toISOString()
        }])
        setIsLoading(false)
        break
    }
  }

  const sendMessage = async (content) => {
    if (!content.trim() || !wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      return
    }

    // Add user message immediately
    setMessages(prev => [...prev, {
      role: 'user',
      content,
      timestamp: new Date().toISOString()
    }])

    // Send via WebSocket
    wsRef.current.send(JSON.stringify({
      type: 'message',
      content
    }))

    setIsLoading(true)
  }

  return (
    <div className="app">
      <Header 
        connected={connected} 
        onOpenSettings={() => setShowSettings(true)}
        onOpenGitHub={() => setShowGitHub(true)}
      />
      
      <main className="chat">
        {messages.length === 0 && !isLoading ? (
          <Welcome onSendMessage={sendMessage} />
        ) : (
          <>
            <MessageList messages={messages} isLoading={isLoading} />
            <ChatInput 
              onSend={sendMessage} 
              disabled={isLoading || !connected}
            />
          </>
        )}
      </main>

      <SettingsModal 
        isOpen={showSettings} 
        onClose={() => setShowSettings(false)} 
      />
      
      <GitHubPanel
        isOpen={showGitHub}
        onClose={() => setShowGitHub(false)}
      />
    </div>
  )
}

export default App