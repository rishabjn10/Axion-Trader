/**
 * @fileoverview Main dashboard page — symmetric grid layout.
 *
 * Layout:
 *   Row 1 — Agent Status (full width, 3-column internal layout)
 *   Row 2 — 4 equal cols: Portfolio Value | Total Return | Today PnL | Mode Toggle
 *   Row 3 — Metrics bar (full width, 4 equal items)
 *   Row 4 — Price Chart (2/3) + Indicator Panel (1/3)
 *   Row 5 — Trade Log (full width)
 *
 * Rows 2–3 use the same 4-column grid so vertical edges align.
 *
 * @component Dashboard
 * @returns {JSX.Element}
 */

import React from 'react'
import AgentStatus from '../components/AgentStatus.jsx'
import PnLCard from '../components/PnLCard.jsx'
import MetricsBar from '../components/MetricsBar.jsx'
import TradeLog from '../components/TradeLog.jsx'
import PriceChart from '../components/PriceChart.jsx'
import IndicatorPanel from '../components/IndicatorPanel.jsx'
import ModeToggle from '../components/ModeToggle.jsx'

export default function Dashboard() {
  return (
    <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6 space-y-6">

      {/* Row 1: Agent status — full width 3-column card */}
      <AgentStatus />

      {/* Row 2: 3 PnL cards + Mode Toggle — 4 equal columns */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-6">
        <PnLCard />
        <ModeToggle />
      </div>

      {/* Row 3: Metrics bar — full width, 4 internal items matching row 2 cols */}
      <MetricsBar />

      {/* Row 4: Price chart (2/3) + Indicator panel (1/3) */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 items-stretch">
        <div className="lg:col-span-2 flex flex-col">
          <PriceChart />
        </div>
        <div className="lg:col-span-1 flex flex-col">
          <IndicatorPanel />
        </div>
      </div>

      {/* Row 5: Trade log — full width */}
      <TradeLog />

    </div>
  )
}
