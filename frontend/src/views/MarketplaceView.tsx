/**
 * 技能市场 (/marketplace) —— 真实消费后端接口（INC34）。
 *
 * 消费的**全部是真实端点**（`forgeflow/api/routers/marketplace.py`）：
 *   GET  /marketplace/skills                 浏览（自家 + 可选共享）
 *   POST /marketplace/skills/publish         上架（DLP + 可信基线双预检）
 *   POST /marketplace/skills/{id}/install    安装到当前租户
 *   POST /marketplace/skills/{id}/rate       评分（1–5）
 *   GET  /marketplace/templates              工作流模板
 *   POST /marketplace/templates/refresh      重扫模板目录
 *
 * 视觉纪律：灰白高级极简（弱边框、充足留白、少按钮），**不做**满屏表格的后台。
 * 每一个动作都真调后端：成功给真实反馈，被拒（403 / 404 / 422）给**逐字**原因 ——
 * 绝不伪造成功，也绝不把失败说成成功。空态一律诚实（「暂无上架技能」等）。
 *
 * `/marketplace` 路由仍是 manager+ 门控（router.tsx）——本文件**不改**门控口径。
 */

import { useState } from 'react'
import { humanizeError } from '../api/errors'
import type { MarketplaceListing, MarketplaceTemplate } from '../api/client'
import {
  useHubSkills,
  useInstallListing,
  useMarketplaceListings,
  useMarketplaceTemplates,
  usePublishListing,
  useRateListing,
  useRefreshTemplates,
} from '../api/hooks'
import { IconChevronDown, IconSparkle } from '../components/icons'
import '../styles/marketplace.css'
// INC38 —— 本页工具栏复用技能中心定义的 `.hub-toolbar` / `.hub-search` / `.hub-select`
// 原子；此前只 import 了 marketplace.css，导致这些类在本页**全部未生效**（真机实测
// computedStyle：`.hub-toolbar` 落回 `display:block`、`gap:normal`），工具栏控件彼此
// 紧贴无间距。按既有多 Hub 页做法（KnowledgeView / SecurityView / SkillsView 均如此）
// 引入共享的 skills.css，恢复工具栏的间距与搜索框外观。
import '../styles/skills.css'

const DOMAINS = ['', '数据分析', '企业知识库', '项目管理', '代码开发', 'general']

export function MarketplaceView() {
  const [q, setQ] = useState('')
  const [domain, setDomain] = useState('')
  const [crossTenant, setCrossTenant] = useState(false)

  const listingsQ = useMarketplaceListings({
    q: q || undefined,
    domain: domain || undefined,
    cross_tenant: crossTenant,
    limit: 50,
  })
  const listings = listingsQ.data?.items ?? []
  const listingsErr = listingsQ.isError
    ? humanizeError(listingsQ.error, '市场技能加载失败')
    : null

  return (
    <section className="view active" data-screen-label="技能市场" data-testid="marketplace-view">
      <div className="page-head">
        <div className="row">
          <div>
            <h1>技能市场</h1>
            <p className="sub">浏览、安装并复用团队沉淀的技能资产 · 数据来自市场接口</p>
          </div>
        </div>
      </div>

      <div className="page-body hub">
        <div className="hub-toolbar">
          <label className="hub-search">
            <span aria-hidden="true">⌕</span>
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="搜索技能名称或描述…"
              aria-label="搜索市场技能"
            />
          </label>
          <select
            className="hub-select"
            value={domain}
            onChange={(e) => setDomain(e.target.value)}
            aria-label="领域筛选"
          >
            {DOMAINS.map((d) => (
              <option key={d} value={d}>
                {d === '' ? '全部领域' : d}
              </option>
            ))}
          </select>
          <label className="mk-toggle">
            <input
              type="checkbox"
              checked={crossTenant}
              onChange={(e) => setCrossTenant(e.target.checked)}
            />
            包含共享技能
          </label>
          <span className="text-mono text-muted text-12">
            {listingsQ.isLoading ? '加载中…' : `${listings.length} 项上架`}
          </span>
        </div>

        {listingsErr ? (
          <div className="card empty" title={listingsErr.detail} role="alert">
            {listingsErr.label}
          </div>
        ) : listings.length === 0 && !listingsQ.isLoading ? (
          <div className="card empty" data-testid="marketplace-empty">
            <span className="big">
              <IconSparkle width={22} height={22} />
            </span>
            暂无上架技能
            <p className="mk-empty-hint">
              在下方「上架技能」里选择一个已发布的技能，把它列入市场后即可被安装与评分。
            </p>
          </div>
        ) : (
          <div className="mk-grid" data-testid="marketplace-listings">
            {listings.map((l) => (
              <ListingCard key={l.id} listing={l} />
            ))}
          </div>
        )}

        <PublishPanel />
        <TemplatesPanel />
      </div>
    </section>
  )
}

