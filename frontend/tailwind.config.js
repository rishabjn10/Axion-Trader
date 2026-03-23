/** @type {import('tailwindcss').Config} */
export default {
  // Use 'class' strategy: add 'dark' class to <html> to enable dark mode
  // This allows programmatic control rather than relying on OS preference
  darkMode: 'class',
  content: [
    './index.html',
    './src/**/*.{js,jsx,ts,tsx}',
  ],
  theme: {
    extend: {
      colors: {
        // Custom brand colours for the trading dashboard
        background: {
          primary: '#0f0f1a',    // Main app background — deep dark purple-black
          secondary: '#1a1a2e',  // Card background
          tertiary: '#16213e',   // Elevated card background
          border: '#2d2d4e',     // Subtle border colour
        },
        accent: {
          cyan: '#00d4ff',       // Highlight colour for active states
          purple: '#7c3aed',     // Secondary accent
        },
        trade: {
          buy: '#22c55e',        // Green for buy/profit
          sell: '#ef4444',       // Red for sell/loss
          hold: '#6b7280',       // Gray for hold
          neutral: '#94a3b8',    // Light gray for neutral states
        },
        status: {
          running: '#22c55e',    // Green — agent is active
          paused: '#f59e0b',     // Amber — agent is paused
          halted: '#ef4444',     // Red — agent is halted
          circuit: '#dc2626',    // Deep red — circuit breaker active
        }
      },
      fontFamily: {
        // Monospace for numbers and prices
        mono: ['JetBrains Mono', 'Fira Code', 'Consolas', 'monospace'],
      },
      animation: {
        'pulse-slow': 'pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite',
        'flash': 'flash 1s ease-in-out infinite',
      },
      keyframes: {
        flash: {
          '0%, 100%': { opacity: '1' },
          '50%': { opacity: '0.3' },
        },
      },
    },
  },
  plugins: [],
}
