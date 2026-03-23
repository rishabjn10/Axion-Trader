/**
 * @fileoverview App layout shell with React Router and navigation.
 *
 * @component App
 * @returns {JSX.Element} The root application component with routing.
 *
 * Routes:
 *   /        — Main dashboard with all trading panels
 *   /trades  — Full trade history page
 *
 * Layout structure:
 *   - Top navigation bar with logo, links, mode badge, and connection status
 *   - Main content area rendered by React Router outlet
 */

import { BrowserRouter, Routes, Route, NavLink } from 'react-router-dom'
import { Activity, BarChart2, Zap } from 'lucide-react'
import clsx from 'clsx'
import { useAgentState } from './hooks/useAgentState.js'
import Dashboard from './pages/Dashboard.jsx'
import TradesPage from './pages/TradesPage.jsx'

/**
 * Navigation bar component with logo, links, mode badge, and connection dot.
 *
 * @component NavBar
 * @returns {JSX.Element} Fixed top navigation bar.
 */
function NavBar() {
  const { data: state, isError } = useAgentState()

  const mode = state?.mode || 'paper'
  const isConnected = !isError && !!state

  return (
    <nav className="fixed top-0 left-0 right-0 z-50 bg-[#0f0f1a]/95 backdrop-blur-sm border-b border-[#2d2d4e]">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <div className="flex items-center justify-between h-14">
          {/* Logo */}
          <div className="flex items-center gap-2">
            <Zap className="w-5 h-5 text-cyan-400" />
            <span className="font-bold text-white tracking-tight">
              axion<span className="text-cyan-400">-trader</span>
            </span>
          </div>

          {/* Navigation links */}
          <div className="flex items-center gap-6">
            <NavLink
              to="/"
              end
              className={({ isActive }) =>
                clsx(
                  'text-sm font-medium transition-colors flex items-center gap-1.5',
                  isActive ? 'text-cyan-400' : 'text-slate-400 hover:text-slate-200'
                )
              }
            >
              <Activity className="w-4 h-4" />
              Dashboard
            </NavLink>
            <NavLink
              to="/trades"
              className={({ isActive }) =>
                clsx(
                  'text-sm font-medium transition-colors flex items-center gap-1.5',
                  isActive ? 'text-cyan-400' : 'text-slate-400 hover:text-slate-200'
                )
              }
            >
              <BarChart2 className="w-4 h-4" />
              Trades
            </NavLink>
          </div>

          {/* Right side: mode badge + connection status */}
          <div className="flex items-center gap-3">
            {/* Trading mode badge */}
            <span
              className={clsx(
                'text-xs font-bold px-2.5 py-1 rounded-md tracking-wider uppercase',
                mode === 'paper'
                  ? 'bg-amber-500/20 text-amber-400 border border-amber-500/30'
                  : 'bg-green-500/20 text-green-400 border border-green-500/30'
              )}
            >
              {mode}
            </span>

            {/* Connection status dot */}
            <div className="flex items-center gap-1.5">
              <div
                className={clsx(
                  'w-2 h-2 rounded-full',
                  isConnected ? 'bg-green-400 animate-pulse' : 'bg-red-400'
                )}
              />
              <span className="text-xs text-slate-500">
                {isConnected ? 'Connected' : 'Disconnected'}
              </span>
            </div>
          </div>
        </div>
      </div>
    </nav>
  )
}

/**
 * Root App component — sets up routing and global layout.
 *
 * @component App
 * @returns {JSX.Element} BrowserRouter with navigation and page routes.
 */
export default function App() {
  return (
    <BrowserRouter>
      <div className="min-h-screen flex flex-col bg-[#0f0f1a]">
        <NavBar />
        {/* Offset content below fixed nav bar */}
        <main className="pt-14 flex-1 flex flex-col">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/trades" element={<TradesPage />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  )
}
