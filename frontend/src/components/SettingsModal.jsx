"""Settings panel - Provider, Model, Skills configuration."""

import { useState, useEffect } from 'react'

const PROVIDERS = {
  anthropic: {
    name: 'Anthropic',
    models: [
      'anthropic/claude-sonnet-4-5-20250929',
      'anthropic/claude-sonnet-4-20250514',
      'anthropic/claude-3-opus-20240229',
      'anthropic/claude-3-sonnet-20240229',
      'anthropic/claude-3-haiku-20240307',
    ],
  },
  openai: {
    name: 'OpenAI',
    models: [
      'openai/gpt-4o',
      'openai/gpt-4o-mini',
      'openai/gpt-4-turbo',
      'openai/gpt-4',
    ],
  },
  google: {
    name: 'Google',
    models: [
      'google/gemini-1.5-pro',
      'google/gemini-1.5-flash',
    ],
  },
  mistral: {
    name: 'Mistral',
    models: [
      'mistral/mistral-large',
      'mistral/mistral-medium',
      'mistral/mistral-small',
    ],
  },
  local: {
    name: 'Local',
    models: [
      'local/codellama',
      'local/llama3',
      'local/mixtral',
    ],
  },
}

const ALL_SKILLS = [
  { id: 'frontend-design', label: 'Frontend Design' },
  { id: 'code-review', label: 'Code Review' },
  { id: 'github', label: 'GitHub' },
  { id: 'gitlab', label: 'GitLab' },
  { id: 'security', label: 'Security' },
  { id: 'docker', label: 'Docker' },
  { id: 'kubernetes', label: 'Kubernetes' },
  { id: 'ssh', label: 'SSH' },
  { id: 'vercel', label: 'Vercel' },
  { id: 'linear', label: 'Linear' },
  { id: 'notion', label: 'Notion' },
  { id: 'discord', label: 'Discord' },
  { id: 'prd', label: 'PRD Generator' },
  { id: 'release-notes', label: 'Release Notes' },
  { id: 'theme-factory', label: 'Theme Factory' },
  { id: 'iterate', label: 'Iterate PR' },
]

