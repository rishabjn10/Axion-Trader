/**
 * @fileoverview Live technical indicator panel with tooltips and confluence signal breakdown.
 */


import { useRef } from 'react'
import clsx from 'clsx'
import { useQuery } from '@tanstack/react-query'
import { api } from '../lib/api.js'
import { useAgentState } from '../hooks/useAgentState.js'
import InfoTooltip from './InfoTooltip.jsx'

async function fetchConfig() {
  const response = await api.get('/api/config')
  return response.data
}

async function fetchLatestDecision() {
  // Fetch enough to skip fast-loop records (timeframe=15, no llm_reasoning)
  const response = await api.get('/api/decisions', { params: { limit: 100 } })
  // Skip fast-loop records (timeframe=15); require rsi > 0 so partial/zero records are ignored
  const stdDecision = response.data.find((d) => d.timeframe !== 15 && d.rsi > 0)
  return stdDecision || null
}

function IndicatorWidget({ label, tooltip, value, badge, badgeClass, detail }) {
  return (
    <div className="bg-[#16213e] rounded-lg p-3 space-y-1.5">
      <div className="flex items-center">
        <p className="text-xs text-slate-500 uppercase tracking-wider">{label}</p>
        {tooltip && <InfoTooltip text={tooltip} />}
      </div>
      <div className="flex items-end justify-between">
        <p className="text-lg font-mono font-bold text-white">{value}</p>
        {badge && (
          <span className={clsx('text-xs px-1.5 py-0.5 rounded font-medium border', badgeClass)}>
            {badge}
          </span>
        )}
      </div>
      {detail && <p className="text-xs text-slate-500">{detail}</p>}
    </div>
  )
}

function ConfluenceWidget({ score, threshold = 3, max = 8 }) {
  const pct = (score / max) * 100
  const passes = score >= threshold
  const colour = passes ? 'bg-green-400' : score >= Math.floor(threshold / 2) ? 'bg-amber-400' : 'bg-red-400'

  return (
    <div className="bg-[#16213e] rounded-lg p-3 space-y-1.5">
      <div className="flex items-center">
        <p className="text-xs text-slate-500 uppercase tracking-wider">Confluence</p>
        <InfoTooltip
          text={`8 independent signals voted: RSI, MACD, Bollinger Bands, VWAP, EMA cross, Fear & Greed, news sentiment, and ADX. Score ≥${threshold}/8 required to unlock the AI decision cycle. Below ${threshold}, the agent skips the cycle entirely — no Gemini call, no trade.`}
          wide
        />
      </div>
      <div className="flex items-end justify-between">
        <p className="text-lg font-mono font-bold text-white">
          {score}<span className="text-sm text-slate-500">/{max}</span>
        </p>
        <span className={clsx(
          'text-xs px-1.5 py-0.5 rounded font-medium border',
          passes
            ? 'text-green-400 bg-green-400/10 border-green-400/30'
            : 'text-red-400 bg-red-400/10 border-red-400/30'
        )}>
          {passes ? 'PASSES' : 'BELOW'}
        </span>
      </div>
      <div className="w-full bg-[#2d2d4e] rounded-full h-1.5">
        <div
          className={clsx('h-1.5 rounded-full transition-all duration-300', colour)}
          style={{ width: `${pct}%` }}
        />
      </div>
      <p className="text-xs text-slate-500">Minimum {threshold}/8 to trade</p>
    </div>
  )
}

function RSIWidget({ rsi }) {
  const rsiVal = rsi || 0
  const badge = rsiVal < 30 ? 'OVERSOLD' : rsiVal > 70 ? 'OVERBOUGHT' : 'NEUTRAL'
  const badgeClass = rsiVal < 30
    ? 'text-green-400 bg-green-400/10 border-green-400/30'
    : rsiVal > 70
    ? 'text-red-400 bg-red-400/10 border-red-400/30'
    : 'text-slate-400 bg-slate-400/10 border-slate-400/30'

  return (
    <IndicatorWidget
      label="RSI (14)"
      tooltip="Relative Strength Index. Measures momentum on a 0–100 scale. Below 30 = oversold (potential buy signal). Above 70 = overbought (potential sell signal). Neutral zone 30–70. Uses a 14-candle lookback period."
      value={rsiVal.toFixed(1)}
      badge={badge}
      badgeClass={badgeClass}
      detail="Momentum oscillator"
    />
  )
}

