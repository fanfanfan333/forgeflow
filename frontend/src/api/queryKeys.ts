/** Run cache keys and invalidation prefixes share one definition. */
export const runKeys = {
  lists: ['hub', 'runs'] as const,
  list: (limit: number) => ['hub', 'runs', limit] as const,
  detail: (runId: string | null) => ['hub', 'run', runId] as const,
  recentLists: ['runs', 'recent'] as const,
  recent: (limit: number) => ['runs', 'recent', limit] as const,
}
