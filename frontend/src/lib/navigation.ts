/** Sends the whole browser to a provider's authorization page. Only http(s) URLs. */
export function goToProvider(url: string): void {
  const parsed = new URL(url)
  if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') {
    throw new Error('Refusing to leave for a non-http(s) address.')
  }
  window.location.assign(parsed.href)
}