/**
 * Parse a breakdown string like "RSI=33.6 (<35) → BULL" into direction + display label.
 */
function parseSignal(line) {
  const isBull = line.includes('→ BULL')
  const isBear = line.includes('→ BEAR')
  const direction = isBull ? 'bull' : isBear ? 'bear' : 'neutral'

  // Extract a short label from the start of the line
  let label = line
  if (line.startsWith('RSI')) label = 'RSI'
  else if (line.startsWith('MACD cross')) label = 'MACD cross'
  else if (line.startsWith('MACD')) label = 'MACD'
  else if (line.startsWith('BB')) label = 'Bollinger %B'
  else if (line.startsWith('Price') && line.includes('VWAP')) label = 'vs VWAP'
  else if (line.startsWith('EMA')) label = 'EMA cross'
  else if (line.startsWith('Fear')) label = 'Fear & Greed'
  else if (line.startsWith('News')) label = 'News sentiment'
  else if (line.startsWith('ADX')) label = 'ADX trend'

  // Extract the detail (everything after the label prefix, strip the → BULL/BEAR part)
  const detail = line
    .replace(/→ (BULL|BEAR)/g, '')
    .replace(/\(contrarian\)/g, '⟳')
    .trim()

  return { direction, label, detail }
}

function SignalRow({ line }) {
  const { direction, label, detail } = parseSignal(line)

  const dot = direction === 'bull'
    ? 'bg-green-400'
    : direction === 'bear'
    ? 'bg-red-400'
    : 'bg-slate-500'

  const labelColor = direction === 'bull'
    ? 'text-green-400'
    : direction === 'bear'
    ? 'text-red-400'
    : 'text-slate-400'

  const dirLabel = direction === 'bull' ? '▲ BULL' : direction === 'bear' ? '▼ BEAR' : '— NEUT'
  const dirClass = direction === 'bull'
    ? 'text-green-400 bg-green-400/10 border-green-400/20'
    : direction === 'bear'
    ? 'text-red-400 bg-red-400/10 border-red-400/20'
    : 'text-slate-500 bg-slate-500/10 border-slate-500/20'

  return (
    <div className="flex items-center gap-2 py-1.5 border-b border-[#2d2d4e] last:border-0">
      <span className={clsx('w-1.5 h-1.5 rounded-full flex-shrink-0', dot)} />
      <span className={clsx('text-xs font-medium w-24 flex-shrink-0', labelColor)}>{label}</span>
      <span className="text-xs text-slate-500 flex-1 truncate font-mono">{detail}</span>
      <span className={clsx('text-xs px-1.5 py-0.5 rounded border font-mono flex-shrink-0', dirClass)}>
        {dirLabel}
      </span>
    </div>
  )
}

function ConfluenceBreakdown({ breakdown }) {
  if (!breakdown || breakdown.length === 0) return null

  const bullCount = breakdown.filter(l => l.includes('→ BULL')).length
  const bearCount = breakdown.filter(l => l.includes('→ BEAR')).length
  const neutCount = breakdown.length - bullCount - bearCount

  return (
    <div className="mt-4 pt-4 border-t border-[#2d2d4e]">
      <div className="flex items-center justify-between mb-2">
        <p className="text-xs text-slate-500 uppercase tracking-wider">Signal Breakdown</p>
        <div className="flex items-center gap-2 text-xs font-mono">
          <span className="text-green-400">▲{bullCount}</span>
          <span className="text-red-400">▼{bearCount}</span>
          <span className="text-slate-500">—{neutCount}</span>
        </div>
      </div>
      <div className="space-y-0">
        {breakdown.map((line, i) => (
          <SignalRow key={i} line={line} />
        ))}
      </div>
    </div>
  )
}

