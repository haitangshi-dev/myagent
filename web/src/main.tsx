import React from 'react'
import ReactDOM from 'react-dom/client'
import '@fontsource/geist-sans/400.css'
import '@fontsource/geist-sans/500.css'
import '@fontsource/geist-sans/600.css'
import '@fontsource/geist-mono/400.css'
import '@fontsource/geist-mono/500.css'
import './index.css'
import App from './App'
import { ErrorBoundary } from './components/ErrorBoundary'
import { initMascotBridge } from './lib/mascotBridge'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </React.StrictMode>,
)

// Electron 壳下启动吉祥物状态桥接（浏览器直接打开时无 electronAPI 会自动跳过）
initMascotBridge()
