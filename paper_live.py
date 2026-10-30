import json, os, sys, time, ctypes, argparse
from datetime import datetime, timezone
from config import *
from chain import rpc_batch, parse_swap, get_token_price, get_token_pool, gecko_get
from replay import compute_metrics, evaluate_gate

WALLETS_FILE = os.path.join(DATA_DIR, "wallets.json")
SOL_MINT_CFG = SOL_MINT

# --- MONEY CONFIG (pinned — authoritative over shared config.py; parallel session races it) ---
# Values can be overridden via env (cloud deploy keeps real values in envvars, not code)
SURVIVAL_MIN_PRICE_RATIO = float(os.environ.get("GATE_PRICE_RATIO", str(SURVIVAL_MIN_PRICE_RATIO)))
MONEY_ENTRY_MIN = int(os.environ.get("GATE_ENTRY_MIN", "7200"))    # 2h — survivor window (backtest PASS)
MONEY_ENTRY_MAX = int(os.environ.get("GATE_ENTRY_MAX", "21600"))   # 6h
MONEY_VOL5_MIN = float(os.environ.get("GATE_VOL5_MIN", "800.0"))   # vol_spike_2x lever (best: WR 47.9 PF 2.13 DD 21.4)
MONEY_TP_MULT = float(os.environ.get("GATE_TP_MULT", "2.0"))
MONEY_SL_PCT = float(os.environ.get("GATE_SL_PCT", "-30.0"))
MONEY_TIME_EXIT = int(os.environ.get("GATE_TIME_EXIT", "3600"))
MONEY_SIZE_FRAC = 0.2
MONEY_DAILY_KILL = 0.6    # pause if pot < 60% of day-start (−40%)
MONEY_LIQ_MIN = float(os.environ.get("GATE_LIQ_MIN", "1500.0"))

def paths(tag):
    return {
        "state": os.path.join(DATA_DIR, f"paper_state_{tag}.json"),
        "status": os.path.join(DATA_DIR, f"paper_status_{tag}.json"),
        "log": os.path.join(DATA_DIR, f"paper_log_{tag}.jsonl"),
        "lock": os.path.join(DATA_DIR, f"paper_lock_{tag}.pid"),
        "report": os.path.join(REPORT_DIR, f"paper_live_{tag}.json"),
    }

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def pid_alive(pid):
    try:
        h = ctypes.windll.kernel32.OpenProcess(0x100000, False, int(pid))
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        return False
    except Exception:
        return False

