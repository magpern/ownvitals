import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './App'
import { initTheme } from './theme/theme'
import './styles/fonts.css'
import './styles/tokens.css'
import './styles/app.css'
import './styles/primitives.css'
import './styles/shell.css'
import './styles/auth.css'

const root = document.getElementById('root')
if (!root) throw new Error('Missing #root element in index.html')

// The theme goes on <html> before the first render, so the app is never painted in the wrong
// one. It is a module, not an inline script: the CSP allows 'self' only.
initTheme()

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
