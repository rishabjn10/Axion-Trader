/**
 * @fileoverview Agent status panel — full-width 3-column layout.
 *
 * @component AgentStatus
 * @returns {JSX.Element} Agent status card with badge, last decision, price, countdown.
 */

import React, { useState, useEffect } from 'react'
import { Clock, TrendingUp, TrendingDown, Minus, Cpu, Radio, Shield } from 'lucide-react'
import clsx from 'clsx'
import { useAgentState } from '../hooks/useAgentState.js'
import { formatCurrency, formatCountdown, getActionColor } from '../lib/utils.js'

function StatusBadge({ status }) {
  const config = {
    running:         { label: 'RUNNING',         class: 'text-green-400 bg-green-400/10 border-green-400/30' },
    paused:          { label: 'PAUSED',           class: 'text-amber-400 bg-amber-400/10 border-amber-400/30' },
    halted:          { label: 'HALTED',           class: 'text-red-400 bg-red-400/10 border-red-400/30' },
    circuit_breaker: { label: 'CIRCUIT BREAKER',  class: 'text-red-400 bg-red-400/10 border-red-400/30 animate-pulse' },
  }
  const { label, class: cls } = config[status] || { label: status?.toUpperCase(), class: 'text-slate-400 bg-slate-400/10 border-slate-400/30' }
  return (
    <span className={clsx('px-3 py-1 rounded-md text-sm font-bold border tracking-wider', cls)}>
      {label}
    </span>
  )
}

function RegimeBadge({ regime }) {
  const config = {
    TRENDING_UP:   { label: 'TRENDING UP',   icon: TrendingUp,   cls: 'text-green-400 bg-green-400/10' },
    TRENDING_DOWN: { label: 'TRENDING DOWN', icon: TrendingDown, cls: 'text-red-400 bg-red-400/10' },
    RANGING:       { label: 'RANGING',       icon: Minus,        cls: 'text-amber-400 bg-amber-400/10' },
  }
  const { label, icon: Icon, cls } = config[regime] || { label: regime, icon: Minus, cls: 'text-slate-400 bg-slate-400/10' }
  return (
    <span className={clsx('flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium', cls)}>
      <Icon className="w-3 h-3" />
      {label}
    </span>
  )
}

function CountdownTimer({ seconds }) {
  const [remaining, setRemaining] = useState(seconds)
  useEffect(() => { setRemaining(seconds) }, [seconds])
  useEffect(() => {
    const interval = setInterval(() => setRemaining((p) => Math.max(0, p - 1)), 1000)
    return () => clearInterval(interval)
  }, [])
  return <span className="font-mono text-cyan-400 font-semibold">{formatCountdown(remaining)}</span>
}

