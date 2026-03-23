/**
 * @fileoverview Metrics bar — Sharpe, drawdown, win rate, trade count with tooltips.
 *
 * @component MetricsBar
 * @returns {JSX.Element}
 */

import React from 'react'
import clsx from 'clsx'
import { useMetrics } from '../hooks/useMetrics.js'
import InfoTooltip from './InfoTooltip.jsx'

function MetricItem({ label, tooltip, value, subvalue, valueClass }) {
  return (
    <div className="flex flex-col items-center text-center px-4 py-4 first:pl-6 last:pr-6">
      <div className="flex items-center justify-center mb-1">
        <p className="text-xs text-slate-500 uppercase tracking-wider">{label}</p>
        {tooltip && <InfoTooltip text={tooltip} />}
      </div>
      <p className={clsx('text-xl font-mono font-bold', valueClass)}>{value}</p>
      {subvalue && <p className="text-xs text-slate-500 mt-0.5">{subvalue}</p>}
    </div>
  )
}

function sharpeColor(sharpe) {
  if (sharpe > 1.5) return 'text-green-400'
  if (sharpe >= 0.5) return 'text-amber-400'
  return 'text-red-400'
}

export default function MetricsBar() {
  const { data: metrics } = useMetrics()

  const sharpe = metrics?.sharpe_ratio || 0
  const maxDrawdown = metrics?.max_drawdown_pct || 0
  const winRate = metrics?.win_rate_pct || 0
  const totalTrades = metrics?.total_trades || 0
  const openPositions = metrics?.open_positions || 0
  const closedTrades = totalTrades - openPositions

  return (
    <div className="card">
      <div className="flex items-center divide-x divide-[#2d2d4e]">
        <MetricItem
          label="Sharpe Ratio"
          tooltip="Risk-adjusted return. Annualised = (mean daily return ÷ std deviation) × √365. Above 1.5 is excellent, 0.5–1.5 is acceptable, below 0.5 means returns don't justify the risk taken. Needs at least 5 days of data to calculate."
          value={sharpe.toFixed(2)}
          subvalue="annualised"
          valueClass={sharpeColor(sharpe)}
        />
        <MetricItem
          label="Max Drawdown"
          tooltip="Largest peak-to-trough decline in portfolio value. Measures the worst-case loss from a portfolio high point to the subsequent low before a new high is reached. A key measure of downside risk — lower is better."
          value={`-${maxDrawdown.toFixed(2)}%`}
          subvalue="peak to trough"
          valueClass="text-red-400"
        />
        <MetricItem
          label="Win Rate"
          tooltip="Percentage of closed trades that were profitable. Above 55% is strong for a trend-following system. Note: win rate alone doesn't determine profitability — a 40% win rate with large winners can outperform a 70% win rate with small gains."
          value={`${winRate.toFixed(1)}%`}
          subvalue={`${metrics?.winning_trades || 0}W / ${metrics?.losing_trades || 0}L`}
          valueClass={winRate > 55 ? 'text-green-400' : winRate > 45 ? 'text-amber-400' : 'text-red-400'}
        />
        <MetricItem
          label="Total Trades"
          tooltip="Total orders placed since the agent started. Includes open positions (not yet closed), closed positions (fully settled with PnL recorded), and failed orders (CLI errors or risk rejections)."
          value={String(totalTrades)}
          subvalue={`${openPositions} open · ${closedTrades} closed`}
          valueClass="text-white"
        />
      </div>
    </div>
  )
}
