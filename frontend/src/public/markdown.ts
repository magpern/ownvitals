// A tiny markdown reader for the legal drafts (docs/legal/*.md, ADR 0016 section 6). Pure: text
// in, a node tree out. It reads only what the drafts use: headings, paragraphs, bullet and
// numbered lists, block quotes, pipe tables, and inline **bold**, *italic*, `code` and
// [text](target). Raw HTML is not markdown here: every character is text, so the output can only
// hold elements this file names. The tree is rendered by React (LegalBody.tsx), never as a string.

export type Inline =
  | { kind: 'text'; text: string }
  | { kind: 'strong' | 'em'; children: Inline[] }
  | { kind: 'code'; text: string }
  | { kind: 'link'; target: string; children: Inline[] }

export type Block =
  | { kind: 'heading'; level: 1 | 2 | 3; children: Inline[] }
  | { kind: 'paragraph'; children: Inline[] }
  | { kind: 'quote'; children: Inline[] }
  | { kind: 'list'; ordered: boolean; items: Inline[][] }
  | { kind: 'table'; head: Inline[][]; rows: Inline[][][] }

const TOKEN = /`([^`]+)`|\*\*(.+?)\*\*|\*(.+?)\*|\[([^\]]+)\]\(([^)\s]+)\)/

export function parseInline(source: string): Inline[] {
  const found = TOKEN.exec(source)
  if (!found) return source ? [{ kind: 'text', text: source }] : []
  const before = source.slice(0, found.index)
  const after = source.slice(found.index + found[0].length)
  const lead: Inline[] = before ? [{ kind: 'text', text: before }] : []
  let node: Inline
  if (found[1] !== undefined) node = { kind: 'code', text: found[1] }
  else if (found[2] !== undefined) node = { kind: 'strong', children: parseInline(found[2]) }
  else if (found[3] !== undefined) node = { kind: 'em', children: parseInline(found[3]) }
  else node = { kind: 'link', target: found[5] ?? '', children: parseInline(found[4] ?? '') }
  return [...lead, node, ...parseInline(after)]
}

const cells = (line: string): string[] => line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((cell) => cell.trim())
const isRule = (line: string) => /^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?$/.test(line.trim())
const BULLET = /^[-*] +(.*)$/
const NUMBER = /^\d+\. +(.*)$/

export function parseMarkdown(source: string): Block[] {
  const lines = source.replace(/\r\n?/g, '\n').split('\n')
  const blocks: Block[] = []
  let i = 0
  while (i < lines.length) {
    const line = lines[i] ?? ''
    const heading = /^(#{1,3}) +(.*)$/.exec(line)
    if (!line.trim()) {
      i += 1
    } else if (heading) {
      blocks.push({ kind: 'heading', level: (heading[1] ?? '#').length as 1 | 2 | 3, children: parseInline((heading[2] ?? '').trim()) })
      i += 1
    } else if (line.startsWith('>')) {
      const text: string[] = []
      while (i < lines.length && (lines[i] ?? '').startsWith('>')) {
        text.push((lines[i] ?? '').replace(/^>\s?/, ''))
        i += 1
      }
      blocks.push({ kind: 'quote', children: parseInline(text.join(' ').trim()) })
    } else if (line.trim().startsWith('|') && isRule(lines[i + 1] ?? '')) {
      const head = cells(line).map(parseInline)
      const rows: Inline[][][] = []
      i += 2
      while (i < lines.length && (lines[i] ?? '').trim().startsWith('|')) {
        rows.push(cells(lines[i] ?? '').map(parseInline))
        i += 1
      }
      blocks.push({ kind: 'table', head, rows })
    } else if (BULLET.test(line) || NUMBER.test(line)) {
      const ordered = NUMBER.test(line)
      const item = ordered ? NUMBER : BULLET
      const items: Inline[][] = []
      while (i < lines.length && item.test(lines[i] ?? '')) {
        items.push(parseInline(item.exec(lines[i] ?? '')?.[1] ?? ''))
        i += 1
      }
      blocks.push({ kind: 'list', ordered, items })
    } else {
      const text: string[] = [line.trim()]
      i += 1
      while (i < lines.length && (lines[i] ?? '').trim() && !/^(#{1,3} |>|\|)/.test(lines[i] ?? '') && !BULLET.test(lines[i] ?? '') && !NUMBER.test(lines[i] ?? '')) {
        text.push((lines[i] ?? '').trim())
        i += 1
      }
      blocks.push({ kind: 'paragraph', children: parseInline(text.join(' ')) })
    }
  }
  return blocks
}
