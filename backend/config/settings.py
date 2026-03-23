"""
Configuration and settings module for axion-trader.

Loads all environment variables via Pydantic BaseSettings and validates them at
import time. If any required key is missing, a clear ValueError is raised before
the agent starts, preventing cryptic runtime failures.

Role in system: every other module imports ``settings`` from here rather than
reading os.environ directly, ensuring a single validated source of truth.

Dependencies: pydantic-settings, python-dotenv
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal

from loguru import logger
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Central configuration object loaded from .env and environment variables.

    All fields are validated at startup. Required fields (GEMINI_API_KEY,
    KRAKEN_API_KEY_READONLY, etc.) will raise ValueError if absent.

    Attributes:
        gemini_api_key: Google Gemini API key for LLM decisions.
        kraken_api_key_readonly: Kraken read-only key for market data queries.
        kraken_api_secret_readonly: Corresponding secret for read-only key.
        kraken_api_key_trading: Kraken trading key for order placement.
        kraken_api_secret_trading: Corresponding secret for trading key.
        trading_mode: 'paper' or 'live' — controls order execution path.
        trading_pair: The exchange pair to trade, e.g. 'BTCUSD'.
        max_position_pct: Maximum portfolio fraction per trade (0.0–1.0).
        confidence_threshold: Minimum AI confidence required to trade.
        stop_loss_pct: Stop-loss as a fraction of entry price.
        daily_loss_limit_pct: Circuit breaker triggers at this daily loss.
        max_open_positions: Hard cap on simultaneous open positions.
        api_host: FastAPI bind host.
        api_port: FastAPI bind port.
        cors_origins: Comma-separated allowed CORS origins.

    Example:
        >>> from backend.config.settings import settings
        >>> print(settings.trading_pair)
        'BTCUSD'
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── API keys ──────────────────────────────────────────────────────────────
    gemini_api_key: str = Field(
        description="Google Gemini API key for LLM-based trading decisions."
    )
    kraken_api_key_readonly: str = Field(
        description="Kraken read-only API key for querying market data and balances."
    )
    kraken_api_secret_readonly: str = Field(
        description="Secret for the Kraken read-only API key."
    )
    kraken_api_key_trading: str = Field(
        description="Kraken trading API key — used only in live mode for order placement."
    )
    kraken_api_secret_trading: str = Field(
        description="Secret for the Kraken trading API key."
    )
    # ── Trading configuration ─────────────────────────────────────────────────
    trading_mode: Literal["paper", "live"] = Field(
        default="paper",
        description="Execution mode: 'paper' uses --paper flag; 'live' places real orders.",
    )
    trading_pair: str = Field(
        default="BTCUSD",
        description="The Kraken trading pair symbol, e.g. 'BTCUSD' or 'ETHUSD'.",
    )
    max_position_pct: float = Field(
        default=0.05,
        ge=0.001,
        le=0.5,
        description="Maximum fraction of portfolio value allocated per single trade.",
    )
    confidence_threshold: float = Field(
        default=0.75,
        ge=0.0,
        le=1.0,
        description="Minimum combined AI confidence score required to approve a trade.",
    )
    stop_loss_pct: float = Field(
        default=0.03,
        ge=0.001,
        le=0.5,
        description="Stop-loss distance as a fraction of entry price.",
    )
    daily_loss_limit_pct: float = Field(
        default=0.08,
        ge=0.01,
        le=1.0,
        description="Daily loss limit as a fraction of starting portfolio value. Circuit breaker activates at this threshold.",
    )
    max_open_positions: int = Field(
        default=2,
        ge=1,
        le=10,
        description="Maximum number of simultaneously open positions across all pairs.",
    )

    # ── Cycle intervals (override for testing) ────────────────────────────────
    fast_loop_minutes: int = Field(
        default=15,
        ge=1,
        le=1440,
        description="Fast loop interval in minutes (rule engine only). Set to 1 for testing.",
    )
    standard_loop_minutes: int = Field(
        default=60,
        ge=1,
        le=1440,
        description="Standard loop interval in minutes (full AI cycle). Set to 5 for testing.",
    )
    trend_loop_minutes: int = Field(
        default=240,
        ge=1,
        le=1440,
        description="Trend loop interval in minutes (regime refresh). Set to 15 for testing.",
    )

    # ── API server ────────────────────────────────────────────────────────────
    api_host: str = Field(
        default="0.0.0.0",
        description="Host address for the FastAPI server to bind to.",
    )
    api_port: int = Field(
        default=8000,
        ge=1024,
        le=65535,
        description="TCP port for the FastAPI server.",
    )
    cors_origins: str = Field(
        default="http://localhost:5173",
        description="Comma-separated list of allowed CORS origins for the API.",
    )

    # ── Indicator constants ───────────────────────────────────────────────────
    # These are not configurable via .env — they are fixed algorithmic parameters.
    TIMEFRAMES: list[int] = Field(
        default=[15, 60, 240],
        description="OHLCV fetch intervals in minutes: 15m, 1h, 4h.",
    )
    RSI_PERIOD: int = Field(default=14, description="RSI lookback period in candles.")
    MACD_FAST: int = Field(default=12, description="MACD fast EMA period.")
    MACD_SLOW: int = Field(default=26, description="MACD slow EMA period.")
    MACD_SIGNAL: int = Field(default=9, description="MACD signal line smoothing period.")
    BB_PERIOD: int = Field(default=20, description="Bollinger Bands SMA period.")
    ATR_PERIOD: int = Field(default=14, description="ATR calculation period.")
    EMA_FAST: int = Field(default=9, description="Fast EMA period for crossover signals.")
    EMA_SLOW: int = Field(default=21, description="Slow EMA period for crossover signals.")

    @field_validator("trading_pair")
    @classmethod
    def validate_trading_pair(cls, v: str) -> str:
        """Normalise trading pair to uppercase and strip whitespace."""
        return v.upper().strip()

    @field_validator("cors_origins")
    @classmethod
    def validate_cors_origins(cls, v: str) -> str:
        """Ensure CORS origins string is not empty."""
        if not v.strip():
            raise ValueError("CORS_ORIGINS must not be empty")
        return v

    @model_validator(mode="after")
    def validate_live_mode_keys(self) -> "Settings":
        """
        In live mode, both trading key and secret must be non-placeholder values.

        Raises:
            ValueError: If live mode is selected but trading credentials are
                        missing or still set to example placeholder values.
        """
        if self.trading_mode == "live":
            placeholder_patterns = {"your_", "placeholder", "example", ""}
            for key_name, key_val in [
                ("KRAKEN_API_KEY_TRADING", self.kraken_api_key_trading),
                ("KRAKEN_API_SECRET_TRADING", self.kraken_api_secret_trading),
            ]:
                if any(key_val.lower().startswith(p) for p in placeholder_patterns):
                    raise ValueError(
                        f"LIVE mode requires a real {key_name}. "
                        f"Current value looks like a placeholder. "
                        f"Set TRADING_MODE=paper or provide real credentials."
                    )
        return self

    @property
    def is_paper_mode(self) -> bool:
        """Return True when operating in paper (simulated) trading mode."""
        return self.trading_mode == "paper"

    @property
    def is_live_mode(self) -> bool:
        """Return True when operating in live (real money) trading mode."""
        return self.trading_mode == "live"

    @property
    def cors_origins_list(self) -> list[str]:
        """Return CORS origins as a parsed list of strings."""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def db_path(self) -> Path:
        """Absolute path to the SQLite database file."""
        return Path(__file__).parent.parent / "data" / "trading.db"


def _load_settings() -> Settings:
    """
    Load and validate settings, exiting with a clear error if validation fails.

    Returns:
        Settings: Validated settings singleton.
    """
    try:
        s = Settings()  # type: ignore[call-arg]
        return s
    except Exception as exc:
        # Print to stderr and exit — we cannot import loguru yet if settings fail
        print(f"\n[axion-trader] CONFIGURATION ERROR: {exc}", file=sys.stderr)
        print(
            "[axion-trader] Copy .env.example to .env and fill in all required values.",
            file=sys.stderr,
        )
        sys.exit(1)


# Module-level singleton — imported by all other modules
settings: Settings = _load_settings()
