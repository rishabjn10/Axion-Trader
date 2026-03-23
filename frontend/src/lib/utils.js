/**
 * @fileoverview Utility functions for formatting trading data for display.
 *
 * All formatters handle null/undefined/NaN gracefully, returning a safe
 * placeholder string rather than crashing the component.
 */

import { format, parseISO, isValid } from 'date-fns'
import clsx from 'clsx'

/**
 * Format a numeric value as a USD currency string.
 *
 * @param {number|null|undefined} value - The numeric value to format.
 * @param {number} [decimals=2] - Number of decimal places.
 * @returns {string} Formatted string like "$67,234.10" or "—" if invalid.
 *
 * @example
 * formatCurrency(67234.1)  // "$67,234.10"
 * formatCurrency(null)     // "—"
 * formatCurrency(1234.5, 0) // "$1,235"
 */
export function formatCurrency(value, decimals = 2) {
  if (value === null || value === undefined || isNaN(value)) return '—'
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  }).format(value)
}

/**
 * Format a percentage value with explicit sign.
 *
 * @param {number|null|undefined} value - Percentage value (e.g. 1.34 for 1.34%).
 * @param {number} [decimals=2] - Decimal places.
 * @returns {string} Formatted string like "+1.34%" or "-2.50%" or "—".
 *
 * @example
 * formatPct(1.34)   // "+1.34%"
 * formatPct(-2.5)   // "-2.50%"
 * formatPct(0)      // "+0.00%"
 */
export function formatPct(value, decimals = 2) {
  if (value === null || value === undefined || isNaN(value)) return '—'
  const sign = value >= 0 ? '+' : ''
  return `${sign}${value.toFixed(decimals)}%`
}

/**
 * Format an ISO 8601 timestamp string for display.
 *
 * @param {string|null|undefined} isoString - ISO 8601 timestamp string.
 * @returns {string} Formatted string like "Mar 22, 14:32" or "—" if invalid.
 *
 * @example
 * formatTimestamp('2024-03-22T14:32:00Z')  // "Mar 22, 14:32"
 * formatTimestamp(null)                     // "—"
 */
export function formatTimestamp(isoString) {
  if (!isoString) return '—'
  try {
    const date = parseISO(isoString)
    if (!isValid(date)) return '—'
    return format(date, 'MMM d, HH:mm')
  } catch {
    return '—'
  }
}

/**
 * Format an ISO 8601 timestamp as a short relative label.
 *
 * @param {string|null|undefined} isoString - ISO 8601 timestamp.
 * @returns {string} e.g. "Mar 22 14:32:05"
 */
export function formatTimestampFull(isoString) {
  if (!isoString) return '—'
  try {
    const date = parseISO(isoString)
    if (!isValid(date)) return '—'
    return format(date, 'MMM d HH:mm:ss')
  } catch {
    return '—'
  }
}

/**
 * Get Tailwind CSS colour classes for a trade action.
 *
 * @param {string} action - Trade action: 'buy', 'sell', or 'hold'.
 * @returns {string} Tailwind class string for text and background colours.
 *
 * @example
 * getActionColor('buy')   // 'text-green-400 bg-green-400/10'
 * getActionColor('sell')  // 'text-red-400 bg-red-400/10'
 * getActionColor('hold')  // 'text-slate-400 bg-slate-400/10'
 */
export function getActionColor(action) {
  switch (action?.toLowerCase()) {
    case 'buy':  return 'text-green-400 bg-green-400/10 border-green-400/30'
    case 'sell': return 'text-red-400 bg-red-400/10 border-red-400/30'
    default:     return 'text-slate-400 bg-slate-400/10 border-slate-400/30'
  }
}

/**
 * Get Tailwind CSS text colour class for a PnL value.
 *
 * @param {number|null|undefined} pnl - PnL value (positive = profit, negative = loss).
 * @returns {string} Tailwind text colour class.
 *
 * @example
 * getPnLColor(1.5)   // 'text-green-400'
 * getPnLColor(-0.5)  // 'text-red-400'
 */
export function getPnLColor(pnl) {
  if (pnl === null || pnl === undefined) return 'text-slate-400'
  return pnl >= 0 ? 'text-green-400' : 'text-red-400'
}

/**
 * Get Tailwind CSS text colour class for agent status.
 *
 * @param {string} status - Agent status string.
 * @returns {string} Tailwind text colour class.
 *
 * @example
 * getStatusColor('running')         // 'text-green-400'
 * getStatusColor('circuit_breaker') // 'text-red-400'
 */
export function getStatusColor(status) {
  switch (status?.toLowerCase()) {
    case 'running':         return 'text-green-400'
    case 'paused':          return 'text-amber-400'
    case 'halted':          return 'text-red-400'
    case 'circuit_breaker': return 'text-red-400'
    default:                return 'text-slate-400'
  }
}

/**
 * Format a countdown in seconds to a MM:SS or HH:MM:SS string.
 *
 * @param {number} seconds - Countdown value in seconds.
 * @returns {string} Formatted time string like "45:30" or "1:23:45".
 *
 * @example
 * formatCountdown(3600)  // "1:00:00"
 * formatCountdown(90)    // "1:30"
 */
export function formatCountdown(seconds) {
  if (!seconds || seconds < 0) return '—'
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = seconds % 60
  if (h > 0) {
    return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
  }
  return `${m}:${String(s).padStart(2, '0')}`
}
