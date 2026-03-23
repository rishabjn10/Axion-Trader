import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

/**
 * Vite configuration for axion-trader React dashboard.
 *
 * The /api proxy forwards all API requests from the dev server (port 5173)
 * to the FastAPI backend (port 8000), avoiding CORS issues during development.
 * In production, configure your reverse proxy (nginx/caddy) to do the same.
 */
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        // Keep /api prefix — FastAPI routes are defined with /api prefix
      }
    }
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    rollupOptions: {
      output: {
        // Code-split by route for faster initial load
        manualChunks: {
          vendor: ['react', 'react-dom', 'react-router-dom'],
          charts: ['recharts'],
          query: ['@tanstack/react-query'],
        }
      }
    }
  }
})
