/**
 * @fileoverview React Query hook for polling the trades endpoint.
 *
 * Polls /api/trades every 10 seconds to keep the trade log updated.
 * Accepts a configurable limit parameter for the full trades page.
 */

import { useQuery } from '@tanstack/react-query'
import { api } from '../lib/api.js'

/**
 * Fetch recent trades from the API.
 *
 * @param {number} limit - Maximum number of trades to fetch.
 * @returns {Promise<Array>} Array of trade objects.
 */
async function fetchTrades(limit) {
  const response = await api.get('/api/trades', { params: { limit } })
  return response.data
}

/**
 * Hook for polling trade history every 10 seconds.
 *
 * @param {number} [limit=50] - Number of trades to fetch.
 * @returns {{
 *   data: Array|undefined,
 *   isLoading: boolean,
 *   isError: boolean,
 *   refetch: function
 * }} React Query result with trades array.
 *
 * @example
 * const { data: trades = [] } = useTrades()
 * const openTrades = trades.filter(t => t.status === 'open')
 */
export function useTrades(limit = 50) {
  return useQuery({
    queryKey: ['trades', limit],
    queryFn: () => fetchTrades(limit),
    refetchInterval: 10_000,
    refetchIntervalInBackground: true,
    placeholderData: (previousData) => previousData,
  })
}
