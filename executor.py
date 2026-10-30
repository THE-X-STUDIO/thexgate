"""Jupiter v6 (lite) executor for THEXSTUDIO $5 challenge.

DRY-RUN BY DEFAULT. Real sends only when dry=False (CLI: --real --send).
Sells go through Jupiter too (quote mint->SOL, full balance).
Logs every action to data/executor_log.jsonl.
Semi-auto confirm via Telegram inline buttons (token from ai-trading-firm\\.env).
"""
import json, os, time, base64, threading
import requests
from datetime import datetime, timezone
from config import RPC_URL, SLIPPAGE_PCT

DATA_DIR_LOCAL = r"C:\Users\acer\trading\data"
WALLET_FILE = os.path.join(DATA_DIR_LOCAL, "wallet_key.json")
LOG_FILE = os.path.join(DATA_DIR_LOCAL, "executor_log.jsonl")
TG_ENV = r"C:\Users\acer\ai-trading-firm\.env"
CHAT_CACHE = os.path.join(DATA_DIR_LOCAL, "tg_chat_id.txt")

SOL_MINT = "So11111111111111111111111111111111111111112"
JUP_BASE = "https://lite-api.jup.ag/swap/v1"
SLIPPAGE_BPS = int(SLIPPAGE_PCT * 100)  # 3% modelled
LAMPORTS = 1_000_000_000

def _log(event, **data):
    entry = {"event": event, "at": datetime.now(timezone.utc).isoformat(), **data}
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")
    print(f"[exec] {event}: " + json.dumps(data, default=str)[:180], flush=True)

def _load_env(path=TG_ENV):
    out = {}
    try:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return out

class Telegram:
    def __init__(self):
        env = _load_env()
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN") or env.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID") or env.get("TELEGRAM_CHAT_ID", "")
        if not self.chat_id and os.path.exists(CHAT_CACHE):
            self.chat_id = open(CHAT_CACHE, encoding="utf-8").read().strip()
        self.offset = 0
        self._drain()

    def _api(self, method, **params):
        if not self.token:
            return None
        try:
            r = requests.post(f"https://api.telegram.org/bot{self.token}/{method}",
                              json=params, timeout=15)
            return r.json()
        except Exception as e:
            _log("tg_error", method=method, err=str(e)[:120])
            return None

    def _drain(self):
        res = self._api("getUpdates", offset=self.offset, timeout=0)
        if res and res.get("ok"):
            for u in res["result"]:
                self.offset = u["update_id"] + 1
                msg = u.get("message") or u.get("callback_query", {}).get("message")
                if msg and not self.chat_id:
                    self.chat_id = str(msg["chat"]["id"])
                    open(CHAT_CACHE, "w", encoding="utf-8").write(self.chat_id)
                    _log("tg_chat_cached", chat_id=self.chat_id)

    def send(self, text, buttons=None):
        params = {"chat_id": self.chat_id, "text": text[:3800]}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": d}] for t, d in buttons]}
        res = self._api("sendMessage", **params)
        if res and res.get("ok"):
            return res["result"]["message_id"]
        return None

    def confirm(self, text, timeout_sec=180):
        """Send question with Yes/No buttons; True only if Yes pressed in time."""
        if not self.token or not self.chat_id:
            return None  # no tg -> caller decides (skip confirm, go ahead)
        mid = self.send(text, buttons=[("YES buy", "trade:yes"), ("NO skip", "trade:no")])
        if not mid:
            return None
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            res = self._api("getUpdates", offset=self.offset, timeout=5)
            if res and res.get("ok"):
                for u in res["result"]:
                    self.offset = u["update_id"] + 1
                    cb = u.get("callback_query")
                    if cb and cb.get("message", {}).get("message_id") == mid:
                        data = cb.get("data", "")
                        self._api("answerCallbackQuery", callback_query_id=cb["id"], text=data)
                        try:
                            self._api("editMessageText", chat_id=self.chat_id, message_id=mid,
                                      text=text[:3800] + ("\n\nANSWERED: " + data))
                        except Exception:
                            pass
                        return data == "trade:yes"
            else:
                time.sleep(2)
        self._api("editMessageText", chat_id=self.chat_id, message_id=mid,
                  text=text[:3800] + "\n\nTIMED OUT -> skipped")
        return False

TG = None
def tg():
    global TG
    if TG is None:
        TG = Telegram()
    return TG