function SettingsModal({ isOpen, onClose }) {
  const [provider, setProvider] = useState('anthropic')
  const [model, setModel] = useState(PROVIDERS['anthropic'].models[0])
  const [apiKey, setApiKey] = useState('')
  const [baseUrl, setBaseUrl] = useState('')
  const [enableBrowser, setEnableBrowser] = useState(true)
  const [selectedSkills, setSelectedSkills] = useState([])

  // Load saved settings on mount
  useEffect(() => {
    const saved = localStorage.getItem('agent-settings')
    if (saved) {
      try {
        const settings = JSON.parse(saved)
        if (settings.provider) setProvider(settings.provider)
        if (settings.model) setModel(settings.model)
        if (settings.baseUrl) setBaseUrl(settings.baseUrl)
        if (settings.enableBrowser !== undefined) setEnableBrowser(settings.enableBrowser)
        if (settings.selectedSkills) setSelectedSkills(settings.selectedSkills)
      } catch (e) {
        console.error('Failed to load settings:', e)
      }
    }
  }, [])

  if (!isOpen) return null

  const handleProviderChange = (newProvider) => {
    setProvider(newProvider)
    setModel(PROVIDERS[newProvider].models[0])
  }

  const toggleSkill = (skillId) => {
    setSelectedSkills(prev =>
      prev.includes(skillId)
        ? prev.filter(s => s !== skillId)
        : [...prev, skillId]
    )
  }

  const handleSave = () => {
    const settings = {
      provider,
      model,
      apiKey: apiKey ? '***' : '',
      baseUrl,
      enableBrowser,
      selectedSkills,
    }
    localStorage.setItem('agent-settings', JSON.stringify(settings))
    onClose()
  }

  const currentModels = PROVIDERS[provider]?.models || []

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <h2>Settings</h2>
          <button className="icon-btn" onClick={onClose}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M18 6L6 18M6 6l12 12" strokeLinecap="round"/>
            </svg>
          </button>
        </div>

        <div className="modal-body">
          {/* Provider */}
          <div className="form-section">
            <h3>Provider</h3>
            <div className="provider-buttons">
              {Object.entries(PROVIDERS).map(([key, value]) => (
                <button
                  key={key}
                  className={`provider-btn ${provider === key ? 'active' : ''}`}
                  onClick={() => handleProviderChange(key)}
                >
                  {value.name}
                </button>
              ))}
            </div>
          </div>

          {/* Model */}
          <div className="form-section">
            <h3>Model</h3>
            <select
              value={model}
              onChange={e => setModel(e.target.value)}
              className="select-input"
            >
              {currentModels.map(m => (
                <option key={m} value={m}>{m.split('/')[1] || m}</option>
              ))}
            </select>
          </div>

          {/* API Key */}
          <div className="form-section">
            <h3>API Key</h3>
            <input
              type="password"
              value={apiKey}
              onChange={e => setApiKey(e.target.value)}
              placeholder="sk-... (leave empty for env)"
              className="text-input"
            />
            <p className="hint">Leave empty to use OPENHANDS_API_KEY env variable</p>
          </div>

          {/* Base URL */}
          <div className="form-section">
            <h3>Base URL (Optional)</h3>
            <input
              type="text"
              value={baseUrl}
              onChange={e => setBaseUrl(e.target.value)}
              placeholder="https://api.anthropic.com"
              className="text-input"
            />
          </div>

          {/* Tools */}
          <div className="form-section">
            <h3>Tools</h3>
            <label className="checkbox-label">
              <input
                type="checkbox"
                checked={enableBrowser}
                onChange={e => setEnableBrowser(e.target.checked)}
              />
              <span>Enable Browser Tool</span>
            </label>
          </div>

          {/* Skills */}
          <div className="form-section">
            <h3>Skills</h3>
            <div className="skills-grid">
              {ALL_SKILLS.map(skill => (
                <label key={skill.id} className="skill-label">
                  <input
                    type="checkbox"
                    checked={selectedSkills.includes(skill.id)}
                    onChange={() => toggleSkill(skill.id)}
                  />
                  <span>{skill.label}</span>
                </label>
              ))}
            </div>
          </div>
        </div>

        <div className="modal-footer">
          <button className="btn btn-secondary" onClick={onClose}>Cancel</button>
          <button className="btn btn-primary" onClick={handleSave}>Save</button>
        </div>
      </div>

      <style>{`
        .modal-overlay {
          position: fixed;
          inset: 0;
          background: rgba(0, 0, 0, 0.85);
          display: flex;
          align-items: center;
          justify-content: center;
          z-index: 1000;
          animation: fadeIn 0.2s ease;
        }

        .modal {
          background: #1a1a1a;
          border: 1px solid #333;
          border-radius: 12px;
          width: 90%;
          max-width: 520px;
          max-height: 90vh;
          overflow: hidden;
          display: flex;
          flex-direction: column;
        }

        .modal-header {
          display: flex;
          justify-content: space-between;
          align-items: center;
          padding: 16px 20px;
          border-bottom: 1px solid #333;
        }

        .modal-header h2 {
          font-size: 18px;
          font-weight: 600;
          color: #f5f5f5;
        }

        .modal-body {
          flex: 1;
          overflow-y: auto;
          padding: 20px;
        }

        .modal-footer {
          display: flex;
          justify-content: flex-end;
          gap: 12px;
          padding: 16px 20px;
          border-top: 1px solid #333;
        }

        .form-section {
          margin-bottom: 24px;
        }

        .form-section h3 {
          font-size: 12px;
          font-weight: 600;
          color: #888;
          margin-bottom: 10px;
          text-transform: uppercase;
          letter-spacing: 0.05em;
        }

        .provider-buttons {
          display: flex;
          gap: 8px;
          flex-wrap: wrap;
        }

        .provider-btn {
          padding: 8px 16px;
          background: #252525;
          border: 1px solid #333;
          border-radius: 6px;
          color: #888;
          font-size: 13px;
          font-weight: 500;
          cursor: pointer;
          transition: all 0.15s ease;
        }

        .provider-btn:hover {
          border-color: #444;
          color: #aaa;
        }

        .provider-btn.active {
          background: #f59e0b;
          border-color: #f59e0b;
          color: #000;
        }

        .select-input, .text-input {
          width: 100%;
          padding: 10px 14px;
          background: #252525;
          border: 1px solid #333;
          border-radius: 6px;
          color: #f5f5f5;
          font-size: 14px;
        }

        .select-input:focus, .text-input:focus {
          outline: none;
          border-color: #f59e0b;
        }

        .hint {
          font-size: 12px;
          color: #666;
          margin-top: 6px;
        }

        .checkbox-label {
          display: flex;
          align-items: center;
          gap: 10px;
          cursor: pointer;
          font-size: 14px;
          color: #ccc;
        }

        .checkbox-label input {
          width: 18px;
          height: 18px;
          accent-color: #f59e0b;
        }

        .skills-grid {
          display: grid;
          grid-template-columns: repeat(2, 1fr);
          gap: 8px;
        }

        .skill-label {
          display: flex;
          align-items: center;
          gap: 8px;
          padding: 8px 12px;
          background: #252525;
          border-radius: 6px;
          font-size: 13px;
          color: #aaa;
          cursor: pointer;
        }

        .skill-label input {
          accent-color: #f59e0b;
        }

        .btn {
          padding: 10px 20px;
          border-radius: 6px;
          font-size: 14px;
          font-weight: 500;
          cursor: pointer;
          transition: all 0.15s ease;
        }

        .btn-secondary {
          background: #252525;
          border: 1px solid #333;
          color: #aaa;
        }

        .btn-secondary:hover {
          border-color: #444;
        }

        .btn-primary {
          background: #f59e0b;
          border: 1px solid #f59e0b;
          color: #000;
        }

        .btn-primary:hover {
          background: #fbbf24;
        }

        .icon-btn {
          background: none;
          border: none;
          color: #888;
          cursor: pointer;
          padding: 4px;
        }

        .icon-btn:hover {
          color: #f5f5f5;
        }

        .icon-btn svg {
          width: 20px;
          height: 20px;
        }

        @keyframes fadeIn {
          from { opacity: 0; }
          to { opacity: 1; }
        }

        @media (max-width: 640px) {
          .modal {
            width: 100%;
            height: 100%;
            max-height: 100%;
            border-radius: 0;
          }
          .skills-grid {
            grid-template-columns: 1fr;
          }
        }
      `}</style>
    </div>
  )
}

export default SettingsModal