def acquire_lock(path):
    if os.path.exists(path):
        try:
            pid = int(open(path, encoding="utf-8").read().strip())
            if pid != os.getpid() and pid_alive(pid):
                return False
        except Exception:
            pass
        try:
            os.remove(path)
        except Exception:
            pass
    with open(path, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    return True

def release_lock(path):
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                pid = f.read().strip()
            if pid.strip() == str(os.getpid()):
                os.remove(path)
    except Exception:
        pass

class Paper:
    def __init__(self, tag, duration, wallets_file=WALLETS_FILE, executor=None, do_confirm=False, notify=False):
        self.tag = tag
        self.duration = duration
        self.p = paths(tag)
        self.wallets_file = wallets_file
        self.exec = executor
        self.confirm = do_confirm
        self.notify = notify
        self.pot = PAPER_POT_START
        self.next_id = 1
        self.positions = []
        self.closed = []
        self.streaks = {}
        self.cooldowns = {}
        self.known = []
        self.seen_pools = []
        self.watch = {}
        self.dbg = {"watch_adds": 0, "mature_seen": 0, "cand_none": 0, "price_dead": 0,
                    "vol_low": 0, "b_scans": 0, "ingested_last": 0}
        self.day_key = ""
        self.day_start_pot = PAPER_POT_START
        self.killed = False
        self.kill_reason = ""
        self.signal_count = 0
        self.start_ts = time.time()
        self.dirty = False
        self.logfh = open(self.p["log"], "a", encoding="utf-8")

    def lo(self, event, **data):
        entry = {"event": event, "at": now_iso(), **data}
        try:
            self.logfh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            self.logfh.flush()
        except Exception:
            pass
        print(f"[{self.tag}] {event}: " + json.dumps(data, default=str)[:200], flush=True)

    def note(self, text):
        if not self.notify:
            return
        try:
            from executor import tg as _tg
            _tg().send(f"[{self.tag}] {text}")
        except Exception:
            pass

    def _state_dict(self):
        return {
            "tag": self.tag, "start_ts": self.start_ts, "duration": self.duration,
            "pot": self.pot, "next_id": self.next_id, "killed": self.killed,
            "kill_reason": self.kill_reason, "signal_count": self.signal_count,
            "positions": self.positions, "closed": self.closed,
            "streaks": self.streaks, "cooldowns": {k: v for k, v in self.cooldowns.items()},
            "known": self.known[-8000:], "seen_pools": self.seen_pools[-4000:],
            "watch": self.watch, "day_key": self.day_key, "day_start_pot": self.day_start_pot,
            "dbg": self.dbg,
        }

    def save(self):
        state = self._state_dict()
        tmp = self.p["state"] + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, default=str)
        os.replace(tmp, self.p["state"])
        self.dirty = False

    def load(self):
        if not os.path.exists(self.p["state"]):
            return False
        try:
            st = json.loads(open(self.p["state"], encoding="utf-8").read())
        except Exception:
            return False
        self.start_ts = st.get("start_ts", time.time())
        self.duration = st.get("duration", self.duration)
        self.pot = st.get("pot", PAPER_POT_START)
        self.next_id = st.get("next_id", 1)
        self.killed = st.get("killed", False)
        self.kill_reason = st.get("kill_reason", "")
        self.signal_count = st.get("signal_count", 0)
        self.positions = st.get("positions", [])
        self.closed = st.get("closed", [])
        self.streaks = st.get("streaks", {}) or {}
        self.cooldowns = st.get("cooldowns", {}) or {}
        self.known = st.get("known", [])
        self.seen_pools = st.get("seen_pools", [])
        self.watch = st.get("watch", {}) or {}
        self.dbg = st.get("dbg", {}) or {}
        self.day_key = st.get("day_key", "") or ""
        self.day_start_pot = st.get("day_start_pot", PAPER_POT_START) or PAPER_POT_START
        return True

    def should_stop(self):
        return self.killed or (time.time() - self.start_ts) >= self.duration

    # ---------- wallet selection ----------
    def build_watchlist(self):
        try:
            ws = json.loads(open(self.wallets_file, encoding="utf-8").read())
        except Exception:
            return []
        cands = [w for w in ws
                 if w.get("wallet") and w.get("n_closed", 0) >= 3 and w.get("win_rate", 0) >= 50]
        cands.sort(key=lambda w: (w.get("win_rate", 0), w.get("pnl_usd", 0)), reverse=True)
        watch = [w["wallet"] for w in cands[:PAPER_WATCHLIST_LIMIT]]
        try:
            pinned = json.loads(open(os.path.join(DATA_DIR, "watch_add.json"), encoding="utf-8").read())
            add = pinned.get("wallets", []) if isinstance(pinned, dict) else []
        except Exception:
            add = []
        return list(dict.fromkeys(add + watch))[:PAPER_WATCHLIST_LIMIT]

    # ---------- risk + sizing ----------
    def sizing(self):
        return max(PAPER_MIN_SIZE, round(self.pot * MONEY_SIZE_FRAC, 2)) if self.pot > 0 else 0

    def wallet_blocked(self, wallet):
        if wallet in self.cooldowns and time.time() < self.cooldowns[wallet]:
            return True
        return False

    # ---------- open / close ----------
    def open_pos(self, mint, source, wallet, sig):
        if self.killed:
            return False
        if self.pot <= PAPER_KILL_AT_POT:
            self.killed = True
            self.kill_reason = f"pot ${self.pot:.2f} <= kill ${PAPER_KILL_AT_POT}"
            self.lo("kill", reason=self.kill_reason)
            self.note(f"KILLED: {self.kill_reason}")
            self.save()
            return False
        if len([1 for x in self.positions if not x.get("closed")]) >= PAPER_MAX_OPEN:
            self.lo("skip", mint=mint[:10], why="max open")
            return False
        if any(x["mint"] == mint for x in self.positions if x.get("open", False)):
            self.lo("skip", mint=mint[:10], why="already open")
            return False
        if wallet and self.wallet_blocked(wallet):
            self.lo("skip", wallet=wallet[:8], why="loss streak cooldown")
            return False
        info = get_token_pool(mint)
        if not info:
            self.lo("skip", mint=mint[:10], why="no pool data")
            return False
        price = info.get("price", 0) or 0
        liq = info.get("liq", 0) or 0
        if price <= 0:
            self.lo("skip", mint=mint[:10], why="no price")
            return False
        if liq < MONEY_LIQ_MIN:
            self.lo("skip", mint=mint[:10], why=f"liq ${liq:.0f} < ${PAPER_MIN_POOL_LIQ:.0f}")
            return False
        notional = self.sizing()
        if notional < PAPER_MIN_SIZE:
            return False
        entry = price
        if self.confirm and self.exec is not None:
            try:
                from executor import tg as _tg
                ans = _tg().confirm(
                    f"BUY src={source} ${notional:.2f} @ ${entry:.12f}\nmint {mint}\n"
                    f"TP {MONEY_TP_MULT}x | SL {MONEY_SL_PCT}% | exit {MONEY_TIME_EXIT}s | liq ${liq:.0f}")
                if ans is False:
                    self.lo("denied", mint=mint[:12], source=source)
                    return False
            except Exception as e:
                self.lo("confirm_error", err=str(e)[:120])
        if self.exec is not None:
            r = self.exec.buy(mint, notional, name=mint[:10])
            if r is None and not self.exec.dry:
                self.lo("exec_buy_failed", mint=mint[:12], source=source)
                return False
        pos = {
            "id": self.next_id, "mint": mint, "source": source,
            "wallet": wallet or "", "sig": sig or "",
            "entry_price": entry, "entry_ts": time.time(),
            "notional": notional, "high": entry,
            "tp_price": round(entry * MONEY_TP_MULT, 12), "sl_price": entry * (1 + MONEY_SL_PCT / 100),
            "liq_at_entry": liq, "open": True,
            "pump": mint.endswith("pump"),
        }
        self.next_id += 1
        self.positions.append(pos)
        self.dirty = True
        self.signal_count += 1
        self.lo("open", id=pos["id"], mint=mint[:12], source=source, wallet=(wallet or "?")[:8],
                entry=entry, notional=notional, liq=round(liq), sig=(sig or "")[:10])
        self.note(f"OPEN #{pos['id']} {mint[:10]}.. src={source} ${notional:.2f} @ {entry:.10f} "
                  f"liq=${liq:.0f} {'[PUMP]' if pos['pump'] else ''}")
        self.save()
        return True

    def close_pos(self, pos, exit_price, reason):
        entry = pos["entry_price"]
        ret_gross = (exit_price / entry - 1) * 100 if entry > 0 else -100
        ret_net = ret_gross - (SLIPPAGE_PCT + POOL_FEE_PCT)
        pnl = pos["notional"] * ret_net / 100 - NETWORK_FEE_USD * 2
        hold = time.time() - pos["entry_ts"]
        rec = {
            "id": pos["id"], "mint": pos["mint"], "source": pos["source"], "wallet": pos["wallet"],
            "entry_price": entry, "exit_price": exit_price, "return_pct": round(ret_net, 2),
            "reason": reason, "hold_sec": int(hold), "pnl_usd": round(pnl, 4),
            "ts": now_iso(), "pump": pos.get("pump", False),
        }
        self.closed.append(rec)
        pos["open"] = False
        self.pot = round(self.pot + pnl, 4)
        self.dirty = True
        w = pos.get("wallet") or ""
        if w:
            if pnl >= 0:
                self.streaks[w] = 0
            else:
                self.streaks[w] = self.streaks.get(w, 0) + 1
                if self.streaks.get(w, 0) >= PAPER_LOSS_STREAK_SKIP:
                    self.cooldowns[w] = time.time() + PAPER_WALLET_COOLDOWN_SEC
                    self.lo("cooldown", wallet=w[:8], streak=self.streaks[w], hours=PAPER_WALLET_COOLDOWN_SEC // 3600)
        self.lo("close", id=pos["id"], mint=pos["mint"][:12], reason=reason,
                ret_net=round(ret_net, 2), pnl=round(pnl, 4), pot=self.pot)
        self.note(f"CLOSE #{pos['id']} {reason} {ret_net:+.1f}% pnl=${pnl:+.2f} pot=${self.pot:.2f}")
        if self.exec is not None:
            try:
                self.exec.sell(pos["mint"], name=pos["mint"][:10])
            except Exception as e:
                self.lo("exec_sell_error", mint=pos["mint"][:12], err=str(e)[:120])
        self.save()

    def check_exits(self):
        for pos in list(self.positions):
            if not pos.get("open", False):
                continue
            price = get_token_price(pos["mint"])
            if price <= 0:
                continue
            pos["high"] = max(pos.get("high", pos["entry_price"]), price)
            reason = None
            if price >= pos["tp_price"]:
                reason = "tp"
            elif price <= pos["sl_price"]:
                reason = "sl"
            elif (time.time() - pos["entry_ts"]) >= MONEY_TIME_EXIT:
                reason = "time"
            if reason:
                self.close_pos(pos, price, reason)

    def close_too_poor(self):
        if self.killed:
            return
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if day != self.day_key:
            self.day_key = day
            self.day_start_pot = self.pot
        if self.pot <= PAPER_KILL_AT_POT:
            self.killed = True
            self.kill_reason = f"pot ${self.pot:.2f} <= kill ${PAPER_KILL_AT_POT}"
            self.lo("kill", reason=self.kill_reason)
            self.note(f"KILLED: {self.kill_reason}")
            self.save()
        elif self.day_start_pot > 0 and self.pot < self.day_start_pot * MONEY_DAILY_KILL:
            self.killed = True
            self.kill_reason = (f"daily kill: pot ${self.pot:.2f} < 60% of day-start "
                                f"${self.day_start_pot:.2f}")
            self.lo("kill", reason=self.kill_reason)
            self.note(f"KILLED: {self.kill_reason}")
            self.save()

    def close_all_at_last(self):
        for pos in self.positions:
            if not pos.get("open", False):
                continue
            price = get_token_price(pos["mint"])
            if price <= 0:
                continue
            self.close_pos(pos, price, "session_end")
        self.save()

    # ---------- track A polling ----------
    def poll_wallets(self, wallet_addrs):
        if not wallet_addrs:
            return []
        known = set(self.known)
        params = [[w, {"limit": 5}] for w in wallet_addrs]
        res = rpc_batch("getSignaturesForAddress", params)
        fresh = []
        if not res:
            return fresh
        for w, lst in zip(wallet_addrs, res):
            if not lst or not isinstance(lst, list):
                continue
            for s in lst:
                sig = s.get("signature")
                if not sig or s.get("err") or sig in known:
                    continue
                known.add(sig)
                fresh.append((w, sig))
        self.known = list(known)[-8000:]
        return fresh

    def fetch_swaps(self, fresh, cap=PAPER_MAX_SIG_FETCH):
        trades = []
        batch = fresh[:cap]
        for i in range(0, len(batch), 6):
            chunk = batch[i:i + 6]
            sigs = [s for _, s in chunk]
            results = rpc_batch("getTransaction",
                                [[sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}] for sig in sigs])
            if not results:
                continue
            for (w, sig), tx in zip(chunk, results):
                if not tx:
                    continue
                try:
                    sw = parse_swap(tx, w)
                except Exception:
                    sw = None
                if sw:
                    trades.append((w, sig, sw))
            if i + 6 < len(batch):
                time.sleep(0.4)
        return trades

    def process_track_a(self, wallet_addrs):
        fresh = self.poll_wallets(wallet_addrs)
        trades = self.fetch_swaps(fresh)
        for w, sig, sw in trades:
            if sw.get("type") != "buy":
                continue
            mint = sw.get("mint", "")
            if not mint or mint == SOL_MINT_CFG:
                continue
            self.open_pos(mint, "A", w, sig)

    # ---------- track B scanning ----------
    def fetch_new_pools_pages(self, n_pages=5, limit=100):
        out = []
        for p in range(1, n_pages + 1):
            data = gecko_get("/networks/solana/new_pools", {"limit": limit, "page": p})
            if not data:
                break
            items = data.get("data", [])
            if not items:
                break
            for item in items:
                attrs = item.get("attributes", {})
                rel = item.get("relationships", {})
                base = ((rel.get("base_token") or {}).get("data") or {}).get("id", "")
                out.append({
                    "pool": attrs.get("address"),
                    "name": attrs.get("name", ""),
                    "base_token": base.split("_")[-1] if base else "",
                    "price_usd": float(attrs.get("base_token_price_usd") or 0),
                    "liquidity_usd": float(attrs.get("reserve_in_usd") or 0),
                    "created": attrs.get("pool_created_at", ""),
                })
        return out

    def parse_created(self, ts_str):
        if not ts_str:
            return 0
        try:
            return int(datetime.fromisoformat(str(ts_str).replace("Z", "+00:00")).timestamp())
        except Exception:
            return 0

    def process_track_b(self):
        pools = self.fetch_new_pools_pages(5)
        now = int(time.time())
        added = 0
        for p in pools:
            addr = p.get("pool") or ""
            if not addr or addr in self.watch:
                continue
            created = self.parse_created(p.get("created", ""))
            if not created:
                continue
            age = now - created
            if age < 0 or age > MONEY_ENTRY_MAX:
                continue
            if p.get("liquidity_usd", 0) < MONEY_LIQ_MIN:
                continue
            mint = p.get("base_token", "")
            if not mint:
                continue
            self.watch[addr] = {"created": created, "mint": mint, "name": p.get("name", "")}
            added += 1
        self.dbg["watch_adds"] += added
        self.dbg["ingested_last"] = added
        made = 0
        for addr, meta in list(self.watch.items()):
            if self.should_stop():
                break
            age = now - meta["created"]
            if age > MONEY_ENTRY_MAX:
                del self.watch[addr]
                continue
            if age < MONEY_ENTRY_MIN:
                continue
            self.dbg["mature_seen"] += 1
            mint = meta["mint"]
            already = any(x["mint"] == mint for x in self.positions)
            if already:
                del self.watch[addr]
                continue
            cand = get_ohlcv_safe(addr, meta["created"])
            if not cand:
                self.dbg["cand_none"] += 1
                continue
            launch = cand.get("launch", 0)
            vol5 = cand.get("vol5", 0)
            last_price = cand.get("last_price", 0)
            if launch <= 0 or last_price <= 0:
                continue
            if last_price < launch * SURVIVAL_MIN_PRICE_RATIO:
                self.dbg["price_dead"] += 1
                del self.watch[addr]
                continue
            if vol5 > 0 and vol5 < MONEY_VOL5_MIN:
                self.dbg["vol_low"] += 1
                continue
            if self.open_pos(mint, "B", "", ""):
                made += 1
                del self.watch[addr]
        self.watch = dict(list(self.watch.items())[-600:])
        self.dbg["b_scans"] += 1
        self.dirty = True  # persist watch every scan (crash-safe, no more RAM-only watch)
        self.save()
        return made

    # ---------- metrics + heartbeats ----------
    def trades_for_metrics(self):
        return [{"return_pct": t["return_pct"], "reason": t.get("reason", "?"),
                 "hold_sec": t.get("hold_sec", 0), "pnl_usd": t.get("pnl_usd", 0)} for t in self.closed]

    def rolling(self):
        t = self.trades_for_metrics()
        if not t:
            return {"n": 0, "win_rate": 0, "pf": 0, "max_dd_pct": 0, "pnl": 0}
        m = compute_metrics(t)
        return {"n": m.get("n", 0), "win_rate": m.get("win_rate", 0), "pf": m.get("pf", 0),
                "max_dd_pct": m.get("max_dd_pct", 0), "pnl": m.get("total_pnl_usd", 0)}

    def status_write(self, elapsed, watch_n):
        r = self.rolling()
        st = {
            "tag": self.tag, "updated": now_iso(),
            "elapsed_min": int(elapsed // 60), "duration_min": int(self.duration // 60),
            "pot": self.pot, "open": len([x for x in self.positions if x.get("open")]),
            "closed": len(self.closed), "signals": self.signal_count,
            "watchlist": watch_n, "killed": self.killed,
            "watch_pools": len(self.watch), "dbg": self.dbg,
            **r,
        }
        tmp = self.p["status"] + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, default=str)
        os.replace(tmp, self.p["status"])

    def final_report(self):
        t = self.trades_for_metrics()
        m = compute_metrics(t)
        g, gr = evaluate_gate(m)
        report = {"label": f"paper_live_{self.tag}", "mode": "paper", "gate": g, "gate_reason": gr,
                  "pot": self.pot, "start": datetime.fromtimestamp(self.start_ts, timezone.utc).isoformat(),
                  "end": now_iso(), "duration_sec": self.duration, "kill": self.kill_reason,
                  "metrics": m, "trades": t}
        for sub, label in ((lambda x: not x.get("pump", False), "routable"),
                           (lambda x: x.get("pump", False), "pump_only")):
            st = [x for x in self.closed if sub(x)]
            if st:
                sm = compute_metrics([{"return_pct": x["return_pct"], "pnl_usd": x.get("pnl_usd", 0),
                                       "reason": x.get("reason", "?"), "hold_sec": x.get("hold_sec", 0)}
                                      for x in st])
                report[f"metrics_{label}"] = sm
                self.lo("split", label=label, n=sm.get("n", 0), wr=sm.get("win_rate", 0),
                        pf=sm.get("pf", 0), pnl=round(sum(x.get("pnl_usd", 0) for x in st), 2))
        with open(self.p["report"], "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False, default=str)
        self.lo("final", gate=g, reason=gr, pot=self.pot, n=m.get("n", 0),
                wr=m.get("win_rate", 0), pf=m.get("pf", 0), dd=m.get("max_dd_pct", 0))
        print("\n" + "=" * 60, flush=True)
        print(f"[PAPER] gate: {g} ({gr})", flush=True)
        print(f"[PAPER] pot ${self.pot:.2f} from ${PAPER_POT_START:.2f} | n={m.get('n',0)} "
              f"WR={m.get('win_rate',0)}% PF={m.get('pf',0)} DD={m.get('max_dd_pct',0)}%", flush=True)
        print(f"[PAPER] report: {self.p['report']}", flush=True)
        print("=" * 60, flush=True)

def get_ohlcv_safe(pool_addr, created_ts):
    try:
        from chain import get_ohlcv
        candles = get_ohlcv(pool_addr, "minute", 1, limit=400)
        if not candles:
            return None
        c0 = None
        for c in candles:
            if created_ts - 120 <= c["ts"] <= created_ts + 300:
                c0 = c
                break
        if c0 is None:
            c0 = candles[0]
        launch = c0["o"]
        vol5 = sum(c.get("v", 0) for c in candles[-5:])
        last_price = candles[-1].get("c", 0)
        return {"launch": launch, "vol5": vol5, "last_price": last_price}
    except Exception:
        return None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="prod")
    ap.add_argument("--duration", type=int, default=86400)
    ap.add_argument("--wallets", default="")
    ap.add_argument("--real", action="store_true", help="attach Jupiter executor (dry-run default)")
    ap.add_argument("--send", action="store_true", help="ACTUALLY send real txs (requires --real)")
    ap.add_argument("--confirm", action="store_true", help="Telegram y/n per entry (default-deny on timeout)")
    ap.add_argument("--notify", action="store_true", help="Telegram open/close/kill notifications (non-blocking)")
    args = ap.parse_args()

    executor = None
    if args.real:
        from executor import Executor
        executor = Executor(dry=not args.send)
        print(f"[exec] mode={'DRY' if executor.dry else 'REAL SEND'} confirm={args.confirm}", flush=True)

    pp = paths(args.tag)
    if not acquire_lock(pp["lock"]):
        print(f"[{args.tag}] already running (lock {pp['lock']}) - exit", flush=True)
        sys.exit(0)
    bot = Paper(args.tag, args.duration, args.wallets or WALLETS_FILE,
                executor=executor, do_confirm=args.confirm, notify=args.notify)
    resumed = bot.load()
    elapsed = max(0, time.time() - bot.start_ts)
    if bot.should_stop():
        bot.close_all_at_last()
        bot.final_report()
        release_lock(pp["lock"])
        sys.exit(0)
    watch = bot.build_watchlist()
    bot.lo("start" if not resumed else "resume", tag=args.tag, resumed=resumed, watchlist=len(watch),
           pot=bot.pot, open=len([x for x in bot.positions if x.get("open")]), closed=len(bot.closed),
           duration_min=int(args.duration // 60), stale_min=int(elapsed // 60))
    if resumed:
        bot.duration = args.duration  # re-arm window on restart
    bot.save()
    remain = bot.duration - elapsed
    next_b = time.time() + 15
    next_save = time.time() + 60
    next_beat = time.time() + 60
    try:
        while not bot.should_stop():
            t0 = time.time()
            try:
                bot.process_track_a(watch)
            except Exception as e:
                bot.lo("error_a", err=str(e)[:200])
            try:
                bot.check_exits()
            except Exception as e:
                bot.lo("error_exits", err=str(e)[:200])
            if time.time() >= next_b and not bot.killed:
                try:
                    bot.process_track_b()
                except Exception as e:
                    bot.lo("error_b", err=str(e)[:160])
                next_b = time.time() + PAPER_B_SCAN_SEC
            bot.close_too_poor()
            try:
                if bot.dirty:
                    bot.save()
            except Exception as e:
                bot.lo("error_save", err=str(e)[:200])
                bot.dirty = False
            now = time.time()
            if now >= next_beat:
                try:
                    r = bot.rolling()
                    print(f"[paper {args.tag} {int((now - bot.start_ts)//60)}m/{bot.duration//60}m] "
                          f"pot=${bot.pot:.2f} open={len([x for x in bot.positions if x.get('open')])} "
                          f"closed={len(bot.closed)} WR={r['win_rate']}% PF={r['pf']} DD={r['max_dd_pct']}% "
                          f"killed={bot.killed}", flush=True)
                    bot.status_write(now - bot.start_ts, len(watch))
                except Exception as e:
                    bot.lo("error_beat", err=str(e)[:200])
                next_beat = now + 60
            sleep_s = PAPER_POLL_CYCLE_SEC - (time.time() - t0)
            if sleep_s > 1:
                time.sleep(sleep_s)
    except KeyboardInterrupt:
        bot.lo("interrupt")
    except BaseException as e:
        bot.lo("fatal", err=repr(e)[:300])
    finally:
        try:
            bot.save()
            bot.close_all_at_last()
            bot.final_report()
        except Exception as e:
            bot.lo("error_final", err=str(e)[:200])
        release_lock(pp["lock"])

if __name__ == "__main__":
    main()
