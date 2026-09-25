export function MarketplaceView() {
  return (
    <section className="view active" data-screen-label="Marketplace">
      <div className="page-head">
        <div className="row">
          <div>
            <h1>
              技能市场{' '}
              <span className="badge amber" style={{ fontSize: 11, verticalAlign: 'middle' }}>预览</span>
            </h1>
            <p className="sub">可浏览并安装的社区工作流模板</p>
          </div>
        </div>
      </div>
      <div className="page-body">
        <div className="panel">
          <div className="panel-body" style={{ padding: 64, textAlign: 'center', color: 'var(--fg-muted)' }}>
            <p style={{ fontFamily: 'var(--font-mono)', fontSize: 11, letterSpacing: '.12em', textTransform: 'uppercase' }}>
              即将上线
            </p>
            <p style={{ marginTop: 12, fontSize: 13, maxWidth: 460, marginInline: 'auto', lineHeight: 1.6 }}>
              接口已在 <code style={{ color: 'var(--blue-4)' }}>/api/marketplace/templates</code> 列出已安装的模板。
              浏览与安装体验正在开发中。在此之前，你可以到{' '}
              <a href="/console/workflows" style={{ color: 'var(--blue-4)' }}>工作流</a> 页面查看内置模板。
            </p>
          </div>
        </div>
      </div>
    </section>
  )
}
