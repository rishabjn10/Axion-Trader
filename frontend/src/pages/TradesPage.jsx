/**
 * @fileoverview Full trade history page — shows extended trade log.
 *
 * @component TradesPage
 * @returns {JSX.Element} Full-page trade history with up to 200 entries.
 */

import React from 'react'
import TradeLog from '../components/TradeLog.jsx'

/**
 * Dedicated trade history page showing a larger trade log.
 *
 * @component TradesPage
 * @returns {JSX.Element} Full-width trade history table.
 */
export default function TradesPage() {
  return (
    <div className="flex-1 flex flex-col max-w-7xl w-full mx-auto px-4 sm:px-6 lg:px-8 py-6">
      <div className="mb-6">
        <h1 className="text-2xl font-bold text-white">Trade History</h1>
        <p className="text-slate-400 text-sm mt-1">
          Complete record of all executed trades with AI reasoning
        </p>
      </div>
      <TradeLog limit={200} fullHeight />
    </div>
  )
}
