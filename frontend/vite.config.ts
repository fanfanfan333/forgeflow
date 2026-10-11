import { defineConfig, type ProxyOptions } from 'vite'
import react from '@vitejs/plugin-react'
import type { ServerResponse } from 'node:http'

// Backend base URL the `/api` proxy forwards to.
//
// Default = http://localhost:8010 — ForgeFlow's *local* backend port
// (`forgeflow/bootstrap/runtime.py::DEFAULT_PORT = 8010`). It deliberately does
// NOT default to :8000: on a shared dev host `:8000` is a *different* project
// (the RAG stack), so `npm run dev` would silently proxy every `/api` call into
// the wrong service and the login form would fail with a bare 502 「网关错误」.
// VITE_API_TARGET still overrides it (docker-compose serves the SPA through
// nginx, whose `/api` upstream is the in-cluster `http://api:8000` — that is an
// nginx concern, not this dev/preview proxy).
const DEFAULT_API_TARGET = 'http://localhost:8010'
const apiTarget = process.env.VITE_API_TARGET ?? DEFAULT_API_TARGET

/**
 * Narrow the proxy `error` callback's `ServerResponse | net.Socket` to the
 * former: only a real HTTP response can carry our JSON diagnostic body.
 */
function isServerResponse(res: unknown): res is ServerResponse {
  return typeof (res as { writeHead?: unknown } | null | undefined)?.writeHead === 'function'
}

/**
 * Build a `configure` hook that replaces Vite's default bare 502 with a
 * self-diagnosing JSON body.
 *
 * The problem it fixes: when the upstream is unreachable, Vite's own error
 * handler answers `502` with an **empty** `text/plain` body, and the SPA can
 * only render the generic status text as 「登录失败（502）：网关错误」 — which
 * tells the user nothing about *why*. Vite registers its handler *after*
 * `configure` runs, so ours is invoked first; by the time Vite's own guard
 * `if (!res.headersSent && !res.writableEnded)` is evaluated we have already
 * written the response, so it is left untouched (Vite still logs the error).
 *
 * The body stays honest (mirrors `src/api/errors.ts`'s "never swallow the real
 * detail" rule): it echoes the **real** `target`, a Chinese explanation, the
 * concrete action to take, and the upstream error code/message — never a vague
 * placeholder.
 *
 * @param target The resolved `/api` proxy target, echoed back verbatim.
 */
function reportProxyError(target: string): NonNullable<ProxyOptions['configure']> {
  return (proxy) => {
    proxy.on('error', (err, _req, res) => {
      // A WebSocket error hands us a raw `net.Socket` (no `writeHead`) — skip it.
      if (!isServerResponse(res)) return
      if (res.headersSent || res.writableEnded) return
      const cause = (err as NodeJS.ErrnoException).code ?? err.message
      const body = JSON.stringify({
        error: 'proxy_unreachable',
        detail: `Vite 开发代理无法连接后端服务 ${target}。`,
        target,
        hint:
          '请确认后端已在本机启动（python -m forgeflow.bootstrap，默认端口 8010）；' +
          '若后端在其他地址，请设置 VITE_API_TARGET 指向它并重新运行 npm run dev。',
        cause,
      })
      res.writeHead(502, { 'Content-Type': 'application/json; charset=utf-8' })
      res.end(body)
    })
  }
}

const proxy = {
  '/api': {
    target: apiTarget,
    changeOrigin: true,
    rewrite: (path: string) => path.replace(/^\/api/, ''),
    // Make an unreachable backend self-diagnosing instead of a bare 502.
    configure: reportProxyError(apiTarget),
  },
  // FastAPI's Swagger UI (served at /api/docs) fetches the spec from the
  // root-absolute /openapi.json. In prod nginx rewrites that to
  // /api/openapi.json; in dev we proxy it straight through so the API
  // reference renders instead of parsing the SPA's index.html.
  // (We do NOT proxy /docs — that's the in-app documentation SPA route.)
  '/openapi.json': {
    target: apiTarget,
    changeOrigin: true,
    // Same diagnostics as /api — this proxy hits the same backend.
    configure: reportProxyError(apiTarget),
  },
}

export default defineConfig({
  plugins: [react()],
  server: {
    allowedHosts: ['localhost', '127.0.0.1', 'host.docker.internal'],
    // Allow importing the repo's docs/*.md (one level above the frontend root)
    // as the single source of truth for the in-app /docs route.
    fs: { allow: ['..'] },
    proxy,
  },
  // Mirror the proxy for `vite preview` so a pre-built dist can be served
  // against a real backend without nginx.
  preview: {
    proxy,
  },
})
