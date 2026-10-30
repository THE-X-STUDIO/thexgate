import json, time, os, requests, hashlib
from config import *

_cache = {}
_cache_dir = DATA_DIR
_last_gecko = [0.0]
_last_dex = [0.0]

def _cached(key, fn, ttl=300):
    if key in _cache:
        return _cache[key]
    path = os.path.join(_cache_dir, hashlib.md5(key.encode()).hexdigest() + ".json")
    if os.path.exists(path) and (time.time() - os.path.getmtime(path)) < ttl:
        try:
            val = json.loads(open(path, encoding="utf-8").read())
            _cache[key] = val
            return val
        except Exception:
            pass
    val = fn()
    if val in (None, [], {}, ""):
        return val  # don't cache empty/failed results (429 storms would poison for full ttl)
    _cache[key] = val
    try:
        open(path, "w", encoding="utf-8").write(json.dumps(val))
    except Exception:
        pass
    return val

def _throttle(slot, gap):
    now = time.time()
    wait = gap - (now - slot[0])
    if wait > 0:
        time.sleep(wait)
    slot[0] = time.time()

def gecko_get(path, params=None):
    _throttle(_last_gecko, GECKO_CALL_GAP)
    url = GECKO_BASE + path
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=20)
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except Exception:
            if attempt == 2:
                return None
            time.sleep(2 * (attempt + 1))
    return None

def dex_get(path):
    _throttle(_last_dex, DEX_CALL_GAP)
    url = DEX_BASE + path
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception:
            if attempt == 2:
                return None
            time.sleep(1 * (attempt + 1))
    return None

def get_trending_pools(limit=20):
    key = f"trending_{limit}"
    def fetch():
        data = gecko_get("/networks/solana/trending_pools")
        if not data:
            return []
        pools = data.get("data", [])[:limit]
        result = []
        for p in pools:
            attrs = p.get("attributes", {})
            result.append({
                "pool": attrs.get("address"),
                "name": attrs.get("name", ""),
                "price_usd": float(attrs.get("base_token_price_usd") or 0),
                "volume_usd": float(attrs.get("volume_usd", {}).get("h24") or 0),
                "liquidity_usd": float(attrs.get("reserve_in_usd") or 0),
                "created": attrs.get("pool_created_at", ""),
            })
        return result
    return _cached(key, fetch, ttl=120)

def get_new_pools(limit=50):
    key = f"newpools_{limit}"
    def fetch():
        data = gecko_get("/networks/solana/new_pools", {"limit": limit})
        if not data:
            return []
        pools = data.get("data", [])
        result = []
        for p in pools:
            attrs = p.get("attributes", {})
            rel = p.get("relationships", {})
            base_token = ((rel.get("base_token") or {}).get("data") or {}).get("id", "")
            result.append({
                "pool": attrs.get("address"),
                "name": attrs.get("name", ""),
                "base_token": base_token.split("_")[-1] if base_token else "",
                "price_usd": float(attrs.get("base_token_price_usd") or 0),
                "volume_usd": float(attrs.get("volume_usd", {}).get("h24") or 0),
                "liquidity_usd": float(attrs.get("reserve_in_usd") or 0),
                "created": attrs.get("pool_created_at", ""),
            })
        return result
    return _cached(key, fetch, ttl=60)

