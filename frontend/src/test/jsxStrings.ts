import { readFileSync, readdirSync } from 'node:fs'
import { join, relative, sep } from 'node:path'
import ts from 'typescript'
import { SRC } from './sourceScan'

// The scanner behind src/i18n/hardcoded.test.ts: user-facing English written into a .tsx file
// instead of the catalogue (ADR 0015 section 9). It parses the file and reports
//   - text between JSX tags,
//   - a string literal shown as a JSX child (`{cond ? 'a' : 'b'}`, `{'a' + b}`), and
//   - a string literal in a user-facing attribute: aria-label, title, placeholder, alt, and the
//     `label` and `hint` props of the form components.
// Only text with a letter counts: punctuation, digits and spaces are layout. A call's arguments
// are not looked into, so `{t('auth.title')}` is fine.

export const USER_FACING_ATTRIBUTES = new Set(['aria-label', 'title', 'placeholder', 'alt', 'label', 'hint'])

export interface Finding {
  /** Relative to `src/`, with forward slashes. */
  file: string
  text: string
}

const LETTER = /\p{L}/u

function shown(text: string): string {
  return text.replace(/\s+/g, ' ').trim()
}

/** The string literals an expression can show as it is: through ?:, &&, ||, ??, + and parentheses. */
function literalsOf(node: ts.Node, out: string[]): void {
  if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
    out.push(node.text)
  } else if (ts.isTemplateExpression(node)) {
    out.push(node.head.text, ...node.templateSpans.map((span) => span.literal.text))
  } else if (ts.isParenthesizedExpression(node) || ts.isAsExpression(node) || ts.isNonNullExpression(node)) {
    literalsOf(node.expression, out)
  } else if (ts.isConditionalExpression(node)) {
    literalsOf(node.whenTrue, out)
    literalsOf(node.whenFalse, out)
  } else if (ts.isBinaryExpression(node)) {
    literalsOf(node.left, out)
    literalsOf(node.right, out)
  }
}

export function scanSource(file: string, text: string): Finding[] {
  const source = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
  const found: Finding[] = []
  const add = (value: string) => {
    if (LETTER.test(value)) found.push({ file, text: shown(value) })
  }
  const visit = (node: ts.Node) => {
    if (ts.isJsxText(node)) {
      add(node.text)
    } else if (ts.isJsxExpression(node) && node.expression !== undefined && !ts.isJsxAttribute(node.parent)) {
      const literals: string[] = []
      literalsOf(node.expression, literals)
      literals.forEach(add)
    } else if (ts.isJsxAttribute(node) && USER_FACING_ATTRIBUTES.has(node.name.getText(source))) {
      const literals: string[] = []
      const value = node.initializer
      if (value !== undefined && ts.isStringLiteral(value)) literals.push(value.text)
      else if (value !== undefined && ts.isJsxExpression(value) && value.expression !== undefined) {
        literalsOf(value.expression, literals)
      }
      literals.forEach(add)
    }
    ts.forEachChild(node, visit)
  }
  visit(source)
  return found
}

function sourceFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name)
    if (entry.isDirectory()) return entry.name === 'test' ? [] : sourceFiles(path)
    return /\.tsx$/.test(entry.name) && !/\.test\.tsx$/.test(entry.name) ? [path] : []
  })
}

/** Every finding in the shipped .tsx files under `src/` (tests and src/test/ are not shipped). */
export function scanAll(root: string = SRC): Finding[] {
  return sourceFiles(root)
    .sort()
    .flatMap((path) => scanSource(relative(root, path).split(sep).join('/'), readFileSync(path, 'utf8')))
}
