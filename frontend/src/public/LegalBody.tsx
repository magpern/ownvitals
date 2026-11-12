import { Fragment, type ReactNode } from 'react'
import type { Locale } from '../i18n/locale'
import { legalBlocks, legalLinkTarget } from './legalPages'
import type { Block, Inline } from './markdown'
import type { LegalPage } from './routes'

// The legal drafts as plain elements (ADR 0016 section 6): no script, no style attribute, no
// external link. A link to another draft becomes a link to that page; any other target is shown
// as text only. The draft banner (a block quote) is kept as written, so the page says it is a
// draft and not legal advice.

function inline(nodes: Inline[]): ReactNode {
  return nodes.map((node, index) => {
    switch (node.kind) {
      case 'text':
        return <Fragment key={index}>{node.text}</Fragment>
      case 'code':
        return <code key={index}>{node.text}</code>
      case 'strong':
        return <strong key={index}>{inline(node.children)}</strong>
      case 'em':
        return <em key={index}>{inline(node.children)}</em>
      case 'link': {
        const href = legalLinkTarget(node.target)
        return href ? (
          <a key={index} href={href}>
            {inline(node.children)}
          </a>
        ) : (
          <Fragment key={index}>{inline(node.children)}</Fragment>
        )
      }
    }
  })
}

function block(item: Block, index: number): ReactNode {
  switch (item.kind) {
    case 'heading':
      return item.level === 1 ? <h1 key={index}>{inline(item.children)}</h1> : item.level === 2 ? <h2 key={index}>{inline(item.children)}</h2> : <h3 key={index}>{inline(item.children)}</h3>
    case 'paragraph':
      return <p key={index}>{inline(item.children)}</p>
    case 'quote':
      return (
        <blockquote key={index} className="pub-draft">
          <p>{inline(item.children)}</p>
        </blockquote>
      )
    case 'list': {
      const items = item.items.map((children, at) => <li key={at}>{inline(children)}</li>)
      return item.ordered ? <ol key={index}>{items}</ol> : <ul key={index}>{items}</ul>
    }
    case 'table':
      return (
        <div key={index} className="pub-table">
          <table>
            <thead>
              <tr>
                {item.head.map((cell, at) => (
                  <th key={at} scope="col">
                    {inline(cell)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {item.rows.map((row, at) => (
                <tr key={at}>
                  {row.map((cell, col) => (
                    <td key={col}>{inline(cell)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )
  }
}

export function LegalBody({ page, locale }: { page: LegalPage; locale: Locale }) {
  return <article className="pub-legal">{legalBlocks(page, locale).map(block)}</article>
}
