import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Vite 构建产物输出到 web/dist，由后端 server/api.py 的 StaticFiles 托管。
export default defineConfig({
  plugins: [react()],
  base: './',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: false,
    target: 'es2020',
  },
  server: {
    port: 5173,
    // 开发态将 /api 代理到本地后端，避免跨域并复用 CORS 之外的最简路径
    proxy: {
      '/api': 'http://localhost:8000',
    },
  },
})
