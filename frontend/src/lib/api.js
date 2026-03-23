/**
 * @fileoverview Axios instance configured for axion-trader API calls.
 *
 * The baseURL is empty — Vite's dev proxy routes /api/* to the backend.
 * In production, configure your reverse proxy similarly.
 *
 * Features:
 * - 10 second request timeout
 * - Response interceptor logging errors with status code and URL
 * - Named export for use across hook files
 */

import axios from 'axios'

/**
 * Pre-configured Axios instance for all API calls.
 *
 * @type {import('axios').AxiosInstance}
 *
 * @example
 * import { api } from '../lib/api.js'
 * const response = await api.get('/api/state')
 */
export const api = axios.create({
  baseURL: '',          // Vite proxy handles /api → http://localhost:8000
  timeout: 10_000,      // 10 second timeout for all requests
  headers: {
    'Content-Type': 'application/json',
    'Accept': 'application/json',
  },
})

// ── Response interceptor — log errors to console ──────────────────────────────
// This allows React Query to still receive the error (it re-throws),
// while ensuring every API failure is visible in the browser console.
api.interceptors.response.use(
  (response) => response,
  (error) => {
    const status = error.response?.status
    const url = error.config?.url
    const message = error.response?.data?.detail || error.message

    console.error(
      `[axion-trader API] ${status || 'Network'} error on ${url}: ${message}`
    )

    // Re-throw so React Query catches it and updates isError state
    return Promise.reject(error)
  }
)