export default function IndicatorPanel() {
  const { data: agentState } = useAgentState()
  const lastGoodDecision = useRef(null)

  const { data: config } = useQuery({
    queryKey: ['config'],
    queryFn: fetchConfig,
    staleTime: 60_000,   // config rarely changes — refetch every minute is enough
    refetchInterval: 60_000,
  })

  const { data: latestDecision } = useQuery({
    queryKey: ['latestDecision'],
    queryFn: fetchLatestDecision,
    refetchInterval: 5_000,
  })

  // Only advance to a new decision if it has real data — never regress to null/zeros
  if (latestDecision?.rsi > 0) lastGoodDecision.current = latestDecision
  const decision = lastGoodDecision.current

  const rsi = decision?.rsi ?? null
  const confluenceScore = decision?.confluence_score ?? 0
  const confluenceBreakdown = decision?.confluence_breakdown ?? []
  const macdCross = decision?.macd_cross || '-'
  const bbPosition = decision?.bb_position || '-'
  const lastPrice = agentState?.last_price || 0
  const regime = agentState?.regime || '—'

  const macdBadge = macdCross === 'bullish' ? 'BULLISH' : macdCross === 'bearish' ? 'BEARISH' : 'NEUTRAL'

  const bbClass = bbPosition === 'LOWER'
    ? 'text-green-400 bg-green-400/10 border-green-400/30'
    : bbPosition === 'UPPER'
    ? 'text-red-400 bg-red-400/10 border-red-400/30'
    : 'text-slate-400 bg-slate-400/10 border-slate-400/30'

  const regimeBadgeClass = regime === 'TRENDING_UP'
    ? 'text-green-400 bg-green-400/10 border-green-400/30'
    : regime === 'TRENDING_DOWN'
    ? 'text-red-400 bg-red-400/10 border-red-400/30'
    : 'text-amber-400 bg-amber-400/10 border-amber-400/30'

  return (
    <div className="card p-6 h-full flex flex-col">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-sm font-semibold text-white">Indicators</h3>
        <span className="text-xs text-slate-500">Updates every 5s</span>
      </div>

      <div className="grid grid-cols-2 gap-3">
        <RSIWidget rsi={rsi} />

        <IndicatorWidget
          label="MACD"
          tooltip="Moving Average Convergence Divergence. Tracks the relationship between a 12-period and 26-period EMA, smoothed by a 9-period signal line. A bullish cross (MACD line crosses above signal) suggests upward momentum. Bearish cross suggests downward momentum."
          value={macdBadge}
          badge={macdCross !== 'none' ? 'CROSS' : ''}
          badgeClass="text-cyan-400 bg-cyan-400/10 border-cyan-400/30"
          detail="12/26/9 EMA"
        />

        <IndicatorWidget
          label="Bollinger"
          tooltip="Bollinger Bands place a 20-period SMA in the middle with bands 2 standard deviations above and below. LOWER = price near the lower band (potential mean-reversion buy). UPPER = price near upper band (potential sell). MIDDLE = no edge."
          value={bbPosition}
          badge={bbPosition !== 'MIDDLE' ? bbPosition : ''}
          badgeClass={bbClass}
          detail="20-period, 2σ"
        />

        <ConfluenceWidget score={confluenceScore} threshold={config?.confluence_min_score ?? 3} />

        <IndicatorWidget
          label="Last Price"
          tooltip="Most recent BTC/USD price received from the Kraken WebSocket feed via the Shock Guard. Updates in real-time independent of the agent cycle — used for flash crash detection (3% drop in 5 min triggers emergency close)."
          value={lastPrice > 0 ? `$${lastPrice.toLocaleString('en-US', { maximumFractionDigits: 0 })}` : '—'}
          detail="From shock guard WS"
        />

        <IndicatorWidget
          label="Regime"
          tooltip="Market structure detected from multi-timeframe analysis. TRENDING UP/DOWN: 4h ADX >25 (strong trend) with 1h EMA slope direction. RANGING: ADX ≤25, no dominant trend. Affects which rules fire — momentum rules in trends, mean-reversion in ranging markets."
          value={regime.replace('_', ' ')}
          badge={regime !== '—' ? (regime === 'RANGING' ? 'RANGE' : regime === 'TRENDING_UP' ? '↑' : '↓') : ''}
          badgeClass={regimeBadgeClass}
          detail="Multi-TF ADX + EMA"
        />
      </div>

      <ConfluenceBreakdown breakdown={confluenceBreakdown} />

      {latestDecision && (
        <p className="text-xs text-slate-600 mt-3 text-right">
          Last cycle: {latestDecision.timestamp?.substring(0, 16).replace('T', ' ')} UTC
        </p>
      )}
    </div>
  )
}
