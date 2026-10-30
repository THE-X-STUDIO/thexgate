import json, os, time, math
from datetime import datetime, timezone
from config import *
from chain import get_ohlcv, get_token_pool, get_new_pools, get_pool_trades, get_trending_pools

def parse_ts(ts_str):
    if not ts_str:
        return 0
    try:
        return int(datetime.fromisoformat(str(ts_str).replace("Z", "+00:00")).timestamp())
    except Exception:
        try:
            return int(ts_str)
        except Exception:
            return 0

def simulate_exit_a(entry_price, entry_ts, pool_addr):
    if entry_price <= 0 or not pool_addr:
        return {"exit_price": 0, "return_pct": -100, "reason": "no_data", "hold_sec": 0}
    before = entry_ts + TIME_EXIT_SEC + 120
    candles = get_ohlcv(pool_addr, "minute", 1, before=before, limit=70)
    if not candles:
        return {"exit_price": entry_price, "return_pct": 0, "reason": "no_ohlcv", "hold_sec": 0}
    candles = [c for c in candles if c["ts"] >= entry_ts - 60]
    if not candles:
        return {"exit_price": entry_price, "return_pct": 0, "reason": "no_candles", "hold_sec": 0}
    tp_price = entry_price * TP_MULT
    sl_price = entry_price * (1 + SL_PCT_A / 100)
    for c in candles:
        if c["ts"] < entry_ts:
            continue
        if c["h"] >= tp_price:
            ret = (tp_price / entry_price - 1) * 100
            return {"exit_price": tp_price, "return_pct": round(ret, 2), "reason": "tp", "hold_sec": c["ts"] - entry_ts}
        if c["l"] <= sl_price:
            ret = (sl_price / entry_price - 1) * 100
            return {"exit_price": sl_price, "return_pct": round(ret, 2), "reason": "sl", "hold_sec": c["ts"] - entry_ts}
    last = candles[-1]
    ret = (last["c"] / entry_price - 1) * 100
    return {"exit_price": last["c"], "return_pct": round(ret, 2), "reason": "time", "hold_sec": last["ts"] - entry_ts}

def replay_track_a(wallets, max_per_wallet=20):
    print(f"\n[replay A] copy trading replay on {len(wallets)} wallets...")
    all_trades = []
    for wi, w in enumerate(wallets):
        wallet_addr = w.get("wallet", "")
        closed = w.get("closed_trades", [])
        if not closed:
            continue
        print(f"[replay A] wallet {wi+1}/{len(wallets)} {wallet_addr[:8]}... ({len(closed)} closed)", flush=True)
        to_process = closed[:max_per_wallet]
        for ti, ct in enumerate(to_process):
            ret_pct = ct.get("return", 0) * 100
            hold_sec = ct.get("hold_sec", 3600)
            if hold_sec <= 0:
                hold_sec = 3600
            pnl_usd = SIZING_USD * ret_pct / 100
            trade = {
                "wallet": wallet_addr,
                "return_pct": round(ret_pct, 2),
                "reason": "gecko_actual",
                "hold_sec": hold_sec,
                "pnl_usd": round(pnl_usd, 4),
            }
            all_trades.append(trade)
        print(f"[replay A]   added {len(to_process)} trades", flush=True)
    return all_trades

