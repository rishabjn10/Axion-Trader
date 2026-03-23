/**
 * @fileoverview React Query hook for polling the agent state endpoint.
 *
 * Polls /api/state every 5 seconds to keep the dashboard updated with
 * the current agent status, last decision, circuit breaker state, and
 * live price. Returns stale data on error to avoid dashboard flicker.
 */

import { useQuery } from '@tanstack/react-query'
import { api } from '../lib/api.js'

/**
 * Fetch current agent state from the API.
 *
 * @returns {Promise<object>} Agent state response from /api/state.
 */
async function fetchAgentState() {
  const response = await api.get('/api/state')
  return response.data
}

/**
 * Hook for polling agent state every 5 seconds.
 *
 * @returns {{
 *   data: object|undefined,
 *   isLoading: boolean,
 *   isError: boolean,
 *   refetch: function
 * }} React Query result with agent state data.
 *
 * @example
 * const { data: state, isLoading } = useAgentState()
 * if (state?.circuit_breaker_active) {
 *   // Show circuit breaker warning
 * }
 */
export function useAgentState() {
  return useQuery({
    queryKey: ['agentState'],
    queryFn: fetchAgentState,
    refetchInterval: 5_000,        // Poll every 5 seconds
    refetchIntervalInBackground: true,
    // Return stale data on error so the dashboard doesn't blank out
    placeholderData: (previousData) => previousData,
    onError: (error) => {
      // Error already logged by axios interceptor — no need to re-log here
      console.warn('[useAgentState] Error:', error.message)
    },
  })
}