function ListingCard({ listing }: { listing: MarketplaceListing }) {
  const install = useInstallListing()
  const rate = useRateListing()
  const [score, setScore] = useState(5)

  const installErr = install.isError ? humanizeError(install.error, '安装失败') : null
  const rateErr = rate.isError ? humanizeError(rate.error, '评分失败') : null

  return (
    <div className="mk-card">
      <div className="mk-top">
        <span className="mk-name">{listing.name || '（未命名技能）'}</span>
        {listing.domain && <span className="badge purple">{listing.domain}</span>}
      </div>
      <span className="mk-desc">{listing.description || '暂无描述'}</span>
      <div className="mk-meta">
        <span>v{listing.version || '—'}</span>
        <span>·</span>
        <span>
          {listing.rating_count > 0
            ? `评分 ${listing.rating.toFixed(1)}（${listing.rating_count}）`
            : '暂无评分'}
        </span>
        <span>·</span>
        <span>{listing.installs} 次安装</span>
        {listing.shared && <span className="badge emerald">已共享</span>}
      </div>
      <div className="mk-actions">
        <button
          type="button"
          className="btn sm primary"
          disabled={install.isPending}
          onClick={() => install.mutate(listing.id)}
        >
          {install.isPending ? '安装中…' : '安装'}
        </button>
        <label className="mk-rate">
          <select
            value={score}
            onChange={(e) => setScore(Number(e.target.value))}
            aria-label="评分"
          >
            {[5, 4, 3, 2, 1].map((n) => (
              <option key={n} value={n}>
                {n} 星
              </option>
            ))}
          </select>
          <button
            type="button"
            className="btn sm ghost"
            disabled={rate.isPending}
            onClick={() => rate.mutate({ listingId: listing.id, score })}
          >
            {rate.isPending ? '评分中…' : '提交评分'}
          </button>
        </label>
      </div>
      {install.isSuccess && (
        <p className="mk-note ok" role="status">
          已安装到当前工作区
        </p>
      )}
      {installErr && (
        <p className="mk-note err" role="alert" title={installErr.detail}>
          {installErr.label}
        </p>
      )}
      {rate.isSuccess && (
        <p className="mk-note ok" role="status">
          已记录评分
        </p>
      )}
      {rateErr && (
        <p className="mk-note err" role="alert" title={rateErr.detail}>
          {rateErr.label}
        </p>
      )}
    </div>
  )
}

function PublishPanel() {
  const skillsQ = useHubSkills({ limit: 100 })
  const skills = skillsQ.data?.items ?? []
  const publish = usePublishListing()
  const [skillId, setSkillId] = useState('')
  const [shared, setShared] = useState(false)
  const [description, setDescription] = useState('')

  const err = publish.isError ? humanizeError(publish.error, '上架失败') : null

  return (
    <details className="mk-panel" data-testid="marketplace-publish">
      <summary className="mk-summary">
        上架技能
        <IconChevronDown className="chev" />
      </summary>
      <div className="mk-panel-body">
        {skills.length === 0 && !skillsQ.isLoading ? (
          <p className="mk-sub">
            当前工作区还没有可上架的技能，先到「技能中心」创建或沉淀一个。
          </p>
        ) : (
          <>
            <label className="mk-field">
              <span>选择技能</span>
              <select value={skillId} onChange={(e) => setSkillId(e.target.value)}>
                <option value="">请选择…</option>
                {skills.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                    {s.current_version ? ` · v${s.current_version}` : ''}
                  </option>
                ))}
              </select>
            </label>
            <label className="mk-field">
              <span>上架说明（可选）</span>
              <input
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="一句话说明这个技能解决什么问题"
              />
            </label>
            <label className="mk-toggle">
              <input type="checkbox" checked={shared} onChange={(e) => setShared(e.target.checked)} />
              允许其他工作区安装（需审核权限）
            </label>
            <div className="mk-actions">
              <button
                type="button"
                className="btn sm primary"
                disabled={publish.isPending || !skillId}
                onClick={() =>
                  publish.mutate({
                    skill_id: skillId,
                    shared,
                    description: description.trim() || undefined,
                  })
                }
              >
                {publish.isPending ? '上架中…' : '上架'}
              </button>
            </div>
          </>
        )}
        {publish.isSuccess && (
          <p className="mk-note ok" role="status">
            已上架「{publish.data.listing.name}」
          </p>
        )}
        {err && (
          <p className="mk-note err" role="alert" title={err.detail}>
            {err.label}
          </p>
        )}
      </div>
    </details>
  )
}

function TemplatesPanel() {
  const templatesQ = useMarketplaceTemplates()
  const refresh = useRefreshTemplates()
  const templates = templatesQ.data?.templates ?? []
  const err = templatesQ.isError ? humanizeError(templatesQ.error, '模板加载失败') : null

  return (
    <details className="mk-panel" data-testid="marketplace-templates">
      <summary className="mk-summary">
        工作流模板
        <span className="mk-count">{templatesQ.isLoading ? '…' : templates.length}</span>
        <IconChevronDown className="chev" />
      </summary>
      <div className="mk-panel-body">
        {err ? (
          <p className="mk-note err" role="alert" title={err.detail}>
            {err.label}
          </p>
        ) : templates.length === 0 && !templatesQ.isLoading ? (
          <p className="mk-sub">暂无可用模板</p>
        ) : (
          <div className="mk-tpl-list">
            {templates.map((t) => (
              <TemplateRow key={`${t.domain}/${t.name}`} template={t} />
            ))}
          </div>
        )}
        <div className="mk-actions">
          <button
            type="button"
            className="btn sm ghost"
            disabled={refresh.isPending}
            onClick={() => refresh.mutate()}
          >
            {refresh.isPending ? '刷新中…' : '重新扫描模板目录'}
          </button>
          {refresh.isSuccess && (
            <span className="mk-sub">已刷新，共 {refresh.data.total} 个模板</span>
          )}
        </div>
      </div>
    </details>
  )
}

function TemplateRow({ template }: { template: MarketplaceTemplate }) {
  return (
    <div className="mk-tpl">
      <div className="mk-tpl-head">
        <span className="mk-tpl-name">{template.name}</span>
        <span className="mk-tpl-meta">
          {template.domain}
          {template.version ? ` · v${template.version}` : ''}
        </span>
      </div>
      <p className="mk-tpl-desc">{template.description || '暂无描述'}</p>
    </div>
  )
}
