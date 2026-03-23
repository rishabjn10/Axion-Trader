/**
 * @fileoverview PnL cards — portfolio value, total return, today's PnL with tooltips.
 *
 * @component PnLCard
 * @returns {JSX.Element}
 */

import React from 'react'
import { TrendingUp, TrendingDown, DollarSign, Calendar } from 'lucide-react'
import clsx from 'clsx'
import { useMetrics } from '../hooks/useMetrics.js'
import { formatCurrency, formatPct, getPnLColor } from '../lib/utils.js'
import InfoTooltip from './InfoTooltip.jsx'

function MetricCard({ title, tooltip, value, subtitle, icon: Icon, trend }) {
  const trendColour = trend === null ? 'text-slate-400' : trend >= 0 ? 'text-green-400' : 'text-red-400'
  const TrendIcon = trend !== null && (trend >= 0 ? TrendingUp : TrendingDown)

  return (
    <div className="card p-6">
      <div className="flex items-start justify-between">
        <div className="space-y-1">
          <div className="flex items-center">
            <p className="text-xs text-slate-500 uppercase tracking-wider">{title}</p>
            {tooltip && <InfoTooltip text={tooltip} />}
          </div>
          <p className="text-2xl font-mono font-bold text-white">{value}</p>
          {subtitle && (
            <p className={clsx('text-sm font-medium flex items-center gap-1', trendColour)}>
              {TrendIcon && <TrendIcon className="w-3.5 h-3.5" />}
              {subtitle}
            </p>
          )}
        </div>
        <div className="p-2.5 bg-[#16213e] rounded-lg">
          <Icon className="w-5 h-5 text-cyan-400" />
        </div>
      </div>
    </div>
  )
}

export default function PnLCard() {
  const { data: metrics, isLoading } = useMetrics()

  if (isLoading && !metrics) {
    return (
      <>
        {[...Array(3)].map((_, i) => (
          <div key={i} className="card p-6 animate-pulse">
            <div className="h-4 bg-slate-700 rounded w-24 mb-3" />
            <div className="h-8 bg-slate-700 rounded w-36 mb-2" />
            <div className="h-4 bg-slate-700 rounded w-20" />
          </div>
        ))}
      </>
    )
  }

  const portfolioValue = metrics?.portfolio_value_usd || 0
  const totalPnlUsd = metrics?.total_pnl_usd || 0
  const totalPnlPct = metrics?.total_pnl_pct || 0
  const dailyPnlUsd = metrics?.daily_pnl_usd || 0
  const dailyPnlPct = metrics?.daily_pnl_pct || 0

  return (
    <>
      <MetricCard
        title="Portfolio Value"
        tooltip="Total estimated value of the account in USD. In paper mode this starts at $10,000 (the paper account's virtual balance). Updates after each agent cycle completes and saves a portfolio snapshot."
        value={formatCurrency(portfolioValue)}
        subtitle={totalPnlPct !== 0 ? `${formatPct(totalPnlPct)} all-time` : 'No change yet'}
        icon={DollarSign}
        trend={totalPnlPct}
      />
      <MetricCard
        title="Total Return"
        tooltip="Percentage gain or loss from the starting portfolio value to current value. Calculated as (current − initial) ÷ initial × 100. Includes both realised PnL from closed trades and unrealised PnL from open positions."
        value={formatPct(totalPnlPct)}
        subtitle={totalPnlUsd !== 0 ? `${formatCurrency(totalPnlUsd)} USD` : 'Starting position'}
        icon={TrendingUp}
        trend={totalPnlPct}
      />
      <MetricCard
        title="Today's PnL"
        tooltip="Profit or loss recorded in the most recent portfolio snapshot for today's date. Resets to zero at midnight UTC. Reflects only completed portfolio snapshot cycles — the agent must have run at least one full standard cycle today."
        value={formatCurrency(dailyPnlUsd)}
        subtitle={dailyPnlPct !== 0 ? formatPct(dailyPnlPct) : 'No trades today'}
        icon={Calendar}
        trend={dailyPnlPct}
      />
    </>
  )
}
