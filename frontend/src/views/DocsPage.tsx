import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams } from '@tanstack/react-router'
import { DOCS, DOC_GROUPS, DOCS_BY_SLUG, editUrl, issueUrl, prevNext } from '../docs/manifest'
import type { DocEntry } from '../docs/manifest'
import { getDocSource } from '../docs/content'
import { extractToc, highlightSegments, searchDocs } from '../docs/search'
import type { TocItem } from '../docs/search'
import { DocMarkdown } from '../components/DocMarkdown'
import { IconThumbDown, IconThumbUp } from '../components/icons'
import { useDocumentTitle } from '../hooks/useDocumentTitle'
import '../styles/docs.css'

function DocsTopbar({ navOpen, onMenuToggle }: { navOpen: boolean; onMenuToggle: () => void }) {
  return (
    <header className="docs-topbar">
      <button
        type="button"
        className="docs-menu-btn"
        aria-label={navOpen ? '关闭导航' : '打开导航'}
        aria-expanded={navOpen}
        aria-controls="docs-sidebar"
        onClick={onMenuToggle}
      >
        <span aria-hidden="true">{navOpen ? '✕' : '☰'}</span>
      </button>
      <Link to="/" className="brand" aria-label="ForgeFlow 首页">
        <span className="brand-mark" />
        <span className="brand-name">ForgeFlow</span>
        <span className="docs-tag">文档</span>
      </Link>
      <span className="docs-version" title="文档版本">v0.1.0</span>
      <nav aria-label="站点导航">
        <a href="/">落地页</a>
        <a href="/console">控制台</a>
        <Link to="/architecture">架构</Link>
        <a href="/api/docs">API 参考 ↗</a>
        <a href="https://github.com/JoelJohnsonThomas/forgeflow" target="_blank" rel="noopener noreferrer">GitHub ↗</a>
      </nav>
    </header>
  )
}

function Highlighted({ text, query }: { text: string; query: string }) {
  return (
    <>
      {highlightSegments(text, query).map((seg, i) =>
        seg.match ? <mark key={i}>{seg.text}</mark> : <span key={i}>{seg.text}</span>,
      )}
    </>
  )
}

function DocsSidebar({
  active,
  open,
  onNavigate,
}: {
  active?: string
  open?: boolean
  onNavigate?: () => void
}) {
  const [q, setQ] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)
  const query = q.trim()
  const hits = useMemo(() => (query ? searchDocs(query) : []), [query])

  // "/" or Ctrl/Cmd+K focuses search from anywhere on the page.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null
      const typing = t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)
      if ((e.key === '/' && !typing) || (e.key.toLowerCase() === 'k' && (e.ctrlKey || e.metaKey))) {
        e.preventDefault()
        inputRef.current?.focus()
        inputRef.current?.select()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  return (
    <aside id="docs-sidebar" className={`docs-sidebar${open ? ' open' : ''}`} aria-label="文档">
      <label className="docs-search">
        <span className="sr-only">搜索文档</span>
        <input
          ref={inputRef}
          type="search"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Escape' && q) {
              e.stopPropagation()
              setQ('')
            }
          }}
          placeholder="搜索文档…"
          aria-label="搜索文档"
        />
        <kbd className="docs-search-kbd" aria-hidden="true">/</kbd>
      </label>

      {query ? (
        <div className="docs-results" role="region" aria-label="搜索结果">
          <p className="docs-results-count" role="status">
            {hits.length === 0 ? `没有匹配「${query}」的页面。` : `${hits.length} 条结果`}
          </p>
          {hits.map((h) => (
            <Link
              key={h.entry.slug}
              to="/docs/$slug"
              params={{ slug: h.entry.slug }}
              hash={h.heading?.id}
              className="docs-result"
              onClick={() => {
                setQ('')
                onNavigate?.()
              }}
            >
              <span className="docs-result-title">
                <Highlighted text={h.entry.title} query={query} />
              </span>
              {h.heading && (
                <span className="docs-result-heading">
                  § <Highlighted text={h.heading.text} query={query} />
                </span>
              )}
              {h.snippet && (
                <span className="docs-result-snippet">
                  <Highlighted text={h.snippet} query={query} />
                </span>
              )}
            </Link>
          ))}
        </div>
      ) : (
        DOC_GROUPS.map((group) => (
          <div className="docs-nav-group" key={group}>
            <div className="docs-nav-title">{group}</div>
            {DOCS.filter((d) => d.group === group).map((d) => (
              <Link
                key={d.slug}
                to="/docs/$slug"
                params={{ slug: d.slug }}
                className="docs-nav-link"
                activeProps={{ className: 'docs-nav-link active' }}
                aria-current={active === d.slug ? 'page' : undefined}
                onClick={onNavigate}
              >
                {d.title}
              </Link>
            ))}
          </div>
        ))
      )}
    </aside>
  )
}

