/**
 * @fileoverview Reusable info tooltip — shows a ⓘ icon that reveals explanatory
 * text on hover. Pure CSS/Tailwind, no library dependency.
 *
 * @component InfoTooltip
 * @param {{ text: string, wide?: boolean }} props
 * @returns {JSX.Element}
 */

import React from 'react'

/**
 * Small ⓘ icon with a hover tooltip containing explanatory text.
 *
 * @param {{ text: string, wide?: boolean }} props
 *   text  — tooltip copy, can include newlines
 *   wide  — if true, tooltip is wider (w-72 instead of w-56)
 * @returns {JSX.Element}
 */
export default function InfoTooltip({ text, wide = false }) {
  return (
    <div className="group relative inline-flex items-center ml-1 align-middle">
      {/* Trigger icon */}
      <svg
        className="w-3 h-3 text-slate-600 group-hover:text-slate-400 cursor-help transition-colors"
        viewBox="0 0 16 16"
        fill="currentColor"
        aria-hidden="true"
      >
        <path d="M8 1a7 7 0 1 0 0 14A7 7 0 0 0 8 1zm0 3a.875.875 0 1 1 0 1.75A.875.875 0 0 1 8 4zm.75 3v4.5h-1.5V7h1.5z" />
      </svg>

      {/* Tooltip bubble — appears above */}
      <div
        className={[
          'absolute z-50',
          wide ? 'w-72' : 'w-60',
          'bottom-full left-1/2 -translate-x-1/2 mb-2',
          'bg-[#0f0f23] border border-[#3d3d5e] rounded-lg p-3',
          'text-xs text-slate-300 leading-relaxed whitespace-normal',
          'shadow-2xl pointer-events-none',
          'invisible opacity-0 group-hover:visible group-hover:opacity-100',
          'transition-opacity duration-150',
        ].join(' ')}
      >
        {text}
        {/* Arrow */}
        <div className="absolute top-full left-1/2 -translate-x-1/2 border-4 border-transparent border-t-[#3d3d5e]" />
      </div>
    </div>
  )
}
