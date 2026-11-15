import { vi } from 'vitest'

// An `EventSource` for the tests (the network guard refuses the real one). A test drives it:
// `emit('scanned')` delivers an event of that name, `fail()` closes it as a browser does when the
// server ends the stream for good. Synthetic: no server, no network.

export class FakeEventSource {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSED = 2
  static opened: FakeEventSource[] = []

  readonly url: string
  readyState = FakeEventSource.OPEN
  onerror: ((event: Event) => void) | null = null
  private readonly listeners = new Map<string, ((event: MessageEvent) => void)[]>()

  constructor(url: string) {
    this.url = url
    FakeEventSource.opened.push(this)
  }

  addEventListener(name: string, listener: (event: MessageEvent) => void): void {
    this.listeners.set(name, [...(this.listeners.get(name) ?? []), listener])
  }

  close(): void {
    this.readyState = FakeEventSource.CLOSED
  }

  emit(name: string): void {
    if (this.readyState === FakeEventSource.CLOSED) return
    for (const listener of this.listeners.get(name) ?? []) {
      listener(new MessageEvent(name, { data: JSON.stringify({ state: name }), lastEventId: name }))
    }
  }

  fail(): void {
    this.readyState = FakeEventSource.CLOSED
    this.onerror?.(new Event('error'))
  }

  /** The newest stream, which a test drives. */
  static latest(): FakeEventSource {
    const last = FakeEventSource.opened.at(-1)
    if (last === undefined) throw new Error('no EventSource was opened')
    return last
  }
}

/** Puts `FakeEventSource` in place of the global for this test. */
export function installEventSource(): typeof FakeEventSource {
  FakeEventSource.opened = []
  vi.stubGlobal('EventSource', FakeEventSource)
  return FakeEventSource
}
