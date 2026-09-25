import { humanizeError } from '../api/errors'

/**
 * Renders a humanised error message. The raw, untranslated diagnostic string is
 * preserved in the `title` attribute so nothing is lost to translation.
 *
 * The console has a single translation path (`humanizeError`) precisely so the
 * "raw English detail" bug cannot drift back in one view at a time.
 */
export function ErrorText({ error, label = '加载失败' }: { error: unknown; label?: string }) {
  const { label: text, detail } = humanizeError(error, label)
  return <span title={detail}>{text}</span>
}
