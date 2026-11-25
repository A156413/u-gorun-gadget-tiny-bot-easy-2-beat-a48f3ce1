"""Offline backtester mirroring main.ts logic, to tune params fast and check
robustness across halves of the dev window. Approximates the harness:
fills at the next bar's open, 10 bps fee, 5 bps adverse slippage; impact
ignored (our ~$1-2.5k trades are tiny vs pool depth). Flatten at last close.
"""
import gzip, json, sys
from pathlib import Path

KIT = Path.home() / "workspace/makefun/kits/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1-kit"
MANIFEST = json.loads((KIT / "dataset/manifest.json").read_text())
SCALES = {s["symbol"]: (s["price_scale"], s["qty_scale"]) for s in MANIFEST["symbols"]}
SYMBOLS = [s["symbol"] for s in MANIFEST["symbols"]]

def load_bars():
    data = {}
    for sym in SYMBOLS:
        bars = []
        with gzip.open(KIT / f"dataset/bars/{sym}.ndjson.gz", "rt") as f:
            for line in f:
                bars.append(json.loads(line))
        bars.sort(key=lambda b: b["t"])
        data[sym] = bars
    return data

BARS = load_bars()
N = len(BARS["SOL"])
CAPITAL = 10_000.0
FEE = 0.0010
SLIP = 0.0005
MAX_WEIGHT = 0.25
MIN_NOTIONAL = 1.0

def price(sym, bar):
    ps, _ = SCALES[sym]
    return int(bar["c"]) / 10**ps

def open_price(sym, bar):
    ps, _ = SCALES[sym]
    return int(bar["o"]) / 10**ps

class Ema:
    def __init__(self, span):
        self.span, self.warm, self.value = span, [], None
    def step(self, c):
        if self.value is not None:
            self.value += (2 / (self.span + 1)) * (c - self.value)
            return
        self.warm.append(c)
        if len(self.warm) == self.span:
            self.value = sum(self.warm) / self.span

