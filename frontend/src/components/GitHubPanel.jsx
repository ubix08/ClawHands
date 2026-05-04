"""GitHub panel - Repository, Issues, PRs configuration."""

import { useState, useEffect } from 'react'

const API_BASE = '/api'

function GitHubPanel({ isOpen, onClose }) {
  const [token, setToken] = useState('')
  const [connected, setConnected] = useState(false)
  const [repos, setRepos] = useState([])
  const [selectedRepo, setSelectedRepo] = useState(null)
  const [issues, setIssues] = useState([])
  const [prs, setPrs] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [activeTab, setActiveTab] = useState('repos')

  // Load saved token on mount
  useEffect(() => {
    const saved = localStorage.getItem('github-token')
    if (saved) {
      setToken(saved)
      fetchRepos(saved)
    }
  }, [])

  const fetchRepos = async (githubToken) => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch(`${API_BASE}/github/repos`, {
        headers: { 'Authorization': `Bearer ${githubToken}` }
      })
      if (!res.ok) throw new Error('Failed to fetch repos')
      const data = await res.json()
      setRepos(data.repositories || [])
      setConnected(true)
    } catch (e) {
      setError(e.message)
      setConnected(false)
    } finally {
      setLoading(false)
    }
  }

  const fetchIssues = async (repoFullName) => {
    setLoading(true)
    try {
      const res = await fetch(`${API_BASE}/github/repos/${repoFullName}/issues`, {
        headers: { 'Authorization': `Bearer ${token}` }
      })
      if (!res.ok) throw new Error('Failed to fetch issues')
      const data = await res.json()
      setIssues(data.issues || [])
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  const fetchPRs = async (repoFullName) => {
    setLoading(true)
    try {
      const res = await fetch(`${API_BASE}/github/repos/${repoFullName}/pulls`, {
        headers: { 'Authorization': `Bearer ${token}` }
      })
      if (!res.ok) throw new Error('Failed to fetch PRs')
      const data = await res.json()
      setPrs(data.pull_requests || [])
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  const handleConnect = () => {
    if (!token.trim()) {
      setError('Please enter a GitHub token')
      return
    }
    localStorage.setItem('github-token', token)
    fetchRepos(token)
  }

  const handleDisconnect = () => {
    localStorage.removeItem('github-token')
    setToken('')
    setConnected(false)
    setRepos([])
    setSelectedRepo(null)
    setIssues([])
    setPrs([])
  }

  const handleRepoSelect = (repo) => {
    setSelectedRepo(repo)
    fetchIssues(repo.full_name)
    fetchPRs(repo.full_name)
    setActiveTab('repos')
  }

  if (!isOpen) return null

  return (
    <div className="panel-overlay" onClick={onClose}>
      <div className="panel" onClick={e => e.stopPropagation()}>
        <div className="panel-header">
          <div className="header-title">
            <svg viewBox="0 0 24 24" fill="currentColor" width="24" height="24">
              <path d="M12 0c-6.626 0-12 5.373-12 12 0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.235 1.839 1.235 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23.957-.266 1.983-.399 3.003-.404 1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.476 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576 4.765-1.589 8.199-6.086 8.199-11.386 0-6.627-5.373-12-12-12z"/>
            </svg>
            <span>GitHub</span>
          </div>
          <button className="icon-btn" onClick={onClose}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M18 6L6 18M6 6l12 12" strokeLinecap="round"/>
            </svg>
          </button>
        </div>

        <div className="panel-body">
          {!connected ? (
            <div className="connect-section">
              <h3>Connect to GitHub</h3>
              <p className="hint">
                Enter your GitHub personal access token to connect repositories.
                <a href="https://github.com/settings/tokens" target="_blank" rel="noopener">
                  Generate token
                </a>
              </p>
              <input
                type="password"
                value={token}
                onChange={e => setToken(e.target.value)}
                placeholder="ghp_..."
                className="text-input"
              />
              {error && <p className="error">{error}</p>}
              <button className="btn btn-primary" onClick={handleConnect}>
                Connect
              </button>
            </div>
          ) : (
            <>
              <div className="tabs">
                <button 
                  className={`tab ${activeTab === 'repos' ? 'active' : ''}`}
                  onClick={() => setActiveTab('repos')}
                >
                  Repos ({repos.length})
                </button>
                <button 
                  className={`tab ${activeTab === 'issues' ? 'active' : ''}`}
                  onClick={() => setActiveTab('issues')}
                >
                  Issues ({issues.length})
                </button>
                <button 
                  className={`tab ${activeTab === 'prs' ? 'active' : ''}`}
                  onClick={() => setActiveTab('prs')}
                >
                  PRs ({prs.length})
                </button>
              </div>

              <div className="tab-content">
                {activeTab === 'repos' && (
                  <div className="repo-list">
                    {repos.map(repo => (
                      <div 
                        key={repo.id} 
                        className={`repo-item ${selectedRepo?.id === repo.id ? 'selected' : ''}`}
                        onClick={() => handleRepoSelect(repo)}
                      >
                        <div className="repo-icon">
                          {repo.private ? '🔒' : '📂'}
                        </div>
                        <div className="repo-info">
                          <div className="repo-name">{repo.full_name}</div>
                          <div className="repo-desc">{repo.description || 'No description'}</div>
                        </div>
                        <div className="repo-stars">⭐ {repo.stargazers_count}</div>
                      </div>
                    ))}
                  </div>
                )}

                {activeTab === 'issues' && selectedRepo && (
                  <div className="issue-list">
                    {issues.length === 0 ? (
                      <p className="empty">No issues found</p>
                    ) : (
                      issues.map(issue => (
                        <div key={issue.id} className="issue-item">
                          <div className="issue-state">{issue.state === 'open' ? '🔴' : '🟢'}</div>
                          <div className="issue-info">
                            <div className="issue-title">{issue.title}</div>
                            <div className="issue-meta">
                              #{issue.number} • {issue.comments} comments
                            </div>
                          </div>
                        </div>
                      ))
                    )}
                  </div>
                )}

                {activeTab === 'prs' && selectedRepo && (
                  <div className="pr-list">
                    {prs.length === 0 ? (
                      <p className="empty">No PRs found</p>
                    ) : (
                      prs.map(pr => (
                        <div key={pr.id} className="pr-item">
                          <div className="pr-state">
                            {pr.merged ? '🟣' : pr.state === 'open' ? '🟢' : '🔴'}
                          </div>
                          <div className="pr-info">
                            <div className="pr-title">{pr.title}</div>
                            <div className="pr-meta">
                              #{pr.number} • {pr.head?.ref} → {pr.base?.ref}
                            </div>
                          </div>
                        </div>
                      ))
                    )}
                  </div>
                )}

                {!selectedRepo && activeTab !== 'repos' && (
                  <p className="empty">Select a repository first</p>
                )}
              </div>

              <button className="btn btn-disconnect" onClick={handleDisconnect}>
                Disconnect
              </button>
            </>
          )}
        </div>
      </div>

      <style>{`
        .panel-overlay {
          position: fixed;
          inset: 0;
          background: rgba(0, 0, 0, 0.85);
          display: flex;
          align-items: center;
          justify-content: center;
          z-index: 1000;
          animation: fadeIn 0.2s ease;
        }

        .panel {
          background: #1a1a1a;
          border: 1px solid #333;
          border-radius: 12px;
          width: 90%;
          max-width: 560px;
          max-height: 90vh;
          overflow: hidden;
          display: flex;
          flex-direction: column;
        }

        .panel-header {
          display: flex;
          justify-content: space-between;
          align-items: center;
          padding: 14px 18px;
          border-bottom: 1px solid #333;
        }

        .header-title {
          display: flex;
          align-items: center;
          gap: 10px;
          font-size: 16px;
          font-weight: 600;
          color: #f5f5f5;
        }

        .header-title svg {
          color: #f5f5f5;
        }

        .panel-body {
          flex: 1;
          overflow-y: auto;
          padding: 18px;
          display: flex;
          flex-direction: column;
        }

        .connect-section h3 {
          font-size: 15px;
          font-weight: 600;
          color: #f5f5f5;
          margin-bottom: 8px;
        }

        .connect-section .hint {
          font-size: 13px;
          color: #888;
          margin-bottom: 16px;
          line-height: 1.5;
        }

        .connect-section .hint a {
          color: #f59e0b;
          text-decoration: none;
        }

        .connect-section .hint a:hover {
          text-decoration: underline;
        }

        .text-input {
          width: 100%;
          padding: 10px 14px;
          background: #252525;
          border: 1px solid #333;
          border-radius: 6px;
          color: #f5f5f5;
          font-size: 14px;
          margin-bottom: 12px;
        }

        .text-input:focus {
          outline: none;
          border-color: #f59e0b;
        }

        .error {
          color: #ef4444;
          font-size: 13px;
          margin-bottom: 12px;
        }

        .btn {
          width: 100%;
          padding: 12px;
          border-radius: 6px;
          font-size: 14px;
          font-weight: 600;
          cursor: pointer;
          transition: all 0.15s ease;
          border: none;
        }

        .btn-primary {
          background: #f59e0b;
          color: #000;
        }

        .btn-primary:hover {
          background: #fbbf24;
        }

        .btn-disconnect {
          background: #dc2626;
          color: #fff;
          margin-top: 16px;
        }

        .btn-disconnect:hover {
          background: #ef4444;
        }

        .tabs {
          display: flex;
          gap: 4px;
          margin-bottom: 16px;
        }

        .tab {
          flex: 1;
          padding: 10px;
          background: #252525;
          border: none;
          border-radius: 6px;
          color: #888;
          font-size: 13px;
          font-weight: 500;
          cursor: pointer;
        }

        .tab.active {
          background: #333;
          color: #f5f5f5;
        }

        .tab-content {
          flex: 1;
          min-height: 200px;
        }

        .repo-list, .issue-list, .pr-list {
          display: flex;
          flex-direction: column;
          gap: 8px;
        }

        .repo-item, .issue-item, .pr-item {
          display: flex;
          align-items: flex-start;
          gap: 12px;
          padding: 12px;
          background: #252525;
          border-radius: 8px;
          cursor: pointer;
          transition: all 0.15s ease;
        }

        .repo-item:hover {
          background: #2a2a2a;
        }

        .repo-item.selected {
          border: 1px solid #f59e0b;
        }

        .repo-icon {
          font-size: 18px;
        }

        .repo-info, .issue-info, .pr-info {
          flex: 1;
        }

        .repo-name, .issue-title, .pr-title {
          font-size: 14px;
          font-weight: 500;
          color: #f5f5f5;
          margin-bottom: 4px;
        }

        .repo-desc, .issue-meta, .pr-meta {
          font-size: 12px;
          color: #888;
        }

        .repo-stars {
          font-size: 13px;
          color: #f59e0b;
        }

        .issue-state, .pr-state {
          font-size: 14px;
        }

        .empty {
          text-align: center;
          color: #666;
          padding: 40px;
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
          .panel {
            width: 100%;
            height: 100%;
            max-height: 100%;
            border-radius: 0;
          }
        }
      `}</style>
    </div>
  )
}

export default GitHubPanel