import os as _os
RPC_URL = _os.environ.get("THEX_RPC_URL", "https://api.mainnet-beta.solana.com")
GECKO_BASE = "https://api.geckoterminal.com/api/v2"
DEX_BASE = "https://api.dexscreener.com"
SOL_MINT = "So11111111111111111111111111111111111111112"
DEX_PROGRAMS = {
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",
    "routeUGWgWzqBWFcrCfv8tritsqukccJPu3q5GPP3xS",
    "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB",
}
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
SLIPPAGE_PCT = 3.0
COPY_DELAY_SEC = 3
TIME_EXIT_SEC = 3600
SIZING_USD = 5.0
MAX_OPEN = 3
GECKO_CALL_GAP = 4.0
DEX_CALL_GAP = 0.3
RPC_TIMEOUT_SEC = 25.0
SURVIVAL_MIN_AGE = 2400
SURVIVAL_MAX_AGE = 6600
SURVIVAL_MIN_PRICE_RATIO = 0.75
SURVIVAL_MIN_LIQ = 1500.0
SURVIVAL_MIN_VOL5M = 400.0
GATE_MIN_WIN_RATE = 40.0
GATE_MIN_PF = 1.2
GATE_MAX_DD = 50.0
GATE_MIN_TRADES = 15
DISC_MIN_TRADES = 5
DISC_MAX_TRADES = 150
DATA_DIR = r"C:\Users\acer\trading\data"
REPORT_DIR = r"C:\Users\acer\trading\reports"
BACKTEST_DIR = r"C:\Users\acer\trading\data\backtest"

POOL_FEE_PCT = 0.3
NETWORK_FEE_USD = 0.01

BACKTEST_CONFIGS = {
    "conservative": {"tp_mult": 2.0, "sl_pct": -30.0, "size_frac": 0.2, "time_exit_sec": 3600, "label": "Conservative"},
    "aggressive": {"tp_mult": 3.0, "sl_pct": -50.0, "size_frac": 0.5, "time_exit_sec": 3600, "label": "Aggressive"},
    "yolo": {"tp_mult": 5.0, "sl_pct": -70.0, "size_frac": 1.0, "time_exit_sec": 3600, "label": "Yolo"},
    "shadow_15m": {"tp_mult": 3.0, "sl_pct": -50.0, "size_frac": 0.5, "time_exit_sec": 900, "label": "Shadow 15min (soak-only)"},
}

def round_trip_cost_pct():
    return SLIPPAGE_PCT + POOL_FEE_PCT

def trade_cost_usd(notional):
    return notional * (SLIPPAGE_PCT + POOL_FEE_PCT) / 100.0 + NETWORK_FEE_USD * 2

# --- Exit rules (live.py / replay.py reference these) ---
TP_MULT = 2.0          # take profit at 2x (matches backtest conservative)
SL_PCT_A = -30.0       # stop loss, copy track
SL_PCT_B = -30.0       # stop loss, survival track

# --- Paper runner settings (paper_live.py) ---
PAPER_POT_START = 5.0
PAPER_SIZE_FRAC = 0.2
PAPER_MIN_SIZE = 0.25
PAPER_MAX_OPEN = 3
PAPER_KILL_AT_POT = 2.0
PAPER_LOSS_STREAK_SKIP = 2
PAPER_WALLET_COOLDOWN_SEC = 7200
PAPER_MIN_POOL_LIQ = 800.0
PAPER_POLL_CYCLE_SEC = 30.0
PAPER_B_SCAN_SEC = 120.0
PAPER_MAX_SIG_FETCH = 8
PAPER_WATCHLIST_LIMIT = 25
