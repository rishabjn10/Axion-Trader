/**
 * @fileoverview Scrollable trade log table with expandable AI reasoning rows.
 *
 * @component TradeLog
 * @param {{ limit?: number }} props
 * @returns {JSX.Element} Scrollable table of recent trades.
 */

import React, { useState } from 'react'
import clsx from 'clsx'
import { ChevronDown, ChevronRight } from 'lucide-react'
import { useTrades } from '../hooks/useTrades.js'
import {
  formatCurrency,
  formatPct,
  formatTimestamp,
  getActionColor,
  getPnLColor,
} from '../lib/utils.js'

/**
 * Status badge for trade status field.
 *
 * @param {{ status: string }} props
 * @returns {JSX.Element}
 */
function StatusBadge({ status }) {
  const config = {
    open:   'text-cyan-400 bg-cyan-400/10 border-cyan-400/30',
    closed: 'text-slate-400 bg-slate-400/10 border-slate-400/30',
    failed: 'text-red-400 bg-red-400/10 border-red-400/30',
  }
  return (
    <span className={clsx('px-1.5 py-0.5 rounded text-xs border', config[status] || 'text-slate-400 bg-slate-700')}>
      {status?.toUpperCase()}
    </span>
  )
}

/**
 * Individual trade row with expandable AI reasoning.
 *
 * @param {{ trade: object, isFirst: boolean }} props
 * @returns {JSX.Element}
 */
