/**
 * @fileoverview Application entry point — mounts React app with QueryClient and Router.
 *
 * Sets up:
 * - React 18 concurrent mode via createRoot
 * - TanStack React Query with sensible defaults for polling
 * - React Hot Toast for notifications
 * - Global CSS import
 */

import React from 'react'
import ReactDOM from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Toaster } from 'react-hot-toast'
import App from './App.jsx'
import './index.css'

// Configure React Query with polling-optimised defaults
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Don't retry on error — agent may be legitimately down
      retry: 1,
      retryDelay: 2000,
      // Keep stale data visible while refetching (no flicker)
      staleTime: 0,
      // Keep cached data for 5 minutes after component unmounts
      gcTime: 5 * 60 * 1000,
      // Refetch when window regains focus
      refetchOnWindowFocus: true,
    },
  },
})

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
      {/* Toast notifications — positioned top-right for trading alerts */}
      <Toaster
        position="top-right"
        toastOptions={{
          duration: 4000,
          style: {
            background: '#1a1a2e',
            color: '#e2e8f0',
            border: '1px solid #2d2d4e',
            borderRadius: '8px',
            fontSize: '14px',
          },
          success: {
            iconTheme: { primary: '#22c55e', secondary: '#0f0f1a' },
          },
          error: {
            iconTheme: { primary: '#ef4444', secondary: '#0f0f1a' },
          },
        }}
      />
    </QueryClientProvider>
  </React.StrictMode>,
)