def get_pool_trades(pool_address, limit=250):
    key = f"trades_{pool_address}_{limit}"
    def fetch():
        data = gecko_get(f"/networks/solana/pools/{pool_address}/trades", {"limit": limit})
        if not data:
            return []
        trades = []
        for t in data.get("data", []):
            attrs = t.get("attributes", {})
            kind = attrs.get("kind", "")
            from_tok = attrs.get("from_token_address", "")
            to_tok = attrs.get("to_token_address", "")
            from_amt = float(attrs.get("from_token_amount") or 0)
            to_amt = float(attrs.get("to_token_amount") or 0)
            sol = "So11111111111111111111111111111111111111112"
            if kind == "buy":
                tokens_amount = to_amt
                native_amount = from_amt
            elif kind == "sell":
                tokens_amount = from_amt
                native_amount = to_amt
            else:
                tokens_amount = to_amt
                native_amount = from_amt
            trades.append({
                "wallet": attrs.get("tx_from_address", ""),
                "kind": kind,
                "price_usd": float(attrs.get("price_to_in_usd") or 0),
                "volume_usd": float(attrs.get("volume_in_usd") or 0),
                "tokens_amount": tokens_amount,
                "native_amount": native_amount,
                "from_tok": from_tok,
                "to_tok": to_tok,
                "ts": attrs.get("block_timestamp", ""),
                "tx": attrs.get("tx_hash", ""),
            })
        return trades
    return _cached(key, fetch, ttl=120)

def get_ohlcv(pool_address, timeframe="minute", agg=1, before=None, limit=60):
    key = f"ohlcv_{pool_address}_{timeframe}_{agg}_{before}_{limit}"
    def fetch():
        params = {"aggregate": agg, "limit": limit, "currency": "usd"}
        if before:
            params["before_timestamp"] = int(before)
        data = gecko_get(f"/networks/solana/pools/{pool_address}/ohlcv/{timeframe}", params)
        if not data:
            return []
        raw = data.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
        candles = []
        for c in raw:
            candles.append({"ts": c[0], "o": c[1], "h": c[2], "l": c[3], "c": c[4], "v": c[5]})
        candles.sort(key=lambda x: x["ts"])
        return candles
    return _cached(key, fetch, ttl=600)

def get_token_price(mint):
    key = f"price_{mint}"
    def fetch():
        data = dex_get(f"/latest/dex/tokens/{mint}")
        if not data:
            return 0.0
        pairs = [p for p in (data.get("pairs") or [])
                 if p.get("chainId") == "solana"
                 and (p.get("baseToken") or {}).get("address") == mint]
        if not pairs:
            return 0.0
        pairs.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0), reverse=True)
        return float(pairs[0].get("priceUsd") or 0)
    return _cached(key, fetch, ttl=15)

def get_token_pool(mint):
    key = f"pool_{mint}"
    def fetch():
        data = dex_get(f"/latest/dex/tokens/{mint}")
        if not data:
            return None
        pairs = [p for p in (data.get("pairs") or [])
                 if p.get("chainId") == "solana"
                 and (p.get("baseToken") or {}).get("address") == mint]
        if not pairs:
            return None
        pairs.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0), reverse=True)
        return {
            "pair": pairs[0].get("pairAddress"),
            "liq": float((pairs[0].get("liquidity") or {}).get("usd") or 0),
            "price": float(pairs[0].get("priceUsd") or 0),
        }
    return _cached(key, fetch, ttl=30)

def rpc_batch(method, params_list):
    payload = [
        {"jsonrpc": "2.0", "id": i, "method": method, "params": p}
        for i, p in enumerate(params_list)
    ]
    for attempt in range(3):
        try:
            r = requests.post(RPC_URL, json=payload, timeout=RPC_TIMEOUT_SEC, headers={"Content-Type": "application/json"})
            if r.status_code == 429:
                time.sleep(3 * (attempt + 1))
                continue
            r.raise_for_status()
            results = r.json()
            if isinstance(results, list):
                return [x.get("result") for x in results]
            return [results.get("result")]
        except Exception:
            if attempt == 2:
                return [None] * len(params_list)
            time.sleep(2 * (attempt + 1))

def get_signatures(address, limit=50, before=None):
    params = [address, {"limit": limit}]
    if before:
        params[1]["before"] = before
    result = rpc_batch("getSignaturesForAddress", [params])
    if not result or not result[0]:
        return []
    return [
        {"sig": s.get("signature"), "ts": s.get("blockTime"), "err": s.get("err")}
        for s in result[0]
        if not s.get("err")
    ]

