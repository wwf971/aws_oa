// main page build target. served under /main/, loaded only after login.
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  root: 'src/main',
  base: '/main/',
  plugins: [react()],
  resolve: {
    // @wwf971/react-comp-misc is symlinked and served from source; without
    // dedupe, its imports of react/mobx resolve to its own node_modules,
    // putting two copies in the bundle (two reacts -> hooks crash).
    dedupe: ['react', 'react-dom', 'mobx', 'mobx-react-lite'],
  },
  build: {
    outDir: '../../dist/main',
    emptyOutDir: true,
  },
})
