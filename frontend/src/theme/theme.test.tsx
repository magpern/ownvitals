import { act, fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  THEME_ATTRIBUTE,
  THEME_PREFERENCES,
  THEME_STORAGE_KEY,
  applyTheme,
  getThemePreference,
  initTheme,
  isThemePreference,
  readStoredPreference,
  setThemePreference,
  subscribeThemePreference,
  type ThemePreference,
} from './theme'
import { useThemePreference } from './useThemePreference'

// System (the default), Light and Dark: what is stored, what is put on <html>, and what a change
// does. How the stylesheet answers the attribute and the device is src/styles/tokens.test.ts.

const root = document.documentElement
const attribute = () => root.getAttribute(THEME_ATTRIBUTE)
const stored = () => window.localStorage.getItem(THEME_STORAGE_KEY)

beforeEach(() => {
  window.localStorage.clear()
  root.removeAttribute(THEME_ATTRIBUTE)
  initTheme()
})

describe('the choices', () => {
  it('are System, Light and Dark, with System first', () => {
    expect(THEME_PREFERENCES).toEqual(['system', 'light', 'dark'])
    expect(isThemePreference('system')).toBe(true)
    expect(isThemePreference('sepia')).toBe(false)
    expect(isThemePreference(null)).toBe(false)
  })
})

describe('reading what is stored', () => {
  it.each<[string | null, ThemePreference]>([
    [null, 'system'],
    ['system', 'system'],
    ['light', 'light'],
    ['dark', 'dark'],
    ['Dark', 'system'],
    ['sepia', 'system'],
    ['', 'system'],
  ])('%j is %s', (value, expected) => {
    if (value !== null) window.localStorage.setItem(THEME_STORAGE_KEY, value)
    expect(readStoredPreference()).toBe(expected)
  })

  it('is System when storage cannot be read', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    expect(readStoredPreference()).toBe('system')
    expect(() => initTheme()).not.toThrow()
    expect(attribute()).toBeNull()
  })
})

describe('applying a choice', () => {
  it('puts data-theme on the root for Light and Dark, in either direction', () => {
    applyTheme('dark')
    expect(attribute()).toBe('dark')
    applyTheme('light')
    expect(attribute()).toBe('light')
    applyTheme('dark')
    expect(attribute()).toBe('dark')
  })

  it('sets no attribute for System, and takes one away', () => {
    expect(attribute()).toBeNull()
    applyTheme('light')
    applyTheme('system')
    expect(attribute()).toBeNull()
    expect(root.hasAttribute(THEME_ATTRIBUTE)).toBe(false)
  })

  it('writes nothing else to the root: no inline style, no script', () => {
    applyTheme('dark')
    expect(root.hasAttribute('style')).toBe(false)
    expect(document.querySelectorAll('script:not([src]), style')).toHaveLength(0)
  })
})

describe('before the first render (initTheme)', () => {
  it('applies a stored Dark and a stored Light', () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, 'dark')
    expect(initTheme()).toBe('dark')
    expect(attribute()).toBe('dark')
    expect(getThemePreference()).toBe('dark')
    window.localStorage.setItem(THEME_STORAGE_KEY, 'light')
    expect(initTheme()).toBe('light')
    expect(attribute()).toBe('light')
  })

  it('leaves the root alone for System: nothing stored, or anything unknown', () => {
    expect(initTheme()).toBe('system')
    expect(attribute()).toBeNull()
    window.localStorage.setItem(THEME_STORAGE_KEY, 'sepia')
    expect(initTheme()).toBe('system')
    expect(attribute()).toBeNull()
  })

  it('removes a stale attribute when the choice is System', () => {
    root.setAttribute(THEME_ATTRIBUTE, 'dark')
    initTheme()
    expect(attribute()).toBeNull()
  })
})