class Executor:
    def __init__(self, dry=True):
        self.dry = dry
        self.kp = None
        if not dry:
            from solders.keypair import Keypair
            self.kp = Keypair.from_bytes(json.load(open(WALLET_FILE)))
            _log("executor_real", pubkey=str(self.kp.pubkey()))
        else:
            _log("executor_dry")

    # ---------- chain reads ----------
    def _rpc(self, method, params):
        r = requests.post(RPC_URL, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
        r.raise_for_status()
        j = r.json()
        if j.get("error"):
            raise RuntimeError(str(j["error"])[:200])
        return j["result"]

    def sol_balance_lamports(self):
        if self.dry or not self.kp:
            return LAMPORTS  # pretend $1 for dry
        res = self._rpc("getBalance", [str(self.kp.pubkey())])
        return res["value"]

    def token_balance_raw(self, mint):
        if self.dry or not self.kp:
            return "0", 0
        res = self._rpc("getTokenAccountsByOwner",
                        [str(self.kp.pubkey()), {"mint": mint}, {"encoding": "jsonParsed"}])
        for acc in res.get("value", []):
            info = acc["account"]["data"]["parsed"]["info"]
            return info["tokenAmount"]["amount"], info["tokenAmount"]["decimals"]
        return "0", 0

    # ---------- jupiter ----------
    def quote(self, in_mint, out_mint, amount):
        r = requests.get(f"{JUP_BASE}/quote", params={
            "inputMint": in_mint, "outputMint": out_mint, "amount": str(int(amount)),
            "slippageBps": SLIPPAGE_BPS, "restrictIntermediateTokens": "true",
        }, timeout=20)
        r.raise_for_status()
        return r.json()

    def _swap_and_send(self, quote):
        if self.dry:
            return {"dry": True, "outAmount": quote.get("outAmount"), "priceImpact": quote.get("priceImpactPct")}
        r = requests.post(f"{JUP_BASE}/swap", json={
            "quoteResponse": quote,
            "userPublicKey": str(self.kp.pubkey()),
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": "auto",
        }, timeout=30)
        r.raise_for_status()
        swap = r.json()
        raw = base64.b64decode(swap["swapTransaction"])
        from solders.transaction import VersionedTransaction
        tx = VersionedTransaction.from_bytes(raw)
        signed = VersionedTransaction(tx.message, [self.kp])
        b64 = base64.b64encode(bytes(signed)).decode()
        sig = self._rpc("sendTransaction", [b64, {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}])
        ok = self._confirm(sig)
        return {"sig": sig, "confirmed": ok}

    def _confirm(self, sig, tries=20):
        for _ in range(tries):
            try:
                res = self._rpc("getSignatureStatuses", [[sig]])
                st = res["value"][0]
                if st and st.get("confirmationStatus") in ("confirmed", "finalized") and not st.get("err"):
                    return True
                if st and st.get("err"):
                    _log("tx_err", sig=sig, err=str(st["err"])[:160])
                    return False
            except Exception as e:
                _log("confirm_retry", err=str(e)[:100])
            time.sleep(2)
        return False

    # ---------- public actions ----------
    def buy(self, mint, usd, name=""):
        lamports = int(usd * LAMPORTS)
        if lamports < 50_000_000:  # $0.05 dust guard
            _log("buy_skip_dust", usd=usd)
            return None
        try:
            q = self.quote(SOL_MINT, mint, lamports)
        except Exception as e:
            _log("buy_no_route", mint=mint[:10], err=str(e)[:140])
            return None
        res = self._swap_and_send(q)
        _log("buy", mode="dry" if self.dry else "real", name=name, usd=usd,
             out=q.get("outAmount"), sig=res.get("sig"), confirmed=res.get("confirmed"),
             impact=q.get("priceImpactPct"))
        res["outAmount"] = q.get("outAmount")
        return res

    def sell(self, mint, name=""):
        raw_amt, dec = self.token_balance_raw(mint)
        if raw_amt in ("0", 0, None) and not self.dry:
            _log("sell_skip_empty", mint=mint[:10])
            return None
        amt = int(raw_amt) if raw_amt else 1_000_000  # dry placeholder
        try:
            q = self.quote(mint, SOL_MINT, max(amt, 1))
        except Exception as e:
            _log("sell_no_route", mint=mint[:10], err=str(e)[:140])
            return None
        res = self._swap_and_send(q)
        _log("sell", mode="dry" if self.dry else "real", name=name,
             in_amt=raw_amt, out_lamports=q.get("outAmount"), sig=res.get("sig"),
             confirmed=res.get("confirmed"))
        return res

    def status(self):
        return {
            "mode": "dry" if self.dry else "real",
            "pubkey": str(self.kp.pubkey()) if self.kp else None,
            "sol_lamports": self.sol_balance_lamports(),
            "tg_chat": tg().chat_id or "(none — message the bot once)",
        }