function TradeRow({ trade, isFirst }) {
  const [expanded, setExpanded] = useState(false)

  const pnlValue = trade.pnl_pct
  const pnlDisplay = pnlValue !== null ? formatPct(pnlValue) : '—'
  const pnlColour = getPnLColor(pnlValue)

  return (
    <>
      <tr
        className={clsx(
          'cursor-pointer transition-colors hover:bg-[#16213e]',
          // Live pulse for the most recent open trade
          isFirst && trade.status === 'open' && 'bg-cyan-400/5'
        )}
        onClick={() => setExpanded((e) => !e)}
      >
        <td className="px-4 py-3 text-xs text-slate-400 font-mono whitespace-nowrap">
          <div className="flex items-center gap-1.5">
            {/* Pulse dot for open trades */}
            {trade.status === 'open' && isFirst && (
              <span className="w-1.5 h-1.5 bg-cyan-400 rounded-full animate-pulse flex-shrink-0" />
            )}
            {formatTimestamp(trade.timestamp)}
          </div>
        </td>
        <td className="px-4 py-3">
          <span className={clsx('px-2 py-0.5 rounded-md text-xs font-bold border', getActionColor(trade.action))}>
            {trade.action?.toUpperCase()}
          </span>
        </td>
        <td className="px-4 py-3 text-sm text-slate-300 font-mono">{trade.pair}</td>
        <td className="px-4 py-3 text-sm font-mono text-slate-200 whitespace-nowrap">
          {formatCurrency(trade.entry_price)}
        </td>
        <td className="px-4 py-3 text-sm font-mono text-slate-400 whitespace-nowrap">
          {trade.exit_price ? formatCurrency(trade.exit_price) : '—'}
        </td>
        <td className={clsx('px-4 py-3 text-sm font-mono font-medium whitespace-nowrap', pnlColour)}>
          {pnlDisplay}
        </td>
        <td className="px-4 py-3">
          <StatusBadge status={trade.status} />
        </td>
        <td className="px-4 py-3 text-slate-500">
          {expanded ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
        </td>
      </tr>

      {/* Expandable AI reasoning row */}
      {expanded && (
        <tr className="bg-[#16213e]">
          <td colSpan={8} className="px-6 py-4">
            <div className="space-y-2">
              <p className="text-xs text-slate-500 uppercase tracking-wide">AI Reasoning</p>
              <p className="text-sm text-slate-300 leading-relaxed">
                {trade.llm_reasoning || 'No AI reasoning recorded for this trade.'}
              </p>
              {trade.stop_price > 0 && (
                <div className="flex flex-wrap gap-x-6 gap-y-1.5 text-xs font-mono text-slate-500 mt-3 pt-3 border-t border-[#2d2d4e]">
                  <span>
                    Stop Loss:{' '}
                    <span className="text-red-400 font-semibold">{formatCurrency(trade.stop_price)}</span>
                  </span>
                  {trade.take_profit_price > 0 && (
                    <span>
                      Target:{' '}
                      <span className="text-green-400 font-semibold">{formatCurrency(trade.take_profit_price)}</span>
                    </span>
                  )}
                  {trade.stop_price > 0 && trade.take_profit_price > 0 && trade.entry_price > 0 && (
                    <span>
                      R:R:{' '}
                      <span className="text-cyan-400 font-semibold">
                        {(Math.abs(trade.take_profit_price - trade.entry_price) / Math.abs(trade.entry_price - trade.stop_price)).toFixed(1)}:1
                      </span>
                    </span>
                  )}
                  <span>Volume: <span className="text-slate-300">{trade.volume?.toFixed(6)} BTC</span></span>
                  <span>Mode: <span className={trade.mode === 'live' ? 'text-green-400' : 'text-amber-400'}>{trade.mode?.toUpperCase()}</span></span>
                  {trade.closed_at && (
                    <span>Closed: <span className="text-slate-300">{new Date(trade.closed_at).toLocaleString()}</span></span>
                  )}
                  <span>Order ID: <span className="text-slate-400">{trade.order_id}</span></span>
                </div>
              )}
            </div>
          </td>
        </tr>
      )}
    </>
  )
}

/**
 * Scrollable trade log table showing recent trades.
 *
 * Columns: Time, Action, Pair, Entry, Exit, PnL, Status
 * Clicking a row expands it to show the full AI reasoning.
 *
 * @component TradeLog
 * @param {{ limit?: number }} props - limit defaults to 50.
 * @returns {JSX.Element} Trade log card with scrollable table.
 */
export default function TradeLog({ limit = 50, fullHeight = false }) {
  const { data: trades = [], isLoading } = useTrades(limit)

  return (
    <div className={clsx('card flex flex-col', fullHeight && 'flex-1 min-h-0')}>
      {/* Header */}
      <div className="px-6 py-4 border-b border-[#2d2d4e] flex items-center justify-between flex-shrink-0">
        <div>
          <h3 className="text-sm font-semibold text-white">Trade Log</h3>
          <p className="text-xs text-slate-500 mt-0.5">Click any row to see AI reasoning</p>
        </div>
        <span className="text-xs text-slate-500">{trades.length} trades</span>
      </div>

      {/* Scrollable table */}
      <div className={clsx('overflow-auto', fullHeight ? 'flex-1 min-h-0' : 'max-h-[400px]')}>
        {isLoading && trades.length === 0 ? (
          <div className="p-8 text-center text-slate-500 text-sm">Loading trades…</div>
        ) : trades.length === 0 ? (
          <div className="p-8 text-center text-slate-500 text-sm">
            No trades executed yet. The agent will trade when confluence and risk conditions are met.
          </div>
        ) : (
          <table className="w-full table-fixed">
            <colgroup>
              <col className="w-[18%]" />
              <col className="w-[10%]" />
              <col className="w-[12%]" />
              <col className="w-[15%]" />
              <col className="w-[15%]" />
              <col className="w-[12%]" />
              <col className="w-[12%]" />
              <col className="w-[6%]" />
            </colgroup>
            <thead className="sticky top-0 bg-[#1a1a2e] z-10">
              <tr className="border-b border-[#2d2d4e]">
                {['Time', 'Action', 'Pair', 'Entry', 'Exit', 'PnL', 'Status', ''].map((h) => (
                  <th key={h} className="px-4 py-2.5 text-left text-xs font-medium text-slate-500 uppercase tracking-wider">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-[#2d2d4e]">
              {trades.map((trade, i) => (
                <TradeRow key={trade.order_id} trade={trade} isFirst={i === 0} />
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
