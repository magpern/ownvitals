import { useId, type ReactNode } from 'react'

// A part of the overview: a heading, an optional link to the area's page and its tiles.

export function AreaSection({ title, link, children }: { title: string; link?: ReactNode; children: ReactNode }) {
  const id = useId()
  return (
    <section className="ov-area" aria-labelledby={id}>
      <div className="ov-between">
        <h2 id={id} className="ov-area__title">
          {title}
        </h2>
        {link}
      </div>
      <div className="ov-tiles">{children}</div>
    </section>
  )
}
