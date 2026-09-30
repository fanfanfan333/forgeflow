/**
 * INC14 — a lightweight Markdown renderer for the run's result body.
 *
 * Deliberately NOT `components/DocMarkdown.tsx`: that component is docs-page
 * specific — it rewrites links to `/docs` routes, resolves images through the
 * docs manifest, adds `#` heading anchors and code "copy" buttons. Coupling the
 * runs view to the docs manifest would be an architectural smell. Here we reuse
 * the **dependencies** (react-markdown + remark-gfm + rehype-highlight) but not
 * the docs component.
 *
 * The body is rendered **verbatim** — no rewriting, sanitising of meaning, or
 * templating. GFM is enabled so the report's Markdown table renders as a table.
 */
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeHighlight from 'rehype-highlight'

export function ResultMarkdown({ source }: { source: string }) {
  return (
    <div className="res-prose">
      <Markdown
        remarkPlugins={[remarkGfm]}
        // `detect: false` avoids pulling every highlight.js grammar in just to
        // guess at an unlabelled fence. Labelled fences still highlight.
        rehypePlugins={[[rehypeHighlight, { detect: false }]]}
      >
        {source}
      </Markdown>
    </div>
  )
}
