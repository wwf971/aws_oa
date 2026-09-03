// login page build target. served at the site root (/), kept small:
// no mobx, no component library, so the first visit loads fast.
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  root: 'src/login',
  base: '/',
  plugins: [react()],
  build: {
    outDir: '../../dist/login',
    emptyOutDir: true,
    // login assets get their own folder so they never collide with
    // main page assets in the web bucket
    assetsDir: 'login-assets',
  },
})