def replay_track_b(pool_count=30, pools=None):
    print(f"\n[replay B] survival momentum replay...")
    if pools is None:
        pools = get_new_pools(pool_count)
    print(f"[replay B] got {len(pools)} new pools")
    all_trades = []
    now = int(time.time())
    for pi, p in enumerate(pools):
        pool_addr = p.get("pool")
        name = p.get("name", "?")
        created_str = p.get("created", "")
        created_ts = parse_ts(created_str)
        if created_ts <= 0:
            continue
        age = now - created_ts
        if age < SURVIVAL_MIN_AGE:
            continue
        if age > SURVIVAL_MAX_AGE * 4:
            continue
        liq = p.get("liquidity_usd", 0)
        if liq < SURVIVAL_MIN_LIQ:
            continue
        price_now = p.get("price_usd", 0)
        if price_now <= 0:
            continue
        before = created_ts + SURVIVAL_MAX_AGE + 300
        candles = get_ohlcv(pool_addr, "minute", 1, before=before, limit=SURVIVAL_MAX_AGE // 60 + 10)
        if len(candles) < 35:
            continue
        launch_price = candles[0]["o"]
        if launch_price <= 0:
            continue
        entry_idx = None
        for ci, c in enumerate(candles):
            if c["ts"] - created_ts >= SURVIVAL_MIN_AGE:
                entry_idx = ci
                break
        if entry_idx is None:
            continue
        entry_candle = candles[entry_idx]
        entry_price = entry_candle["o"]
        entry_ts = entry_candle["ts"]
        if entry_price <= 0:
            continue
        if entry_price < launch_price * SURVIVAL_MIN_PRICE_RATIO:
            continue
        vol5m = 0
        for ci in range(max(0, entry_idx - 5), entry_idx):
            vol5m += candles[ci].get("v", 0) * candles[ci].get("c", 0)
        if vol5m < SURVIVAL_MIN_VOL5M and vol5m > 0:
            continue
        tp_price = entry_price * TP_MULT
        sl_price = entry_price * (1 + SL_PCT_B / 100)
        exit_price = None
        exit_reason = "time"
        exit_ts = entry_ts + TIME_EXIT_SEC
        for c in candles[entry_idx:]:
            if c["ts"] > entry_ts + TIME_EXIT_SEC:
                break
            if c["h"] >= tp_price:
                exit_price = tp_price
                exit_reason = "tp"
                exit_ts = c["ts"]
                break
            if c["l"] <= sl_price:
                exit_price = sl_price
                exit_reason = "sl"
                exit_ts = c["ts"]
                break
        if exit_price is None:
            last_c = None
            for c in reversed(candles):
                if c["ts"] <= entry_ts + TIME_EXIT_SEC:
                    last_c = c
                    break
            if last_c is None:
                last_c = candles[-1]
            exit_price = last_c["c"]
        ret = (exit_price / entry_price - 1) * 100
        trade = {
            "pool": pool_addr,
            "name": name,
            "launch_price": launch_price,
            "entry_price": entry_price,
            "entry_ts": entry_ts,
            "exit_price": exit_price,
            "return_pct": round(ret, 2),
            "reason": exit_reason,
            "hold_sec": exit_ts - entry_ts,
            "pnl_usd": round(SIZING_USD * ret / 100, 4),
            "liq": liq,
            "age_at_entry": entry_ts - created_ts,
        }
        all_trades.append(trade)
        if (pi + 1) % 10 == 0:
            print(f"[replay B]   processed {pi+1}/{len(pools)}, {len(all_trades)} trades so far")
    return all_trades

def compute_metrics(trades):
    if not trades:
        return {"n": 0}
    returns = [t["return_pct"] / 100 for t in trades]
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r < 0]
    total_win = sum(wins) if wins else 0
    total_loss = abs(sum(losses)) if losses else 0
    pf = total_win / total_loss if total_loss > 0 else (99 if total_win > 0 else 0)
    bankroll = SIZING_USD
    peak = bankroll
    max_dd = 0
    for r in returns:
        bankroll *= (1 + r)
        peak = max(peak, bankroll)
        if peak > 0:
            max_dd = max(max_dd, (peak - bankroll) / peak)
    reasons = {}
    for t in trades:
        r = t.get("reason", "?")
        reasons[r] = reasons.get(r, 0) + 1
    return {
        "n": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(trades) * 100, 1),
        "pf": round(pf, 2),
        "avg_return_pct": round(sum(returns) / len(returns) * 100, 2),
        "total_pnl_usd": round(sum(t.get("pnl_usd", 0) for t in trades), 4),
        "compounded_end": round(bankroll, 4),
        "max_dd_pct": round(max_dd * 100, 1),
        "exit_reasons": reasons,
    }

def evaluate_gate(metrics):
    if metrics.get("n", 0) < GATE_MIN_TRADES:
        return "INCONCLUSIVE", f"only {metrics.get('n',0)} trades (need {GATE_MIN_TRADES})"
    reasons = []
    ok = True
    if metrics["win_rate"] < GATE_MIN_WIN_RATE:
        ok = False
        reasons.append(f"win_rate {metrics['win_rate']}% < {GATE_MIN_WIN_RATE}%")
    if metrics["pf"] < GATE_MIN_PF:
        ok = False
        reasons.append(f"pf {metrics['pf']} < {GATE_MIN_PF}")
    if metrics["max_dd_pct"] > GATE_MAX_DD:
        ok = False
        reasons.append(f"max_dd {metrics['max_dd_pct']}% > {GATE_MAX_DD}%")
    if metrics["total_pnl_usd"] <= 0:
        ok = False
        reasons.append(f"total_pnl ${metrics['total_pnl_usd']} <= 0")
    if ok:
        return "PASS", "all criteria met"
    return "FAIL", "; ".join(reasons)

def save_report(label, trades, metrics, gate, gate_reason, path=None):
    path = path or os.path.join(REPORT_DIR, f"replay_{label}.json")
    report = {"label": label, "gate": gate, "gate_reason": gate_reason,
              "metrics": metrics, "trades": trades}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n[report] {label}: gate={gate} ({gate_reason})")
    print(f"[report] n={metrics.get('n',0)} WR={metrics.get('win_rate',0)}% PF={metrics.get('pf',0)} "
          f"avg={metrics.get('avg_return_pct',0)}% pnl=${metrics.get('total_pnl_usd',0)} "
          f"dd={metrics.get('max_dd_pct',0)}%")
    return path

if __name__ == "__main__":
    from discover import load_wallets
    wallets = load_wallets()
    trades_a = replay_track_a(wallets) if wallets else []
    m_a = compute_metrics(trades_a)
    g_a, gr_a = evaluate_gate(m_a)
    save_report("track_a", trades_a, m_a, g_a, gr_a)
    trades_b = replay_track_b()
    m_b = compute_metrics(trades_b)
    g_b, gr_b = evaluate_gate(m_b)
    save_report("track_b", trades_b, m_b, g_b, gr_b)
