import arrayPaths from './api-array-paths.generated'

/** Container shapes come from the server contract, never a universal list object. */
export function emptyApiResponse(url: string, fallback: unknown = { total: 0, items: [] }): unknown {
  const path = new URL(url).pathname.replace(/^\/api/, '')
  const array = arrayPaths.some(pattern => new RegExp(`^${pattern.replace(/\{[^}]+\}/g, '[^/]+')}/?$`).test(path))
  return array ? [] : fallback
}
