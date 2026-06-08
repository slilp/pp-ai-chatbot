'use client'

import { useState, useRef, useEffect, useCallback } from 'react'

interface Message {
  role: 'user' | 'assistant'
  content: string
  suggestions?: string[]
  timestamp: string
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
  }
]

function now() {
  return new Date().toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' })
}

const STORAGE_KEY = 'pp-ai-chatbot-history'
const MAX_STORED_MESSAGES = 50

export default function ChatPage() {
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [statusText, setStatusText] = useState('')
  const bottomRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  // Always-current snapshot of messages — lets sendMessage read history without stale closure
  const messagesRef = useRef<Message[]>([])
  useEffect(() => { messagesRef.current = messages }, [messages])

  // restore from localStorage on mount
  useEffect(() => {
    try {
      const saved = localStorage.getItem(STORAGE_KEY)
      if (saved) setMessages(JSON.parse(saved))
    } catch {
      // ignore
    }
  }, [])

  // persist to localStorage whenever messages change
  useEffect(() => {
    if (messages.length === 0) return
    try {
      const toStore = messages.slice(-MAX_STORED_MESSAGES)
      localStorage.setItem(STORAGE_KEY, JSON.stringify(toStore))
    } catch {
      // ignore
    }
  }, [messages])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, loading])

  const clearHistory = useCallback(() => {
    setMessages([])
    try { localStorage.removeItem(STORAGE_KEY) } catch { /* ignore */ }
  }, [])

  const sendMessage = useCallback(async (question: string) => {
    const q = question.trim()
    if (!q || loading) return

    setMessages(prev => [...prev, { role: 'user', content: q, timestamp: now() }])
    setInput('')
    setLoading(true)

    // Add empty assistant message to fill in as tokens arrive
    setMessages(prev => [...prev, { role: 'assistant', content: '', timestamp: now() }])
    setStatusText('Understanding your question...')

    try {
      // build history from completed turns using ref to avoid stale closure
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
        const lines = chunk.split('\n')

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          const payload = line.slice(6).trim()
          if (payload === '[DONE]' || payload === '') continue
          if (payload.startsWith('[ERROR]')) {
            setStatusText('')
            setMessages(prev => {
              const updated = [...prev]
              updated[updated.length - 1] = {
                ...updated[updated.length - 1],
                content: 'เกิดข้อผิดพลาด: ' + payload.slice(8),
              }
              return updated
            })
            break
          }
          try {
            const parsed = JSON.parse(payload)
            // Status event from the orchestrator
            if (parsed && typeof parsed === 'object' && parsed.type === 'status') {
              setStatusText((parsed as StatusEvent).message)
              continue
            }
            // Text token
            if (typeof parsed === 'string') {
              setStatusText('')
              setLoading(false)
              setMessages(prev => {
                const updated = [...prev]
                updated[updated.length - 1] = {
                  ...updated[updated.length - 1],
                  content: updated[updated.length - 1].content + parsed,
                }
                return updated
              })
            }
          } catch {
            // skip malformed chunk
          }
        }
      }
    } catch {
      setMessages(prev => {
        const updated = [...prev]
        updated[updated.length - 1] = {
          ...updated[updated.length - 1],
          content: 'ไม่สามารถเชื่อมต่อ log service ได้ กรุณาตรวจสอบว่า AI service กำลังทำงานที่ port 8080',
        }
        return updated
      })
    } finally {
      setLoading(false)
      setStatusText('')
      inputRef.current?.focus()
    }
  }, [loading])

  function handleKeyDown(e: KeyboardEvent) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      sendMessage(input)
    }
  }

  return (
    <div className="flex flex-col flex-1 overflow-hidden bg-[#f3f2ef]">

      {/* ── Header ── */}
      <header className="shrink-0 bg-white border-b border-[#e0ddd8] px-6 py-3 flex items-center justify-between shadow-sm">
        <div className="flex items-center gap-3">
          <div className="flex items-center justify-center w-9 h-9 rounded-lg bg-[#0a66c2] text-white font-bold text-base shadow">
            A          
          </div>
          <div>
            <h1 className="font-semibold text-[#1c1c1c] text-sm leading-tight">Payment Log AI</h1>
            <p className="text-[#666] text-xs">Powered by KuranasakiRTX</p>
          </div>
        </div>
        <div className="flex items-center gap-3">
          {messages.length > 0 && (
            <button
              onClick={clearHistory}
              className="text-xs text-[#666] hover:text-[#c00] border border-[#e0ddd8] hover:border-[#c00] px-3 py-1.5 rounded-full transition-colors"
            >
              Clear chat
            </button>
          )}
          <div className="flex items-center gap-2 text-xs font-medium text-[#057642] bg-[#e7f3ec] border border-[#c3dfd0] px-3 py-1.5 rounded-full">
            <span className="w-1.5 h-1.5 rounded-full bg-[#057642] animate-pulse" />
            Connected
          </div>
        </div>
      </header>

      {/* ── Messages ── */}
      <div className="flex-1 overflow-y-auto chat-scroll px-4 py-6">
        <div className="max-w-3xl mx-auto space-y-5">

          {messages.length === 0 ? (
            <div className="flex flex-col items-center justify-center min-h-[60vh] gap-8 text-center">
              <div className="space-y-3">
                <div className="flex items-center justify-center w-16 h-16 rounded-2xl bg-[#e8f0f9] border border-[#c5d9f0] mx-auto text-3xl">
                  🔍
                </div>
                <h2 className="text-2xl font-bold text-[#1c1c1c]">How can I help you?</h2>
                <p className="text-[#666] text-sm max-w-sm">
                  Ask me anything about payment platform logs — errors, transaction traces, or root cause analysis.
                  Limited to UAT data on 8 June 2026 only
                </p>
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 w-full max-w-2xl">
                {EXAMPLES.map(ex => (
                  <button
                    key={ex.title}
                    onClick={() => sendMessage(ex.query)}
                    className="group flex flex-col gap-2 p-4 rounded-xl bg-white border border-[#e0ddd8] hover:border-[#0a66c2] hover:shadow-md transition-all text-left shadow-sm"
                  >
                    <span className="text-xs font-semibold text-[#0a66c2] group-hover:text-[#004182] uppercase tracking-wide">
                      {ex.title}
                    </span>
                    <span className="text-sm text-[#444] leading-snug">{ex.desc}</span>
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
                    : 'bg-white border border-[#e0ddd8] text-[#0a66c2]'
                }`}>
                  {msg.role === 'user' ? 'You' : 'AI'}
                </div>

                {/* Bubble */}
                <div className={`flex flex-col gap-1 max-w-[75%] ${msg.role === 'user' ? 'items-end' : 'items-start'}`}>
                  <div className={`rounded-2xl px-4 py-3 text-sm leading-relaxed shadow-sm ${
                    msg.role === 'user'
                      ? 'bg-[#0a66c2] text-white rounded-tr-sm'
                      : 'bg-white border border-[#e0ddd8] text-[#1c1c1c] rounded-tl-sm'
                  }`}>
                    <p className="whitespace-pre-wrap">{msg.content}</p>

                    {msg.suggestions && msg.suggestions.length > 0 && (
                      <div className="mt-3 pt-3 border-t border-[#e0ddd8] space-y-1.5">
                        <p className="text-xs font-semibold text-[#666] uppercase tracking-wide">Suggested follow-ups</p>
                        <div className="flex flex-col gap-1.5">
                          {msg.suggestions.map((s, j) => (
                            <button
                              key={j}
                              onClick={() => sendMessage(s)}
                              className="text-xs text-[#0a66c2] hover:text-[#004182] bg-[#e8f0f9] hover:bg-[#d0e5f7] border border-[#c5d9f0] hover:border-[#0a66c2] rounded-lg px-3 py-2 text-left transition-all font-medium"
                            >
                              {s}
                            </button>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                  <span className="text-xs text-[#999] px-1">{msg.timestamp}</span>
                </div>

              </div>
            ))
          )}

          {/* Loading indicator — shown while orchestrator/analyzer are working */}
          {(loading || statusText) && (
            <div className="flex gap-3 animate-fade-in-up">
              <div className="shrink-0 flex items-center justify-center w-9 h-9 rounded-full bg-white border border-[#e0ddd8] text-[#0a66c2] text-xs font-bold shadow-sm">
                AI
              </div>
              <div className="bg-white border border-[#e0ddd8] rounded-2xl rounded-tl-sm px-4 py-3 shadow-sm">
                <div className="flex items-center gap-2.5">
                  <div className="flex gap-1">
                    {[0, 150, 300].map(d => (
                      <span key={d} className="w-2 h-2 rounded-full bg-[#0a66c2] animate-bounce" style={{ animationDelay: `${d}ms` }} />
                    ))}
                  </div>
                  <span className="text-xs text-[#666]">{statusText || 'Analyzing logs…'}</span>
                </div>
              </div>
            </div>
          )}

          <div ref={bottomRef} />
        </div>
      </div>

      {/* ── Input ── */}
      <div className="shrink-0 bg-white border-t border-[#e0ddd8] px-4 pt-3 pb-4 shadow-[0_-1px_4px_rgba(0,0,0,0.06)]">
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
              onKeyDown={handleKeyDown as unknown as React.KeyboardEventHandler<HTMLTextAreaElement>}
              placeholder="Ask about payment logs, errors, or transaction traces…"
              disabled={loading}
              className="flex-1 resize-none bg-[#f3f2ef] border border-[#d0ccc7] focus:border-[#0a66c2] focus:bg-white placeholder-[#999] text-[#1c1c1c] rounded-xl px-4 py-3 text-sm outline-none transition-all disabled:opacity-50 leading-relaxed"
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
          <p className="text-xs text-[#aaa] text-center">
            Press{' '}
            <kbd className="px-1.5 py-0.5 rounded bg-[#f3f2ef] border border-[#d0ccc7] text-[#666] text-xs font-mono">
              Enter
            </kbd>{' '}
            to send ·{' '}
            <kbd className="px-1.5 py-0.5 rounded bg-[#f3f2ef] border border-[#d0ccc7] text-[#666] text-xs font-mono">
              Shift+Enter
            </kbd>{' '}
            for new line
          </p>
        </div>
      </div>

    </div>
  )
}