def run(fast=5, slow=13, mom=6, stop=0.96, tp=1.08, window=None, verbose=False,
        long_span=0, slow_slope=0, seed_entry=False):
    lo, hi = window or (0, N)
    series = {s: {"fast": Ema(fast), "slow": Ema(slow), "long": Ema(long_span) if long_span else None,
                  "slow_hist": [], "closes": [], "gap": None,
                  "now": None, "down": False} for s in SYMBOLS}
    cash = CAPITAL
    pos = {}  # sym -> [qty, cost]
    equity_hist = []
    trades = 0
    pending = []  # orders decided at bar i, fill at i+1: (side, sym, amount_or_qty)
    pnl_half = [0.0, 0.0]
    for i in range(lo, hi):
        # fills from previous bar's decisions, at this bar's open
        for side, sym, amt in pending:
            op = open_price(sym, BARS[sym][i])
            if side == "buy":
                fill_px = op * (1 + SLIP)
                qty = amt * (1 - FEE) / fill_px
                cost = amt
                if sym in pos:
                    pos[sym][0] += qty; pos[sym][1] += cost
                else:
                    pos[sym] = [qty, cost]
                cash -= amt
                trades += 1
            else:
                fill_px = op * (1 - SLIP)
                qty = amt
                if sym in pos and pos[sym][0] > 0:
                    q = min(qty, pos[sym][0])
                    proceeds = q * fill_px * (1 - FEE)
                    frac = q / pos[sym][0]
                    pos[sym][1] *= (1 - frac)
                    pos[sym][0] -= q
                    if pos[sym][0] <= 1e-12:
                        del pos[sym]
                    cash += proceeds
                    trades += 1
        pending = []
        # signals on this bar's close
        cross_up, fresh = set(), set()
        for sym in SYMBOLS:
            bar = BARS[sym][i]
            s = series[sym]
            c = price(sym, bar)
            s["fast"].step(c); s["slow"].step(c)
            if s["long"] is not None:
                s["long"].step(c)
            if s["slow"].value is not None:
                s["slow_hist"].append(s["slow"].value)
                if len(s["slow_hist"]) > slow_slope + 1:
                    s["slow_hist"].pop(0)
            s["closes"].append(c)
            if len(s["closes"]) > mom + 1:
                s["closes"].pop(0)
            s["gap"] = s["now"]
            s["now"] = (s["fast"].value - s["slow"].value) if s["fast"].value is not None and s["slow"].value is not None else None
            if s["now"] is None or s["now"] >= 0:
                s["down"] = False
            fresh.add(sym)
            if s["gap"] is None or s["now"] is None or bar["v"] == "0":
                continue
            mom_ok = (mom == 0 or (len(s["closes"]) >= mom + 1 and s["closes"][-1] > s["closes"][0]))
            long_ok = (s["long"] is None or s["long"].value is None or c > s["long"].value)
            slope_ok = (slow_slope == 0 or len(s["slow_hist"]) < slow_slope + 1
                        or s["slow_hist"][-1] > s["slow_hist"][0])
            if s["now"] is None or bar["v"] == "0":
                continue
            crossed = (s["gap"] is not None and s["now"] > 0 and s["gap"] <= 0) or \
                      (seed_entry and s["gap"] is None and s["now"] > 0)
            if crossed and mom_ok and long_ok and slope_ok:
                cross_up.add(sym)
            if s["gap"] is not None and s["now"] < 0 and s["gap"] >= 0:
                s["down"] = True
        # decisions
        orders = []
        equity = cash + sum(price(sym, BARS[sym][i]) * q for sym, (q, _) in pos.items())
        slots = min(len(SYMBOLS), 10)
        stake = equity / slots
        for sym in SYMBOLS:
            if sym not in fresh:
                continue
            if sym in pos:
                q, cost = pos[sym]
                val = price(sym, BARS[sym][i]) * q
                if series[sym]["down"] or val >= cost * tp or val <= cost * stop:
                    orders.append(("sell", sym, q))
            elif sym in cross_up:
                orders.append(("buy", sym, None))
        sells = [o for o in orders if o[0] == "sell"]
        buys = [o for o in orders if o[0] == "buy"]
        room = slots - len(pos)
        cash_avail = cash
        for _, sym, q in sells[:16]:
            pending.append(("sell", sym, q))
        n_orders = len(sells)
        for _, sym, _ in buys:
            if room <= 0 or n_orders >= 16:
                break
            sl = min(stake, MAX_WEIGHT * equity)
            sl = min(sl, cash_avail)
            if sl < MIN_NOTIONAL:
                break
            pending.append(("buy", sym, sl))
            cash_avail -= sl
            room -= 1
            n_orders += 1
        equity = cash + sum(price(sym, BARS[sym][i]) * q for sym, (q, _) in pos.items())
        equity_hist.append(equity)
    # flatten at last close of window
    final_equity = cash
    for sym, (q, _) in pos.items():
        final_equity += price(sym, BARS[sym][hi - 1]) * q
    peak, dd = CAPITAL, 0.0
    for e in equity_hist:
        peak = max(peak, e)
        dd = max(dd, (peak - e) / peak)
    return {"pnl": final_equity - CAPITAL, "trades": trades, "dd": dd,
            "final_equity": final_equity}

if __name__ == "__main__":
    combos = [
        dict(fast=5, slow=13, mom=12, stop=0.95, tp=1.10, slow_slope=6),
        dict(fast=5, slow=13, mom=12, stop=0.95, tp=1.10, slow_slope=6, seed_entry=True),
        dict(fast=8, slow=21, mom=12, stop=0.94, tp=1.12, long_span=50),
        dict(fast=8, slow=21, mom=12, stop=0.94, tp=1.12, long_span=50, seed_entry=True),
        dict(fast=8, slow=21, mom=12, stop=0.94, tp=1.12),
        dict(fast=8, slow=21, mom=12, stop=0.94, tp=1.12, seed_entry=True),
        dict(fast=5, slow=13, mom=12, stop=0.95, tp=1.10, slow_slope=6, long_span=34),
        dict(fast=5, slow=13, mom=6, stop=0.95, tp=1.10, slow_slope=6),
    ]
    print(f"{'params':55s} {'full':>9s} {'1st half':>9s} {'2nd half':>9s} {'trades':>6s} {'dd':>6s}")
    for c in combos:
        full = run(**c)
        h1 = run(**c, window=(0, 68))
        h2 = run(**c, window=(68, 136))
        print(f"{str(c):55s} {full['pnl']:9.1f} {h1['pnl']:9.1f} {h2['pnl']:9.1f} {full['trades']:6d} {full['dd']:6.3f}")