export default function AgentStatus() {
  const { data: state, isLoading } = useAgentState()

  if (isLoading && !state) {
    return (
      <div className="card p-6 animate-pulse">
        <div className="h-8 bg-slate-700 rounded w-32 mb-4" />
        <div className="h-4 bg-slate-700 rounded w-48" />
      </div>
    )
  }

  const status        = state?.status || 'running'
  const mode          = state?.mode || 'paper'
  const pair          = state?.pair || 'BTCUSD'
  const regime        = state?.regime || 'UNKNOWN'
  const lastDecision  = state?.last_decision
  const nextCycle     = state?.next_cycle_in_seconds || 0
  const lastPrice     = state?.last_price || 0
  const cbActive      = state?.circuit_breaker_active
  const cbRecovery    = state?.circuit_breaker_recovery_time
  const shockActive   = state?.shock_guard_active

  return (
    <div className="card p-6">
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 items-start">

        {/* ── Left: Status + Pair ───────────────────────────────────────── */}
        <div className="space-y-3">
          <div className="flex items-center gap-2">
            <Cpu className="w-4 h-4 text-slate-400" />
            <span className="text-slate-400 text-sm font-medium">Agent Status</span>
            <span className={clsx(
              'ml-1 px-2 py-0.5 rounded text-xs font-bold tracking-wider border',
              mode === 'paper'
                ? 'text-amber-400 bg-amber-400/10 border-amber-400/30'
                : 'text-green-400 bg-green-400/10 border-green-400/30'
            )}>
              {mode.toUpperCase()}
            </span>
          </div>

          <StatusBadge status={status} />

          <div className="flex items-center gap-3 flex-wrap">
            <span className="text-2xl font-mono font-bold text-white">{pair}</span>
            <RegimeBadge regime={regime} />
          </div>

          {/* System health badges */}
          <div className="flex items-center gap-2 flex-wrap pt-1">
            <span className={clsx(
              'flex items-center gap-1 text-xs px-2 py-0.5 rounded border',
              shockActive
                ? 'text-green-400 bg-green-400/10 border-green-400/20'
                : 'text-slate-500 bg-slate-500/10 border-slate-500/20'
            )}>
              <Radio className="w-3 h-3" />
              WS {shockActive ? 'Live' : 'Off'}
            </span>
            <span className={clsx(
              'flex items-center gap-1 text-xs px-2 py-0.5 rounded border',
              cbActive
                ? 'text-red-400 bg-red-400/10 border-red-400/20'
                : 'text-slate-500 bg-slate-500/10 border-slate-500/20'
            )}>
              <Shield className="w-3 h-3" />
              {cbActive ? 'CB Active' : 'CB Clear'}
            </span>
          </div>

          {cbActive && cbRecovery && (
            <p className="text-xs text-red-400">
              Recovery: {cbRecovery.replace('T', ' ').substring(0, 16)} UTC
            </p>
          )}
        </div>

        {/* ── Center: Last Decision ─────────────────────────────────────── */}
        <div className="lg:border-x lg:border-[#2d2d4e] lg:px-6 space-y-2">
          <p className="text-xs text-slate-500 uppercase tracking-wide">Last Decision</p>

          {lastDecision ? (
            <div className="space-y-2">
              <div className="flex items-center gap-3 flex-wrap">
                <span className={clsx(
                  'px-2.5 py-0.5 rounded-md text-xs font-bold border',
                  getActionColor(lastDecision.action)
                )}>
                  {lastDecision.action?.toUpperCase()}
                </span>
                <span className="text-slate-300 text-sm font-mono">
                  {(lastDecision.confidence * 100).toFixed(0)}% confidence
                </span>
              </div>
              {lastDecision.reasoning && (
                <p className="text-slate-400 text-xs leading-relaxed line-clamp-3">
                  {lastDecision.reasoning}
                </p>
              )}
              <p className="text-slate-600 text-xs">
                {lastDecision.timestamp?.substring(0, 16).replace('T', ' ')} UTC
              </p>
            </div>
          ) : (
            <div className="space-y-1">
              <p className="text-slate-600 text-sm">Awaiting first cycle…</p>
              <p className="text-slate-700 text-xs">
                Agent will analyse indicators and make a decision on the next standard loop cycle.
              </p>
            </div>
          )}
        </div>

        {/* ── Right: Price + Countdown ──────────────────────────────────── */}
        <div className="space-y-4 lg:text-right">
          <div>
            <p className="text-xs text-slate-500 uppercase tracking-wide">Live Price</p>
            <p className="font-mono text-2xl font-bold text-white mt-0.5">
              {lastPrice > 0
                ? `$${lastPrice.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
                : '—'}
            </p>
          </div>

          <div>
            <p className="text-xs text-slate-500 uppercase tracking-wide flex items-center lg:justify-end gap-1">
              <Clock className="w-3 h-3" />
              Next cycle
            </p>
            <div className="text-2xl font-bold mt-0.5">
              <CountdownTimer seconds={nextCycle} />
            </div>
          </div>
        </div>

      </div>
    </div>
  )
}