def get_transactions_batch(signatures):
    if not signatures:
        return []
    params = [
        [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]
        for sig in signatures
    ]
    results = rpc_batch("getTransaction", params)
    return [r for r in results if r is not None]

def parse_swap(tx, wallet_address):
    meta = tx.get("meta", {})
    if meta.get("err"):
        return None
    account_keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
    keys = []
    for k in account_keys:
        keys.append(k if isinstance(k, str) else k.get("pubkey", ""))
    has_dex = any(k in DEX_PROGRAMS for k in keys)
    if not has_dex:
        return None
    wallet_idx = None
    for i, k in enumerate(keys):
        if k == wallet_address:
            wallet_idx = i
            break
    if wallet_idx is None:
        return None
    pre_bal = meta.get("preBalances", [])
    post_bal = meta.get("postBalances", [])
    sol_change = 0.0
    if wallet_idx < len(pre_bal) and wallet_idx < len(post_bal):
        sol_change = (post_bal[wallet_idx] - pre_bal[wallet_idx]) / 1e9
    pre_tokens = {}
    post_tokens = {}
    for item in meta.get("preTokenBalances", []):
        if item.get("owner") == wallet_address:
            mint = item.get("mint", "")
            amt = float(item.get("uiTokenAmount", {}).get("uiAmount") or 0)
            pre_tokens[mint] = pre_tokens.get(mint, 0) + amt
    for item in meta.get("postTokenBalances", []):
        if item.get("owner") == wallet_address:
            mint = item.get("mint", "")
            amt = float(item.get("uiTokenAmount", {}).get("uiAmount") or 0)
            post_tokens[mint] = post_tokens.get(mint, 0) + amt
    all_mints = set(list(pre_tokens.keys()) + list(post_tokens.keys()))
    bought = None
    sold = None
    for mint in all_mints:
        if mint == SOL_MINT:
            continue
        pre = pre_tokens.get(mint, 0)
        post = post_tokens.get(mint, 0)
        change = post - pre
        if change > 0 and (pre == 0 or change > pre * 0.005):
            bought = {"mint": mint, "amount": change}
        elif change < 0 and (post == 0 or abs(change) > pre * 0.005):
            sold = {"mint": mint, "amount": abs(change)}
    sigs = tx.get("transaction", {}).get("signatures", [])
    ts = tx.get("blockTime", 0)
    if bought and sol_change < -0.0001:
        return {"type": "buy", "mint": bought["mint"], "sol": abs(sol_change),
                "tokens": bought["amount"], "ts": ts, "sig": sigs[0] if sigs else ""}
    if sold and sol_change > 0.0001:
        return {"type": "sell", "mint": sold["mint"], "sol": sol_change,
                "tokens": sold["amount"], "ts": ts, "sig": sigs[0] if sigs else ""}
    if bought and sold:
        return {"type": "swap", "mint_in": sold["mint"], "mint_out": bought["mint"],
                "tokens_in": sold["amount"], "tokens_out": bought["amount"],
                "ts": ts, "sig": sigs[0] if sigs else ""}
    return None

def fetch_wallet_history(wallet, max_txs=50):
    key = f"hist_{wallet}_{max_txs}"
    def fetch():
        sigs = get_signatures(wallet, limit=min(max_txs, 100))
        sigs = [s for s in sigs if s.get("ts")][:max_txs]
        if not sigs:
            return []
        swaps = []
        batch_size = 4
        for i in range(0, len(sigs), batch_size):
            batch = sigs[i:i + batch_size]
            try:
                txs = get_transactions_batch([s["sig"] for s in batch])
            except Exception:
                txs = []
            for tx in txs:
                if tx is None:
                    continue
                try:
                    swap = parse_swap(tx, wallet)
                    if swap:
                        swaps.append(swap)
                except Exception:
                    continue
            if i + batch_size < len(sigs):
                time.sleep(1.5)
        return swaps
    return _cached(key, fetch, ttl=3600)
