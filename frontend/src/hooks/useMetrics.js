/**
 * @fileoverview React Query hook for polling the metrics endpoint.
 *
 * Polls /api/metrics every 15 seconds — metrics change less frequently
 * than state or trades so a longer interval is appropriate.
 */

import { useQuery } from '@tanstack/react-query'
import { api } from '../lib/api.js'

/**
 * Fetch performance metrics from the API.
 *
 * @returns {Promise<object>} Metrics response object.
 */
async function fetchMetrics() {
  const response = await api.get('/api/metrics')
  return response.data
}

/**
 * Hook for polling portfolio metrics every 15 seconds.
 *
 * @returns {{
 *   data: object|undefined,
 *   isLoading: boolean,
 *   isError: boolean,
 *   refetch: function
 * }} React Query result with metrics data.
 *
 * @example
 * const { data: metrics } = useMetrics()
 * console.log(metrics?.sharpe_ratio)
 */
export function useMetrics() {
  return useQuery({
    queryKey: ['metrics'],
    queryFn: fetchMetrics,
    refetchInterval: 15_000,
    refetchIntervalInBackground: true,
    placeholderData: (previousData) => previousData,
  })
}
