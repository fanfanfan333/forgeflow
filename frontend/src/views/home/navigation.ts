export const NEW_CHAT_EVENT = 'forgeflow:new-chat'
export const SEARCH_HISTORY_EVENT = 'forgeflow:search-history'

export function requestNewChat() {
  window.dispatchEvent(new Event(NEW_CHAT_EVENT))
}

export function requestHistorySearch() {
  window.dispatchEvent(new Event(SEARCH_HISTORY_EVENT))
}
