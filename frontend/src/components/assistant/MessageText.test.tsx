import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { MessageText } from './MessageText'
import { blocks, spans } from './messageFormat'

// ADR 0014 section 5: an answer is untrusted text. Paragraphs, bulleted lists and bold only; a
// link, an image or a script renders as the characters it is. Synthetic texts only.

const ACTIVE = [
  "See <a href='http://synthetic-attacker.example.test'>this</a>.",
  '<img src=x onerror=alert(1)> synthetic',
  "<script>alert('synthetic')</script>",
  '[click](javascript:alert(1)) synthetic',
  'Visit https://synthetic-attacker.example.test/phish now.',
  '![image](https://synthetic-attacker.example.test/x.png)',
  '# A heading that stays text',
]

describe('an answer as text', () => {
  it.each(ACTIVE)('renders %s as text, never as an element', (content) => {
    const { container } = render(<MessageText content={content} />)
    expect(container.querySelector('a, img, script, iframe, h1, h2, h3, style')).toBeNull()
    expect(container.textContent).toBe(content)
  })

  it('keeps paragraphs, bulleted lists and bold', () => {
    const content = 'First **bold** part.\n\n- one\n- **two**\n\nLast line one\nline two'
    const { container } = render(<MessageText content={content} />)
    expect(container.querySelectorAll('p')).toHaveLength(2)
    expect(Array.from(container.querySelectorAll('li'), (li) => li.textContent)).toEqual(['one', 'two'])
    expect(Array.from(container.querySelectorAll('strong'), (b) => b.textContent)).toEqual(['bold', 'two'])
  })

  it('leaves an unpaired or empty bold marker as it is', () => {
    expect(spans('a ** b')).toEqual([{ text: 'a ** b', bold: false }])
    expect(spans('a **** b')).toEqual([{ text: 'a **** b', bold: false }])
    expect(blocks('\n\n')).toEqual([])
  })

  it('makes a list only when every line is a bullet', () => {
    expect(blocks('- one\nnot a bullet')).toEqual([{ kind: 'paragraph', spans: [{ text: '- one\nnot a bullet', bold: false }] }])
  })
})
