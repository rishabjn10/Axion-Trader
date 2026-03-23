/**
 * @fileoverview BTC/USD price chart — fetches real OHLCV data from /api/ohlcv.
 * Shows live price history from the first render with buy/sell trade markers.
 *
 * @component PriceChart
 * @returns {JSX.Element} Responsive line chart with trade markers.
 */

import React, { useMemo } from 'react'
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from 'recharts'
import { useQuery } from '@tanstack/react-query'
import { parseISO } from 'date-fns'
import { useTrades } from '../hooks/useTrades.js'
import { api } from '../lib/api.js'
import { formatTimestamp, formatCurrency } from '../lib/utils.js'

/**
 * Fetch OHLCV candles from the backend /api/ohlcv endpoint.
 * @returns {Promise<Array>}
 */
async function fetchOHLCV() {
  const response = await api.get('/api/ohlcv', { params: { interval: 60, limit: 72 } })
  return response.data
}

/**
 * Custom Recharts tooltip — shows time, price, high, low.
 */
function ChartTooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null
  const d = payload[0]?.payload || {}
  return (
    <div className="bg-[#1a1a2e] border border-[#2d2d4e] rounded-lg px-3 py-2 text-xs shadow-xl space-y-0.5">
      <p className="text-slate-400 mb-1">{label}</p>
      <p className="font-mono font-semibold text-cyan-400">
        Close: {formatCurrency(d.price)}
      </p>
      {d.high != null && (
        <p className="font-mono text-slate-400">H: {formatCurrency(d.high)}</p>
      )}
      {d.low != null && (
        <p className="font-mono text-slate-400">L: {formatCurrency(d.low)}</p>
      )}
      {d.tradeAction && (
        <p className={`font-bold mt-1 ${d.tradeAction === 'buy' ? 'text-green-400' : 'text-red-400'}`}>
          {d.tradeAction === 'buy' ? '▲ BUY executed' : '▼ SELL executed'}
        </p>
      )}
    </div>
  )
}

/**
 * Custom dot renderer — shows coloured markers only on candles where a trade executed.
 */
function TradeDot({ cx, cy, payload }) {
  if (!payload?.tradeAction) return null
  const isBuy = payload.tradeAction === 'buy'
  const color = isBuy ? '#22c55e' : '#ef4444'
  const label = isBuy ? '▲' : '▼'
  return (
    <g>
      <circle cx={cx} cy={cy} r={6} fill={color} stroke="#1a1a2e" strokeWidth={1.5} />
      <text
        x={cx}
        y={isBuy ? cy - 12 : cy + 18}
        textAnchor="middle"
        fill={color}
        fontSize={9}
        fontWeight="bold"
      >
        {label}
      </text>
    </g>
  )
}

/**
 * Price chart component — always shows real OHLCV data, trade markers overlay.
 *
 * @component PriceChart
 * @returns {JSX.Element}
 */
export default function PriceChart() {
  const { data: ohlcv = [], isLoading } = useQuery({
    queryKey: ['ohlcv'],
    queryFn: fetchOHLCV,
    refetchInterval: 60_000,
    placeholderData: (prev) => prev,
    retry: 2,
  })

  const { data: trades = [] } = useTrades(30)

  // Merge OHLCV candles with trade markers — match trade to nearest 1h candle
  const chartData = useMemo(() => {
    if (!ohlcv.length) return []

    return ohlcv.map((candle) => {
      const candleMs = new Date(candle.time).getTime()
      const matchedTrade = trades.find(
        (t) =>
          t.entry_price > 0 &&
          Math.abs(new Date(t.timestamp).getTime() - candleMs) < 3_600_000
      )
      return {
        time: formatTimestamp(candle.time),
        price: candle.price,
        high: candle.high,
        low: candle.low,
        tradeAction: matchedTrade?.action || null,
      }
    })
  }, [ohlcv, trades])

  // Y-axis domain with 1% padding
  const prices = chartData.map((d) => d.price).filter(Boolean)
  const yMin = prices.length ? Math.min(...prices) * 0.999 : 'auto'
  const yMax = prices.length ? Math.max(...prices) * 1.001 : 'auto'

  const tradeCount = chartData.filter((d) => d.tradeAction).length

  return (
    <div className="card p-6 h-full flex flex-col">
      <div className="flex items-center justify-between mb-4">
        <div>
          <h3 className="text-sm font-semibold text-white">Price Chart</h3>
          <p className="text-xs text-slate-500 mt-0.5">
            72h · 1h candles
            {tradeCount > 0 && (
              <span className="ml-2">
                · <span className="text-green-400">▲ Buy</span>{' '}
                <span className="text-red-400">▼ Sell</span> markers
              </span>
            )}
          </p>
        </div>
        <div className="flex items-center gap-1 text-xs text-slate-500">
          <span className="w-3 h-0.5 bg-cyan-400 inline-block rounded" />
          <span>Price (close)</span>
        </div>
      </div>

      <div className="flex-1 min-h-0">
      {isLoading && !chartData.length ? (
        <div className="flex items-center justify-center h-full text-slate-500 text-sm animate-pulse">
          Loading price history…
        </div>
      ) : (
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={chartData} margin={{ top: 8, right: 10, left: 0, bottom: 5 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#2d2d4e" vertical={false} />
            <XAxis
              dataKey="time"
              tick={{ fill: '#64748b', fontSize: 10 }}
              tickLine={false}
              axisLine={false}
              interval={11}
            />
            <YAxis
              domain={[yMin, yMax]}
              tick={{ fill: '#64748b', fontSize: 10 }}
              tickLine={false}
              axisLine={false}
              tickFormatter={(v) => `$${(v / 1000).toFixed(1)}k`}
              width={52}
            />
            <Tooltip content={<ChartTooltip />} />
            <Line
              type="monotone"
              dataKey="price"
              stroke="#00d4ff"
              strokeWidth={1.5}
              dot={<TradeDot />}
              activeDot={{ r: 4, fill: '#00d4ff' }}
              connectNulls
              name="Price"
            />
          </LineChart>
        </ResponsiveContainer>
      )}
      </div>
    </div>
  )
}