function DocsShell({ active, children }: { active?: string; children: React.ReactNode }) {
  const [navOpen, setNavOpen] = useState(false)

  // Close the mobile drawer on Escape.
  useEffect(() => {
    if (!navOpen) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setNavOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [navOpen])

  return (
    <div className="docs-root">
      <a href="#docs-content" className="skip-link">跳到正文</a>
      <DocsTopbar navOpen={navOpen} onMenuToggle={() => setNavOpen((v) => !v)} />
      <div className="docs-body">
        {navOpen && <div className="docs-scrim" aria-hidden="true" onClick={() => setNavOpen(false)} />}
        <DocsSidebar active={active} open={navOpen} onNavigate={() => setNavOpen(false)} />
        <main className="docs-main" id="docs-content" tabIndex={-1}>
          {children}
        </main>
      </div>
    </div>
  )
}

export function DocsIndexPage() {
  useDocumentTitle('Documentation')
  return (
    <DocsShell>
      <div className="doc-prose">
        <p className="doc-eyebrow">文档 · v0.1.0 · 预发布</p>
        <h1>ForgeFlow 文档</h1>
        <p>
          从安装、运维到扩展 ForgeFlow 的一切。初次使用？请从{' '}
          <Link to="/docs/$slug" params={{ slug: 'tutorials-first-workflow' }}>
            你的第一个工作流
          </Link>{' '}
          开始 —— 约 15 分钟即可从克隆跑通到完成一次运行。按 <kbd className="kbd">/</kbd> 搜索。
        </p>
      </div>
      {DOC_GROUPS.map((group) => (
        <section className="docs-index-group" key={group}>
          <h2>{group}</h2>
          <div className="docs-card-grid">
            {DOCS.filter((d) => d.group === group).map((d) => (
              <Link key={d.slug} to="/docs/$slug" params={{ slug: d.slug }} className="docs-card">
                <span className="docs-card-title">{d.title}</span>
                {d.summary && <span className="docs-card-summary">{d.summary}</span>}
              </Link>
            ))}
          </div>
        </section>
      ))}
    </DocsShell>
  )
}

/** ~220 wpm, floored at 1 minute. */
function readingTime(source: string): number {
  const words = source.split(/\s+/).filter(Boolean).length
  return Math.max(1, Math.round(words / 220))
}

/** Right-rail "On this page" with scroll-spy. Hidden on narrow viewports (CSS). */
function DocToc({ items }: { items: TocItem[] }) {
  const [activeId, setActiveId] = useState<string | undefined>()

  useEffect(() => {
    const els = items.map((t) => document.getElementById(t.id)).filter((el): el is HTMLElement => el !== null)
    if (els.length === 0) return
    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries.filter((e) => e.isIntersecting)
        if (visible.length > 0) setActiveId(visible[0].target.id)
      },
      { rootMargin: '-64px 0px -70% 0px' },
    )
    els.forEach((el) => observer.observe(el))
    return () => observer.disconnect()
  }, [items])

  if (items.length < 2) return null
  return (
    <nav className="doc-toc" aria-label="本页目录">
      <div className="doc-toc-title">本页目录</div>
      {items.map((t, i) => (
        <a
          key={`${t.id}-${i}`}
          href={`#${t.id}`}
          className={`doc-toc-link lvl-${t.level}${activeId === t.id ? ' active' : ''}`}
        >
          {t.text}
        </a>
      ))}
    </nav>
  )
}

