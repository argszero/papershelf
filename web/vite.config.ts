import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// 构建产物**直接落到 Python 包里**（`papershelf/static`），
// 由 FastAPI 同进程托管 —— 单一镜像、单一进程，符合决策⑨ 的"单体"目标形态。
// 开发态用 `vite dev`（下面的 proxy 把 /api 与资产路由转发给本地 uvicorn）。
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: '../src/papershelf/static',
    emptyOutDir: true,
    // MathML 由服务端渲染，前端不需要任何公式运行时
    sourcemap: false,
  },
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8000',
      '/papers': 'http://127.0.0.1:8000',
      '/share': 'http://127.0.0.1:8000',
      '/activate': 'http://127.0.0.1:8000',
    },
  },
})
