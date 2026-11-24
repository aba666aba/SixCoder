import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 渲染进程按普通前端构建；主进程（electron/main.js）由 electron 直接跑，
// 不进 vite 打包（保持简单，避免 electron 与 vite 的 main/preload 打包复杂度）。
export default defineConfig({
  plugins: [react()],
  base: './',
  build: { outDir: 'dist', emptyOutDir: true },
})
