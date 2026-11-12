import { describe, expect, it } from 'vitest'
import { parseInline, parseMarkdown } from './markdown'

// Synthetic markdown only.

describe('parseInline', () => {
  it('reads bold, italic, code and links, nested and in order', () => {
    expect(parseInline('a **b *c* x** `d` [e](f.md)')).toEqual([
      { kind: 'text', text: 'a ' },
      { kind: 'strong', children: [{ kind: 'text', text: 'b ' }, { kind: 'em', children: [{ kind: 'text', text: 'c' }] }, { kind: 'text', text: ' x' }] },
      { kind: 'text', text: ' ' },
      { kind: 'code', text: 'd' },
      { kind: 'text', text: ' ' },
      { kind: 'link', target: 'f.md', children: [{ kind: 'text', text: 'e' }] },
    ])
  })

  it('keeps raw HTML as text', () => {
    expect(parseInline('<script>x</script>')).toEqual([{ kind: 'text', text: '<script>x</script>' }])
  })
})

describe('parseMarkdown', () => {
  it('reads headings, paragraphs, quotes, lists and tables', () => {
    const blocks = parseMarkdown(['# T', '', '> draft', '> more', '', 'one', 'two', '', '- a', '- b', '', '1. x', '2. y', '', '| H1 | H2 |', '|---|---|', '| c1 | c2 |'].join('\n'))
    expect(blocks.map((block) => block.kind)).toEqual(['heading', 'quote', 'paragraph', 'list', 'list', 'table'])
    expect(blocks[1]).toEqual({ kind: 'quote', children: [{ kind: 'text', text: 'draft more' }] })
    expect(blocks[2]).toEqual({ kind: 'paragraph', children: [{ kind: 'text', text: 'one two' }] })
    expect(blocks[3]).toMatchObject({ ordered: false, items: [[{ text: 'a' }], [{ text: 'b' }]] })
    expect(blocks[4]).toMatchObject({ ordered: true })
    expect(blocks[5]).toMatchObject({ head: [[{ text: 'H1' }], [{ text: 'H2' }]], rows: [[[{ text: 'c1' }], [{ text: 'c2' }]]] })
  })

  it('always makes progress on a stray pipe or odd line', () => {
    expect(parseMarkdown('| not a table\nnext')).toEqual([{ kind: 'paragraph', children: [{ kind: 'text', text: '| not a table next' }] }])
  })
})