describe('changing the choice', () => {
  it('applies it at once and keeps it for this browser', () => {
    setThemePreference('dark')
    expect(attribute()).toBe('dark')
    expect(stored()).toBe('dark')
    expect(getThemePreference()).toBe('dark')
    setThemePreference('light')
    expect(attribute()).toBe('light')
    expect(stored()).toBe('light')
  })

  it('goes back to System by removing both the attribute and the stored value', () => {
    setThemePreference('dark')
    setThemePreference('system')
    expect(attribute()).toBeNull()
    expect(stored()).toBeNull()
    expect(getThemePreference()).toBe('system')
  })

  it('survives a reload: what is stored is applied again', () => {
    setThemePreference('dark')
    root.removeAttribute(THEME_ATTRIBUTE)
    initTheme()
    expect(attribute()).toBe('dark')
  })

  it('ignores a value that is not a choice', () => {
    setThemePreference('dark')
    setThemePreference('sepia' as ThemePreference)
    expect(attribute()).toBe('dark')
    expect(stored()).toBe('dark')
  })

  it('still applies it for this page when storage refuses (a private window, blocked site data)', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('quota')
    })
    vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    expect(() => {
      setThemePreference('dark')
    }).not.toThrow()
    expect(attribute()).toBe('dark')
    expect(getThemePreference()).toBe('dark')
    expect(() => {
      setThemePreference('system')
    }).not.toThrow()
    expect(attribute()).toBeNull()
  })

  it('tells subscribers once per change, and not after they leave', () => {
    const listener = vi.fn()
    const leave = subscribeThemePreference(listener)
    setThemePreference('dark')
    setThemePreference('dark')
    expect(listener).toHaveBeenCalledTimes(1)
    setThemePreference('light')
    expect(listener).toHaveBeenCalledTimes(2)
    leave()
    setThemePreference('system')
    expect(listener).toHaveBeenCalledTimes(2)
  })
})

describe('another tab of the same browser', () => {
  it('changes the choice here too', () => {
    const listener = vi.fn()
    subscribeThemePreference(listener)
    window.localStorage.setItem(THEME_STORAGE_KEY, 'dark')
    window.dispatchEvent(new StorageEvent('storage', { key: THEME_STORAGE_KEY, newValue: 'dark' }))
    expect(attribute()).toBe('dark')
    expect(getThemePreference()).toBe('dark')
    expect(listener).toHaveBeenCalledTimes(1)

    window.localStorage.removeItem(THEME_STORAGE_KEY)
    window.dispatchEvent(new StorageEvent('storage', { key: THEME_STORAGE_KEY, newValue: null }))
    expect(attribute()).toBeNull()
    expect(getThemePreference()).toBe('system')
  })

  it('is not moved by another key', () => {
    setThemePreference('dark')
    window.localStorage.setItem('something.else', 'light')
    window.dispatchEvent(new StorageEvent('storage', { key: 'something.else', newValue: 'light' }))
    expect(attribute()).toBe('dark')
  })
})

describe('the hook for the Settings page', () => {
  function Picker() {
    const [preference, setPreference] = useThemePreference()
    return (
      <div>
        <p>Theme: {preference}</p>
        {THEME_PREFERENCES.map((choice) => (
          <button
            key={choice}
            type="button"
            aria-pressed={preference === choice}
            onClick={() => {
              setPreference(choice)
            }}
          >
            {choice}
          </button>
        ))}
      </div>
    )
  }

  it('shows the choice in force, and changes it in both directions', () => {
    render(<Picker />)
    expect(screen.getByText('Theme: system')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'dark' }))
    expect(screen.getByText('Theme: dark')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'dark' })).toHaveAttribute('aria-pressed', 'true')
    expect(attribute()).toBe('dark')
    fireEvent.click(screen.getByRole('button', { name: 'light' }))
    expect(screen.getByText('Theme: light')).toBeInTheDocument()
    expect(attribute()).toBe('light')
    fireEvent.click(screen.getByRole('button', { name: 'system' }))
    expect(screen.getByText('Theme: system')).toBeInTheDocument()
    expect(attribute()).toBeNull()
  })

  it('follows a change made elsewhere', () => {
    render(<Picker />)
    act(() => {
      setThemePreference('dark')
    })
    expect(screen.getByText('Theme: dark')).toBeInTheDocument()
  })

  it('starts from what is stored', () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, 'light')
    initTheme()
    render(<Picker />)
    expect(screen.getByText('Theme: light')).toBeInTheDocument()
  })
})