/** "Was this page helpful?" — recorded locally; issues go to GitHub. */
function DocFeedback({ entry }: { entry: DocEntry }) {
  const storageKey = `ff-docs-feedback:${entry.slug}`
  const [vote, setVote] = useState<string | null>(() => {
    try {
      return window.localStorage.getItem(storageKey)
    } catch {
      return null
    }
  })
  const record = (v: 'up' | 'down') => {
    setVote(v)
    try {
      window.localStorage.setItem(storageKey, v)
    } catch {
      // Storage unavailable (private mode) — the thanks message still shows.
    }
  }

  return (
    <div className="doc-feedback" role="group" aria-label="页面反馈">
      {vote ? (
        <p className="doc-feedback-thanks">
          感谢反馈。{' '}
          <a href={issueUrl(entry)} target="_blank" rel="noopener noreferrer">
            在 GitHub 上反馈问题 ↗
          </a>
        </p>
      ) : (
        <>
          <span>此页对你有帮助吗？</span>
          <button type="button" className="btn sm" onClick={() => record('up')}>
            <IconThumbUp width={14} height={14} /> 有帮助
          </button>
          <button type="button" className="btn sm" onClick={() => record('down')}>
            <IconThumbDown width={14} height={14} /> 没帮助
          </button>
        </>
      )}
    </div>
  )
}

export function DocsArticlePage() {
  const { slug } = useParams({ strict: false }) as { slug?: string }
  const entry = slug ? DOCS_BY_SLUG[slug] : undefined
  useDocumentTitle(entry ? entry.title : 'Documentation')

  // Scroll to a hash target (or the top) after the page renders.
  useEffect(() => {
    const hash = window.location.hash
    requestAnimationFrame(() => {
      if (hash.length > 1) {
        document.getElementById(decodeURIComponent(hash.slice(1)))?.scrollIntoView()
      } else {
        document.getElementById('docs-content')?.scrollTo?.(0, 0)
        window.scrollTo(0, 0)
      }
    })
  }, [slug])

  if (!entry) {
    return (
      <DocsShell>
        <div className="doc-prose">
          <h1>页面不存在</h1>
          <p>
            没有与此 URL 匹配的文档页面。请返回{' '}
            <Link to="/docs">文档首页</Link>。
          </p>
        </div>
      </DocsShell>
    )
  }

  const source = getDocSource(entry.file)
  const { prev, next } = prevNext(entry.slug)
  const toc = source ? extractToc(source) : []

  return (
    <DocsShell active={entry.slug}>
      <div className="docs-article">
        <div className="docs-article-content">
          <nav className="docs-breadcrumbs" aria-label="面包屑">
            <Link to="/docs">文档</Link>
            <span className="sep">/</span>
            <span>{entry.group}</span>
            <span className="sep">/</span>
            <span className="cur">{entry.title}</span>
          </nav>

          {source && (
            <div className="docs-meta">
              <span>{readingTime(source)} 分钟阅读</span>
              <span className="sep" aria-hidden="true">·</span>
              <a href={editUrl(entry.file)} target="_blank" rel="noopener noreferrer">
                在 GitHub 上编辑此页 ↗
              </a>
            </div>
          )}

          {source ? (
            <DocMarkdown source={source} file={entry.file} />
          ) : (
            <div className="doc-prose">
              <h1>{entry.title}</h1>
              <p>无法加载此页的源内容。</p>
            </div>
          )}

          <DocFeedback entry={entry} />

          <nav className="docs-prevnext" aria-label="分页">
            {prev ? (
              <Link to="/docs/$slug" params={{ slug: prev.slug }} className="docs-prevnext-link prev">
                <span className="dir">← 上一篇</span>
                <span className="ttl">{prev.title}</span>
              </Link>
            ) : (
              <span />
            )}
            {next ? (
              <Link to="/docs/$slug" params={{ slug: next.slug }} className="docs-prevnext-link next">
                <span className="dir">下一篇 →</span>
                <span className="ttl">{next.title}</span>
              </Link>
            ) : (
              <span />
            )}
          </nav>
        </div>
        <DocToc items={toc} />
      </div>
    </DocsShell>
  )
}
