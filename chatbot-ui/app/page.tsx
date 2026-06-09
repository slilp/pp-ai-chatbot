'use client'

import { useState, useRef, useEffect, useCallback, KeyboardEvent } from 'react'

interface Message {
  role: 'user' | 'assistant'
  content: string
  suggestions?: string[]
  timestamp: string
}

interface Session {
  id: string
  name: string
  createdAt: string
  updatedAt: string
  messages: Message[]
}

interface StatusEvent {
  type: 'status'
  message: string
}

const EXAMPLES = [
  {
    title: 'Error Lookup',
    desc: 'I want error on 2026-06-08',
    query: 'I want error on 2026-06-08',
  },
  {
    title: 'AIS related error',
    desc: 'ค้นหา error ที่เกี่ยวกับ AIS ในวันที่ 8 มิถุนายน 2026',
    query: 'ค้นหา error ที่เกี่ยวกับ AIS ในวันที่ 8 มิถุนายน 2026',
  },
]

const SESSIONS_KEY = 'pp-ai-sessions'
const ACTIVE_KEY = 'pp-ai-active'
const THEME_KEY = 'pp-ai-theme'
const MAX_STORED_MESSAGES = 50

function nowTime() {
  return new Date().toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' })
}

function genId() {
  return Date.now().toString(36) + Math.random().toString(36).slice(2)
}

function createSession(): Session {
  return {
    id: genId(),
    name: 'New Chat',
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
    messages: [],
  }
}

