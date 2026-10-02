// Eagerly import the docs the in-app manifest references as raw strings at build
// time. The repo's docs/ directory is the single source of truth — nothing is
// duplicated here. (vite.config.ts allows reading one level above the frontend root.)
//
// INC-AUDIT —— the glob is restricted to the four trees the manifest uses. The
// previous `docs/**/*.md` also matched `docs/sop/**` (internal engineering reports,
// none of which is in the manifest → unreachable in the SPA, since `resolveHref`
// sends any non-manifest doc to GitHub). Bundling them was dead weight **and** it
// shipped machine-specific absolute paths (e.g. `D:\...\ForgeFlow-main`) into
// `assets/DocsPage-*.js` via those reports' 「how to run」 sections.
const modules = import.meta.glob(
  [
    '../../../docs/*.md',
    '../../../docs/tutorials/*.md',
    '../../../docs/operations/*.md',
    '../../../docs/deployment/*.md',
  ],
  {
    query: '?raw',
    import: 'default',
    eager: true,
  },
) as Record<string, string>

// Key by the path under docs/ (e.g. "tutorials/01-first-workflow.md").
const byFile: Record<string, string> = {}
for (const [key, value] of Object.entries(modules)) {
  const m = key.match(/\/docs\/(.+\.md)$/)
  if (m) byFile[m[1]] = value
}

export function getDocSource(file: string): string | undefined {
  return byFile[file]
}
