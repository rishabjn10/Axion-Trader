/**
 * @fileoverview Paper/Live mode toggle with confirmation dialog for live mode.
 *
 * @component ModeToggle
 * @returns {JSX.Element} Mode switch card with confirmation dialog.
 */

import React, { useState } from 'react'
import clsx from 'clsx'
import { AlertTriangle, Shield } from 'lucide-react'
import toast from 'react-hot-toast'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '../lib/api.js'
import { useAgentState } from '../hooks/useAgentState.js'

/**
 * Confirmation dialog for switching to live mode.
 *
 * @param {{ onConfirm: function, onCancel: function }} props
 * @returns {JSX.Element}
 */
function LiveModeConfirmDialog({ onConfirm, onCancel }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/70" onClick={onCancel} />

      {/* Dialog */}
      <div className="relative bg-[#1a1a2e] border border-red-500/30 rounded-xl p-6 max-w-sm w-full mx-4 shadow-2xl">
        <div className="flex items-center gap-3 mb-4">
          <div className="p-2 bg-red-500/20 rounded-lg">
            <AlertTriangle className="w-6 h-6 text-red-400" />
          </div>
          <div>
            <h3 className="text-white font-semibold">Switch to LIVE Mode</h3>
            <p className="text-xs text-slate-400">Real money execution</p>
          </div>
        </div>

        <div className="bg-red-500/10 border border-red-500/20 rounded-lg p-3 mb-5">
          <p className="text-red-300 text-sm leading-relaxed">
            <strong>Warning:</strong> This will execute real trades with real money on Kraken.
            All orders will be placed using your live trading API key.
            Losses are real and irreversible.
          </p>
        </div>

        <div className="flex gap-3">
          <button
            onClick={onCancel}
            className="flex-1 px-4 py-2.5 rounded-lg border border-[#2d2d4e] text-slate-300 text-sm font-medium hover:bg-[#16213e] transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            className="flex-1 px-4 py-2.5 rounded-lg bg-red-500 hover:bg-red-600 text-white text-sm font-semibold transition-colors"
          >
            Confirm LIVE
          </button>
        </div>
      </div>
    </div>
  )
}

/**
 * Paper/Live mode toggle card.
 *
 * Shows current mode and a toggle switch. Switching to live mode requires
 * confirmation via a dialog. Calls POST /api/mode and invalidates state cache.
 *
 * @component ModeToggle
 * @returns {JSX.Element} Mode toggle card.
 */
export default function ModeToggle() {
  const { data: agentState } = useAgentState()
  const [showConfirm, setShowConfirm] = useState(false)
  const [isChanging, setIsChanging] = useState(false)
  const queryClient = useQueryClient()

  const currentMode = agentState?.mode || 'paper'
  const isPaper = currentMode === 'paper'

  /**
   * Handle the toggle click — if switching to live, show confirmation.
   */
  const handleToggle = () => {
    if (isPaper) {
      // Switching paper → live: require confirmation
      setShowConfirm(true)
    } else {
      // Switching live → paper: no confirmation needed
      changeMode('paper')
    }
  }

  /**
   * Execute the mode change API call.
   *
   * @param {'paper'|'live'} mode
   */
  const changeMode = async (mode) => {
    setIsChanging(true)
    setShowConfirm(false)
    try {
      const response = await api.post('/api/mode', { mode })
      if (response.data.success) {
        // Invalidate all queries so dashboard refreshes with new mode
        queryClient.invalidateQueries()
        toast.success(`Switched to ${mode.toUpperCase()} mode`, {
          icon: mode === 'paper' ? '📄' : '🔴',
        })
      }
    } catch (err) {
      const msg = err.response?.data?.detail || err.message
      toast.error(`Mode change failed: ${msg}`)
    } finally {
      setIsChanging(false)
    }
  }

  return (
    <>
      <div className="card p-6 h-full">
        <div className="flex items-center gap-2 mb-4">
          <Shield className="w-4 h-4 text-slate-400" />
          <span className="text-sm font-medium text-slate-400">Trading Mode</span>
        </div>

        {/* Current mode display */}
        <div className={clsx(
          'rounded-lg p-4 mb-4 border',
          isPaper
            ? 'bg-amber-400/10 border-amber-400/20'
            : 'bg-green-400/10 border-green-400/20'
        )}>
          <p className={clsx(
            'text-2xl font-bold font-mono',
            isPaper ? 'text-amber-400' : 'text-green-400'
          )}>
            {currentMode.toUpperCase()}
          </p>
          <p className="text-xs text-slate-400 mt-1">
            {isPaper
              ? 'Simulated orders — no real money'
              : 'Live orders — real money execution'}
          </p>
        </div>

        {/* Toggle button */}
        <button
          onClick={handleToggle}
          disabled={isChanging}
          className={clsx(
            'w-full py-2.5 rounded-lg text-sm font-semibold transition-all',
            isChanging && 'opacity-50 cursor-not-allowed',
            isPaper
              ? 'bg-[#16213e] border border-[#2d2d4e] text-slate-300 hover:border-amber-400/50'
              : 'bg-[#16213e] border border-[#2d2d4e] text-slate-300 hover:border-green-400/50'
          )}
        >
          {isChanging ? 'Switching…' : isPaper ? 'Switch to LIVE ⚡' : 'Switch to PAPER'}
        </button>

        <p className="text-xs text-slate-600 mt-3 text-center">
          Takes effect on next agent cycle
        </p>
      </div>

      {/* Confirmation dialog */}
      {showConfirm && (
        <LiveModeConfirmDialog
          onConfirm={() => changeMode('live')}
          onCancel={() => setShowConfirm(false)}
        />
      )}
    </>
  )
}
