import json, os, threading, time, traceback
from flask import Flask, jsonify

import paper_live as pl

TAG = "gate"
DURATION = int(os.environ.get("GATE_DURATION", "172800"))
SUPA_URI = os.environ.get("SUPA_STATE_URI", "")
POT_START_OVERRIDE = os.environ.get("GATE_POT_START", "")
CHAT_ID_KEY = "tg_chat_id"

import psycopg2
from psycopg2.extras import Json

_print_lock = threading.Lock()


def slog(msg):
    with _print_lock:
        print(f"[service {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def supa_get():
    if not SUPA_URI:
        return None
    for attempt in range(3):
        try:
            conn = psycopg2.connect(SUPA_URI, sslmode="require", connect_timeout=15)
            cur = conn.cursor()
            cur.execute("select state from thex_gate_state where id=%s", (TAG,))
            row = cur.fetchone()
            conn.close()
            return row[0] if row else None
        except Exception as e:
            slog(f"supa_get err {e!r}")
            time.sleep(3 * (attempt + 1))
    return None


def supa_put(d):
    if not SUPA_URI:
        return
    for attempt in range(3):
        try:
            conn = psycopg2.connect(SUPA_URI, sslmode="require", connect_timeout=15)
            cur = conn.cursor()
            cur.execute(
                "insert into thex_gate_state (id, state) values (%s, %s) "
                "on conflict (id) do update set state=excluded.state, updated_at=now()",
                (TAG, Json(d)),
            )
            conn.commit()
            conn.close()
            return
        except Exception as e:
            slog(f"supa_put err {e!r}")
            time.sleep(3 * (attempt + 1))


class CloudPaper(pl.Paper):
    """Paper with Supabase state sync — survives Render container restarts."""

    def load(self):
        ok = super().load()
        if ok:
            return True
        st = supa_get()
        if st:
            try:
                os.makedirs(os.path.dirname(self.p["state"]), exist_ok=True)
                with open(self.p["state"], "w", encoding="utf-8") as f:
                    json.dump(st, f, ensure_ascii=False, default=str)
                ok = super().load()
                slog(f"state restored from supabase (pot={st.get('pot')}, watch={len(st.get('watch') or {})})")
            except Exception as e:
                slog(f"state restore err {e!r}")
        return ok

    def save(self):
        try:
            super().save()
            st = self._state_dict()
            try:
                from executor import tg as _tg
                st[CHAT_ID_KEY] = _tg().chat_id or ""
            except Exception:
                pass
            supa_put(st)
        except Exception as e:
            slog(f"save err {e!r}")

    def restore_tg_chat(self):
        st = supa_get() or {}
        cid = st.get(CHAT_ID_KEY)
        if cid:
            try:
                from executor import CHAT_CACHE
                if not os.path.exists(CHAT_CACHE):
                    open(CHAT_CACHE, "w", encoding="utf-8").write(str(cid))
                    slog(f"tg chat restored: {str(cid)[:8]}")
            except Exception:
                pass


BOT = None
BOT_LOCK = threading.Lock()
START_TS = time.time()


def runner_loop():
    global BOT
    while True:
        try:
            bot = CloudPaper(TAG, DURATION, notify=True)
            resumed = bot.load()
            if POT_START_OVERRIDE and not bot.closed:
                try:
                    bot.pot = float(POT_START_OVERRIDE)
                except Exception:
                    pass
            with BOT_LOCK:
                BOT = bot
            bot.restore_tg_chat()
            watch = bot.build_watchlist()
            elapsed = max(0, time.time() - bot.start_ts)
            if bot.should_stop():
                bot.close_all_at_last()
                bot.final_report()
                slog("run window complete - final written, exiting thread")
                return
            bot.lo("start" if not resumed else "resume", tag=TAG,
                   resumed=resumed, watchlist=len(watch), pot=bot.pot,
                   duration_min=DURATION // 60, stale_min=int(elapsed // 60))
            bot.save()
            next_b = time.time() + 15
            next_beat = time.time() + 60
            while not bot.should_stop():
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
                        bot.lo("error_b", err=str(e)[:200])
                    next_b = time.time() + pl.PAPER_B_SCAN_SEC
                bot.close_too_poor()
                now = time.time()
                if now >= next_beat:
                    next_beat = now + 60
                    try:
                        bot.status_write(now - bot.start_ts, len(watch))
                        r = bot.rolling()
                        slog(f"beat pot=${bot.pot:.2f} open={len([x for x in bot.positions if x.get('open')])} "
                             f"closed={len(bot.closed)} n={r['n']} watch={len(bot.watch)} "
                             f"dbg={json.dumps(bot.dbg)}")
                    except Exception as e:
                        slog(f"beat err {e!r}")
                time.sleep(5)
            bot.close_all_at_last()
            bot.final_report()
            return
        except Exception:
            slog("runner crashed:\n" + traceback.format_exc()[-1500:])
            time.sleep(20)


app = Flask(__name__)


@app.route("/health")
@app.route("/")
def health():
    try:
        with BOT_LOCK:
            bot = BOT
        if not bot:
            return jsonify({"ok": True, "phase": "booting", "since": START_TS}), 200
        r = bot.rolling()
        return jsonify({
            "ok": True, "tag": TAG,
            "elapsed_min": int((time.time() - bot.start_ts) // 60),
            "duration_min": int(bot.duration // 60),
            "pot": bot.pot, "open": len([x for x in bot.positions if x.get("open")]),
            "closed": len(bot.closed), "watch_pools": len(bot.watch),
            "n": r["n"], "win_rate": r["win_rate"], "pf": r["pf"], "dd": r["max_dd_pct"],
            "dbg": bot.dbg, "killed": bot.killed, "kill_reason": bot.kill_reason,
        }), 200
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)[:200]}), 200


@app.route("/full")
def full():
    try:
        with BOT_LOCK:
            bot = BOT
        return jsonify(json.load(open(bot.p["state"], encoding="utf-8")))
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)[:200]}), 200


if __name__ == "__main__":
    threading.Thread(target=runner_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")), threaded=True)