export default function ChatPage() {
  const [sessions, setSessions] = useState<Session[]>([])
  const [activeId, setActiveId] = useState<string | null>(null)
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [statusText, setStatusText] = useState('')
  const [sidebarOpen, setSidebarOpen] = useState(true)
  const [dark, setDark] = useState(false)
  const [renamingId, setRenamingId] = useState<string | null>(null)
  const [renameValue, setRenameValue] = useState('')
  const [hydrated, setHydrated] = useState(false)

  const bottomRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const messagesRef = useRef<Message[]>([])
  const renameInputRef = useRef<HTMLInputElement>(null)
  // Stable ref so sendMessage always sees current activeId without re-creating
  const activeIdRef = useRef<string | null>(null)
  useEffect(() => { activeIdRef.current = activeId }, [activeId])

  // ── Initialise from localStorage ────────────────────────────────────────────
  useEffect(() => {
    try {
      const theme = localStorage.getItem(THEME_KEY)
      if (theme === 'dark') setDark(true)

      const raw = localStorage.getItem(SESSIONS_KEY)
      const savedSessions: Session[] = raw ? JSON.parse(raw) : []
      const savedActiveId = localStorage.getItem(ACTIVE_KEY)

      if (savedSessions.length > 0) {
        setSessions(savedSessions)
        const target = savedSessions.find(s => s.id === savedActiveId) ?? savedSessions[0]
        setActiveId(target.id)
      } else {
        const initial = createSession()
        setSessions([initial])
        setActiveId(initial.id)
      }
    } catch {
      const initial = createSession()
      setSessions([initial])
      setActiveId(initial.id)
    }
    setHydrated(true)
  }, [])

  // ── Persist sessions ─────────────────────────────────────────────────────────
  useEffect(() => {
    if (!hydrated) return
    try { localStorage.setItem(SESSIONS_KEY, JSON.stringify(sessions)) } catch { /* ignore */ }
  }, [sessions, hydrated])

  useEffect(() => {
    if (!hydrated || !activeId) return
    try { localStorage.setItem(ACTIVE_KEY, activeId) } catch { /* ignore */ }
  }, [activeId, hydrated])

  // ── Dark mode ────────────────────────────────────────────────────────────────
  useEffect(() => {
    if (!hydrated) return
    document.documentElement.classList.toggle('dark', dark)
    try { localStorage.setItem(THEME_KEY, dark ? 'dark' : 'light') } catch { /* ignore */ }
  }, [dark, hydrated])

  // ── Derived state ────────────────────────────────────────────────────────────
  const activeSession = sessions.find(s => s.id === activeId) ?? null
  const messages = activeSession?.messages ?? []
  useEffect(() => { messagesRef.current = messages }, [messages])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, loading])

  // ── Fix activeId when sessions change (e.g. after delete) ────────────────────
  useEffect(() => {
    if (!hydrated || sessions.length === 0) return
    if (!activeId || !sessions.find(s => s.id === activeId)) {
      setActiveId(sessions[0].id)
    }
  }, [sessions, activeId, hydrated])

  // ── Focus rename input when editing ─────────────────────────────────────────
  useEffect(() => {
    if (renamingId) setTimeout(() => renameInputRef.current?.focus(), 50)
  }, [renamingId])

  // ── Session CRUD ──────────────────────────────────────────────────────────────
  const handleNewSession = useCallback(() => {
    const s = createSession()
    setSessions(prev => [s, ...prev])
    setActiveId(s.id)
    setInput('')
  }, [])

  const handleDeleteSession = useCallback((id: string) => {
    setSessions(prev => {
      const next = prev.filter(s => s.id !== id)
      return next.length > 0 ? next : [createSession()]
    })
    // activeId correction is handled by the useEffect above
  }, [])

  const startRename = useCallback((id: string, name: string) => {
    setRenamingId(id)
    setRenameValue(name)
  }, [])

  const commitRename = useCallback(() => {
    if (!renamingId) return
    const trimmed = renameValue.trim() || 'New Chat'
    setSessions(prev => prev.map(s => s.id === renamingId ? { ...s, name: trimmed } : s))
    setRenamingId(null)
  }, [renamingId, renameValue])

  // ── Stable message updater — uses ref so sendMessage doesn't go stale ────────
  const updateActiveMessages = useCallback((updater: (prev: Message[]) => Message[]) => {
    const aid = activeIdRef.current
    if (!aid) return
    setSessions(prev => prev.map(s => {
      if (s.id !== aid) return s
      const next = updater(s.messages).slice(-MAX_STORED_MESSAGES)
      // Auto-name session from first user message
      const name = s.name === 'New Chat' && next[0]?.role === 'user'
        ? next[0].content.slice(0, 50)
        : s.name
      return { ...s, messages: next, name, updatedAt: new Date().toISOString() }
    }))
  }, [])

  // ── Send message ─────────────────────────────────────────────────────────────
  const sendMessage = useCallback(async (question: string) => {
    const q = question.trim()
    if (!q || loading) return

    updateActiveMessages(prev => [...prev, { role: 'user', content: q, timestamp: nowTime() }])
    setInput('')
    setLoading(true)
    updateActiveMessages(prev => [...prev, { role: 'assistant', content: '', timestamp: nowTime() }])
    setStatusText('Understanding your question...')

    try {
      const history = messagesRef.current
        .filter(m => m.content)
        .map(m => ({ role: m.role, content: m.content }))

      const res = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: q, history }),
      })

      if (!res.body) throw new Error('No response body')

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      setLoading(false)

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        const chunk = decoder.decode(value, { stream: true })
        for (const line of chunk.split('\n')) {
          if (!line.startsWith('data: ')) continue
          const payload = line.slice(6).trim()
          if (payload === '[DONE]' || payload === '') continue
          if (payload.startsWith('[ERROR]')) {
            setStatusText('')
            updateActiveMessages(prev => {
              const updated = [...prev]
              updated[updated.length - 1] = { ...updated[updated.length - 1], content: 'เกิดข้อผิดพลาด: ' + payload.slice(8) }
              return updated
            })
            break
          }
          try {
            const parsed = JSON.parse(payload)
            if (parsed && typeof parsed === 'object' && parsed.type === 'status') {
              setStatusText((parsed as StatusEvent).message)
              continue
            }
            if (typeof parsed === 'string') {
              setStatusText('')
              setLoading(false)
              updateActiveMessages(prev => {
                const updated = [...prev]
                updated[updated.length - 1] = { ...updated[updated.length - 1], content: updated[updated.length - 1].content + parsed }
                return updated
              })
            }
          } catch { /* skip malformed */ }
        }
      }
    } catch {
      updateActiveMessages(prev => {
        const updated = [...prev]
        updated[updated.length - 1] = { ...updated[updated.length - 1], content: 'ไม่สามารถเชื่อมต่อ log service ได้ กรุณาตรวจสอบว่า AI service กำลังทำงานที่ port 8080' }
        return updated
      })
    } finally {
      setLoading(false)
      setStatusText('')
      inputRef.current?.focus()
    }
  }, [loading, updateActiveMessages])

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      sendMessage(input)
    }
  }

  if (!hydrated) return null

  return (
    <div className="flex h-full overflow-hidden bg-[#f3f2ef] dark:bg-[#0f0f1a]">

      {/* ── Sidebar ── */}
      <aside className={`shrink-0 flex flex-col transition-[width] duration-200 overflow-hidden bg-[#1a1a2e] dark:bg-[#0d0d1a] border-r border-[#2d2d4a] ${sidebarOpen ? 'w-64' : 'w-0'}`}>
        <div className="flex flex-col h-full w-64">

          {/* Sidebar header */}
          <div className="flex items-center px-4 py-3 border-b border-[#2d2d4a]">
            <span className="text-xs font-semibold text-[#8888aa] uppercase tracking-wider">Chat History</span>
          </div>

          {/* New chat */}
          <div className="px-3 py-2.5">
            <button
              onClick={handleNewSession}
              className="w-full flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium text-white bg-[#0a66c2] hover:bg-[#004182] transition-colors"
            >
              <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4 shrink-0">
                <path d="M10.75 4.75a.75.75 0 00-1.5 0v4.5h-4.5a.75.75 0 000 1.5h4.5v4.5a.75.75 0 001.5 0v-4.5h4.5a.75.75 0 000-1.5h-4.5v-4.5z" />
              </svg>
              New Chat
            </button>
          </div>

          {/* Sessions list */}
          <div className="flex-1 overflow-y-auto chat-scroll px-2 pb-2 space-y-0.5">
            {sessions.map(s => (
              <div
                key={s.id}
                className={`group relative flex items-center gap-1 rounded-lg px-2 py-2 cursor-pointer transition-colors ${
                  s.id === activeId
                    ? 'bg-[#2d2d4a] text-white'
                    : 'text-[#aaaacc] hover:bg-[#1e1e3a] hover:text-white'
                }`}
              >
                {renamingId === s.id ? (
                  <input
                    ref={renameInputRef}
                    value={renameValue}
                    onChange={e => setRenameValue(e.target.value)}
                    onBlur={commitRename}
                    onKeyDown={e => {
                      if (e.key === 'Enter') commitRename()
                      if (e.key === 'Escape') setRenamingId(null)
                    }}
                    className="flex-1 min-w-0 bg-[#3d3d5a] text-white text-xs rounded px-2 py-0.5 outline-none border border-[#0a66c2]"
                  />
                ) : (
                  <>
                    <span
                      onClick={() => setActiveId(s.id)}
                      className="flex-1 min-w-0 text-xs truncate select-none"
                    >
                      {s.name}
                    </span>
                    <div className="shrink-0 flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
                      <button
                        onClick={e => { e.stopPropagation(); startRename(s.id, s.name) }}
                        title="Rename"
                        className="p-1 rounded hover:bg-[#3d3d5a] text-[#8888aa] hover:text-white transition-colors"
                      >
                        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" fill="currentColor" className="w-3 h-3">
                          <path d="M13.488 2.513a1.75 1.75 0 00-2.475 0L6.75 6.774a2.75 2.75 0 00-.596.892l-.848 2.047a.75.75 0 00.98.98l2.047-.848a2.75 2.75 0 00.892-.596l4.262-4.262a1.75 1.75 0 000-2.474z" />
                          <path d="M4.75 7.5A.75.75 0 004 8.25v1a.75.75 0 00.75.75h1a.75.75 0 000-1.5H5V8.25A.75.75 0 004.75 7.5zM3.5 3.75A.75.75 0 014.25 3h3a.75.75 0 010 1.5H5v7h7v-2.25a.75.75 0 011.5 0v2.25c0 .966-.784 1.75-1.75 1.75h-7A1.75 1.75 0 013 12.25v-8.5c0-.966.784-1.75 1.75-1.75z" />
                        </svg>
                      </button>
                      <button
                        onClick={e => { e.stopPropagation(); handleDeleteSession(s.id) }}
                        title="Delete"
                        className="p-1 rounded hover:bg-[#3d3d5a] text-[#8888aa] hover:text-red-400 transition-colors"
                      >
                        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" fill="currentColor" className="w-3 h-3">
                          <path fillRule="evenodd" d="M5 3.25V4H2.75a.75.75 0 000 1.5h.3l.815 8.15A1.5 1.5 0 005.357 15h5.285a1.5 1.5 0 001.493-1.35l.815-8.15h.3a.75.75 0 000-1.5H11v-.75A2.25 2.25 0 008.75 1h-1.5A2.25 2.25 0 005 3.25zm2.25-.75a.75.75 0 00-.75.75V4h3v-.75a.75.75 0 00-.75-.75h-1.5zM6.05 6a.75.75 0 01.787.713l.275 5.5a.75.75 0 01-1.498.075l-.275-5.5A.75.75 0 016.05 6zm3.9 0a.75.75 0 01.712.787l-.275 5.5a.75.75 0 01-1.498-.075l.275-5.5a.75.75 0 01.786-.712z" clipRule="evenodd" />
                        </svg>
                      </button>
                    </div>
                  </>
                )}
              </div>
            ))}
          </div>

        </div>
      </aside>

      {/* ── Main area ── */}
      <div className="flex flex-col flex-1 overflow-hidden">

        {/* ── Header ── */}
        <header className="shrink-0 bg-white dark:bg-[#16162a] border-b border-[#e0ddd8] dark:border-[#2d2d4a] px-4 py-3 flex items-center justify-between shadow-sm">
          <div className="flex items-center gap-3">
            {/* Sidebar toggle */}
            <button
              onClick={() => setSidebarOpen(v => !v)}
              className="flex items-center justify-center w-8 h-8 rounded-lg hover:bg-[#f3f2ef] dark:hover:bg-[#2d2d4a] text-[#666] dark:text-[#aaaacc] transition-colors"
              title={sidebarOpen ? 'Hide sidebar' : 'Show sidebar'}
            >
              <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor" className="w-5 h-5">
                <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6.75h16.5M3.75 12h16.5m-16.5 5.25h16.5" />
              </svg>
            </button>

            <div className="flex items-center justify-center w-9 h-9 rounded-lg bg-[#0a66c2] text-white font-bold text-base shadow">
              A
            </div>
            <div>
              <h1 className="font-semibold text-[#1c1c1c] dark:text-white text-sm leading-tight">Payment Log AI</h1>
              <p className="text-[#666] dark:text-[#8888aa] text-xs">Powered by PaymentPlatform</p>
            </div>
          </div>

          <div className="flex items-center gap-2">
            {/* Dark mode toggle */}
            <button
              onClick={() => setDark(v => !v)}
              title={dark ? 'Switch to light mode' : 'Switch to dark mode'}
              className="flex items-center justify-center w-8 h-8 rounded-full hover:bg-[#f3f2ef] dark:hover:bg-[#2d2d4a] text-[#666] dark:text-[#aaaacc] transition-colors"
            >
              {dark ? (
                /* Sun icon */
                <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4">
                  <path d="M10 2a.75.75 0 01.75.75v1.5a.75.75 0 01-1.5 0v-1.5A.75.75 0 0110 2zM10 15a.75.75 0 01.75.75v1.5a.75.75 0 01-1.5 0v-1.5A.75.75 0 0110 15zM10 7a3 3 0 100 6 3 3 0 000-6zM15.657 5.404a.75.75 0 10-1.06-1.06l-1.061 1.06a.75.75 0 001.06 1.06l1.06-1.06zM6.464 14.596a.75.75 0 10-1.06-1.06l-1.06 1.06a.75.75 0 001.06 1.06l1.06-1.06zM18 10a.75.75 0 01-.75.75h-1.5a.75.75 0 010-1.5h1.5A.75.75 0 0118 10zM5 10a.75.75 0 01-.75.75h-1.5a.75.75 0 010-1.5h1.5A.75.75 0 015 10zM14.596 15.657a.75.75 0 001.06-1.06l-1.06-1.061a.75.75 0 10-1.06 1.06l1.06 1.06zM5.404 6.464a.75.75 0 001.06-1.06L5.403 4.343a.75.75 0 00-1.06 1.06l1.06 1.061z" />
                </svg>
              ) : (
                /* Moon icon */
                <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4">
                  <path fillRule="evenodd" d="M7.455 2.004a.75.75 0 01.26.77 7 7 0 009.958 7.967.75.75 0 011.067.853A8.5 8.5 0 116.647 1.921a.75.75 0 01.808.083z" clipRule="evenodd" />
                </svg>
              )}
            </button>

            <div className="flex items-center gap-2 text-xs font-medium text-[#057642] bg-[#e7f3ec] border border-[#c3dfd0] px-3 py-1.5 rounded-full">
              <span className="w-1.5 h-1.5 rounded-full bg-[#057642] animate-pulse" />
              Connected
            </div>
          </div>
        </header>

        {/* ── Messages ── */}
        <div className="flex-1 overflow-y-auto chat-scroll px-4 py-6 bg-[#f3f2ef] dark:bg-[#0f0f1a]">
          <div className="max-w-3xl mx-auto space-y-5">

            {messages.length === 0 ? (
              <div className="flex flex-col items-center justify-center min-h-[60vh] gap-8 text-center">
                <div className="space-y-3">
                  <div className="flex items-center justify-center w-16 h-16 rounded-2xl bg-[#e8f0f9] dark:bg-[#1e2d4a] border border-[#c5d9f0] dark:border-[#2d4a6a] mx-auto text-3xl">
                    🔍
                  </div>
                  <h2 className="text-2xl font-bold text-[#1c1c1c] dark:text-white">How can I help you?</h2>
                  <p className="text-[#666] dark:text-[#8888aa] text-sm max-w-sm">
                    Ask me anything about payment platform logs — errors, transaction traces, or root cause analysis.
                    Limited to UAT data on 8 June 2026 only
                  </p>
                </div>

                <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 w-full max-w-2xl">
                  {EXAMPLES.map(ex => (
                    <button
                      key={ex.title}
                      onClick={() => sendMessage(ex.query)}
                      className="group flex flex-col gap-2 p-4 rounded-xl bg-white dark:bg-[#16162a] border border-[#e0ddd8] dark:border-[#2d2d4a] hover:border-[#0a66c2] hover:shadow-md transition-all text-left shadow-sm"
                    >
                      <span className="text-xs font-semibold text-[#0a66c2] group-hover:text-[#004182] uppercase tracking-wide">
                        {ex.title}
                      </span>
                      <span className="text-sm text-[#444] dark:text-[#aaaacc] leading-snug">{ex.desc}</span>
                    </button>
                  ))}
                </div>
              </div>
            ) : (
              messages.map((msg, i) => msg.role === 'assistant' && !msg.content ? null : (
                <div key={i} className={`flex gap-3 animate-fade-in-up ${msg.role === 'user' ? 'flex-row-reverse' : 'flex-row'}`}>

                  {/* Avatar */}
                  <div className={`shrink-0 flex items-center justify-center w-9 h-9 rounded-full text-xs font-bold shadow-sm ${
                    msg.role === 'user'
                      ? 'bg-[#0a66c2] text-white'
                      : 'bg-white dark:bg-[#16162a] border border-[#e0ddd8] dark:border-[#2d2d4a] text-[#0a66c2]'
                  }`}>
                    {msg.role === 'user' ? 'You' : 'AI'}
                  </div>

                  {/* Bubble */}
                  <div className={`flex flex-col gap-1 max-w-[75%] ${msg.role === 'user' ? 'items-end' : 'items-start'}`}>
                    <div className={`rounded-2xl px-4 py-3 text-sm leading-relaxed shadow-sm ${
                      msg.role === 'user'
                        ? 'bg-[#0a66c2] text-white rounded-tr-sm'
                        : 'bg-white dark:bg-[#16162a] border border-[#e0ddd8] dark:border-[#2d2d4a] text-[#1c1c1c] dark:text-[#e0e0f0] rounded-tl-sm'
                    }`}>
                      <p className="whitespace-pre-wrap">{msg.content}</p>

                      {msg.suggestions && msg.suggestions.length > 0 && (
                        <div className="mt-3 pt-3 border-t border-[#e0ddd8] dark:border-[#2d2d4a] space-y-1.5">
                          <p className="text-xs font-semibold text-[#666] dark:text-[#8888aa] uppercase tracking-wide">Suggested follow-ups</p>
                          <div className="flex flex-col gap-1.5">
                            {msg.suggestions.map((s, j) => (
                              <button
                                key={j}
                                onClick={() => sendMessage(s)}
                                className="text-xs text-[#0a66c2] hover:text-[#004182] bg-[#e8f0f9] dark:bg-[#1e2d4a] hover:bg-[#d0e5f7] dark:hover:bg-[#2d3d5a] border border-[#c5d9f0] dark:border-[#2d4a6a] hover:border-[#0a66c2] rounded-lg px-3 py-2 text-left transition-all font-medium"
                              >
                                {s}
                              </button>
                            ))}
                          </div>
                        </div>
                      )}
                    </div>
                    <span className="text-xs text-[#999] dark:text-[#555] px-1">{msg.timestamp}</span>
                  </div>

                </div>
              ))
            )}

            {/* Loading indicator */}
            {(loading || statusText) && (
              <div className="flex gap-3 animate-fade-in-up">
                <div className="shrink-0 flex items-center justify-center w-9 h-9 rounded-full bg-white dark:bg-[#16162a] border border-[#e0ddd8] dark:border-[#2d2d4a] text-[#0a66c2] text-xs font-bold shadow-sm">
                  AI
                </div>
                <div className="bg-white dark:bg-[#16162a] border border-[#e0ddd8] dark:border-[#2d2d4a] rounded-2xl rounded-tl-sm px-4 py-3 shadow-sm">
                  <div className="flex items-center gap-2.5">
                    <div className="flex gap-1">
                      {[0, 150, 300].map(d => (
                        <span key={d} className="w-2 h-2 rounded-full bg-[#0a66c2] animate-bounce" style={{ animationDelay: `${d}ms` }} />
                      ))}
                    </div>
                    <span className="text-xs text-[#666] dark:text-[#8888aa]">{statusText || 'Analyzing logs…'}</span>
                  </div>
                </div>
              </div>
            )}

            <div ref={bottomRef} />
          </div>
        </div>

        {/* ── Input ── */}
        <div className="shrink-0 bg-white dark:bg-[#16162a] border-t border-[#e0ddd8] dark:border-[#2d2d4a] px-4 pt-3 pb-4 shadow-[0_-1px_4px_rgba(0,0,0,0.06)]">
          <div className="max-w-3xl mx-auto space-y-2">
            <div className="flex gap-3 items-end">
              <textarea
                ref={inputRef}
                rows={1}
                value={input}
                onChange={e => {
                  setInput(e.target.value)
                  e.target.style.height = 'auto'
                  e.target.style.height = Math.min(e.target.scrollHeight, 120) + 'px'
                }}
                onKeyDown={handleKeyDown}
                placeholder="Ask about payment logs, errors, or transaction traces…"
                disabled={loading}
                className="flex-1 resize-none bg-[#f3f2ef] dark:bg-[#0f0f1a] border border-[#d0ccc7] dark:border-[#2d2d4a] focus:border-[#0a66c2] focus:bg-white dark:focus:bg-[#1a1a2e] placeholder-[#999] dark:placeholder-[#555] text-[#1c1c1c] dark:text-[#e0e0f0] rounded-xl px-4 py-3 text-sm outline-none transition-all disabled:opacity-50 leading-relaxed"
                style={{ minHeight: '48px' }}
              />
              <button
                onClick={() => sendMessage(input)}
                disabled={loading || !input.trim()}
                className="shrink-0 flex items-center justify-center w-12 h-12 rounded-xl bg-[#0a66c2] hover:bg-[#004182] disabled:opacity-40 disabled:cursor-not-allowed text-white transition-colors shadow-md"
              >
                <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" className="w-5 h-5">
                  <path d="M3.105 2.289a.75.75 0 00-.826.95l1.414 4.925A1.5 1.5 0 005.135 9.25h6.115a.75.75 0 010 1.5H5.135a1.5 1.5 0 00-1.442 1.086l-1.414 4.926a.75.75 0 00.826.95 28.896 28.896 0 0015.293-7.154.75.75 0 000-1.115A28.897 28.897 0 003.105 2.289z" />
                </svg>
              </button>
            </div>
            <p className="text-xs text-[#aaa] dark:text-[#555] text-center">
              Press{' '}
              <kbd className="px-1.5 py-0.5 rounded bg-[#f3f2ef] dark:bg-[#0f0f1a] border border-[#d0ccc7] dark:border-[#2d2d4a] text-[#666] dark:text-[#8888aa] text-xs font-mono">
                Enter
              </kbd>{' '}
              to send ·{' '}
              <kbd className="px-1.5 py-0.5 rounded bg-[#f3f2ef] dark:bg-[#0f0f1a] border border-[#d0ccc7] dark:border-[#2d2d4a] text-[#666] dark:text-[#8888aa] text-xs font-mono">
                Shift+Enter
              </kbd>{' '}
              for new line
            </p>
          </div>
        </div>

      </div>
    </div>
  )
}
