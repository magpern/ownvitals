import { blocks, type Span } from './messageFormat'

// A message as text (ADR 0014 section 5): paragraphs, bulleted lists and bold only, every
// character a React text node. Links are not made clickable and no HTML or image is rendered.

function Spans({ spans }: { spans: Span[] }) {
  return (
    <>
      {spans.map((span, index) => (span.bold ? <strong key={index}>{span.text}</strong> : <span key={index}>{span.text}</span>))}
    </>
  )
}

export function MessageText({ content }: { content: string }) {
  return (
    <div className="ov-chat-text">
      {blocks(content).map((block, index) =>
        block.kind === 'list' ? (
          <ul key={index}>
            {block.items.map((item, n) => (
              <li key={n}>
                <Spans spans={item} />
              </li>
            ))}
          </ul>
        ) : (
          <p key={index}>
            <Spans spans={block.spans} />
          </p>
        ),
      )}
    </div>
  )
}
