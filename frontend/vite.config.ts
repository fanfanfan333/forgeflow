import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Backend base URL the `/api` proxy forwards to. Defaults to the
// docker-compose API port (8000); override with VITE_API_TARGET to point the
// dev/preview server at a locally-run API (e.g. http://localhost:8010).
const apiTarget = process.env.VITE_API_TARGET ?? 'http://localhost:8000'

const proxy = {
  '/api': {
    target: apiTarget,
    changeOrigin: true,
    rewrite: (path: string) => path.replace(/^\/api/, ''),
  },
  // FastAPI's Swagger UI (served at /api/docs) fetches the spec from the
  // root-absolute /openapi.json. In prod nginx rewrites that to
  // /api/openapi.json; in dev we proxy it straight through so the API
  // reference renders instead of parsing the SPA's index.html.
  // (We do NOT proxy /docs — that's the in-app documentation SPA route.)
  '/openapi.json': {
    target: apiTarget,
    changeOrigin: true,
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
