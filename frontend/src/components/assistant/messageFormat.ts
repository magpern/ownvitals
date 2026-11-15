// The minimal formatting of an assistant answer (ADR 0014 section 5): paragraphs, bulleted lists
// and bold, nothing else. Pure: text in, a small tree out, rendered by `MessageText` as React text
// nodes. No HTML is parsed, no link is recognised and no URL becomes clickable; whatever else the
// text holds (tags, markdown links, images, headings) stays the literal characters it is.

export interface Span {
  text: string
  bold: boolean
}

export type Block = { kind: 'paragraph'; spans: Span[] } | { kind: 'list'; items: Span[][] }

const BULLET = /^\s*[-*•]\s+/

/** `**bold**` pairs become bold spans; an unpaired `**` stays as it is. */
export function spans(line: string): Span[] {
  const out: Span[] = []
  let rest = line
  for (;;) {
    const open = rest.indexOf('**')
    const close = open < 0 ? -1 : rest.indexOf('**', open + 2)
    if (open < 0 || close < 0 || close === open + 2) {
      if (rest !== '') out.push({ text: rest, bold: false })
      return out
    }
    if (open > 0) out.push({ text: rest.slice(0, open), bold: false })
    out.push({ text: rest.slice(open + 2, close), bold: true })
    rest = rest.slice(close + 2)
  }
}

/** Blocks separated by blank lines; a block whose every line is a bullet is a list. */
export function blocks(content: string): Block[] {
  return content
    .replace(/\r\n?/g, '\n')
    .split(/\n\s*\n/)
    .map((chunk) => chunk.split('\n').filter((line) => line.trim() !== ''))
    .filter((lines) => lines.length > 0)
    .map((lines): Block => {
      if (lines.every((line) => BULLET.test(line))) {
        return { kind: 'list', items: lines.map((line) => spans(line.replace(BULLET, ''))) }
      }
      return { kind: 'paragraph', spans: spans(lines.join('\n')) }
    })
}
