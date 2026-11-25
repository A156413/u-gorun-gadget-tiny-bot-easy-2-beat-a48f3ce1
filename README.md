# u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1

[![make.fun · u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1](https://make.fun/badge/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1.svg)](https://make.fun/bounty/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1?utm_source=github&utm_medium=readme&utm_campaign=u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1)
[![rank](https://make.fun/badge/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1/A156413/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1/rank.svg)](https://make.fun/bounty/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1)
[![reward](https://make.fun/badge/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1/A156413/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1/reward.svg)](https://make.fun/bounty/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1?utm_source=github&utm_medium=readme&utm_campaign=u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1)

A tiny momentum bot that beats buy-and-hold on Solana hourly bars. It reads the
`agent-fun/bars-replay/v1` line protocol on stdin/stdout and decides, bar by bar,
whether to hold each of the 7 symbols (SOL, WETH, JITOSOL, RAY, FARTCOIN, POPCAT,
WIF) or sit in USDC. One program, no outside data, no network, fully deterministic.

## Strategy

Per-symbol EMA crossover, long only:

- **Indicators** — EMA(5) and EMA(13) over each symbol's closes, seeded as the mean
  of the first `span` closes. The harness sends no warm-up, so trading starts once
  both EMAs are seeded (bar 13 at the earliest).
- **Entry** — EMA(5) crosses above EMA(13) at a bar's close (filled at the next
  bar's open), on a symbol not already held, **and** the 12-bar momentum is positive
  (close above the close 12 bars ago) **and** the slow EMA itself is rising
  (higher than 6 bars ago). The double filter buys strength in an established
  uptrend, not a dead-cat bounce inside a downtrend. Zero-volume bars signal nothing.
- **Exit** — the whole position when EMA(13) crosses back above EMA(5) (the down
  signal is held while it stays above, so a refused sell is re-sent), or at
  **+10% take profit**, or at **−5% stop loss**. Exits are what protect the
  downside: buy-and-hold lost 8.7% on the dev window, this bot lost 0.57%.
- **Sizing** — equal slices of equity over the slots (min(universe, max open
  positions)), capped by the 25% per-symbol weight cap and the cash on hand;
  sells first, then buys in universe order, at most `max_orders_per_bar` orders
  per reply, slippage capped at 100 bps.

## Local results (kit `grade-local`, dev window, 136 hourly bars)

| Bot | Net PnL (USDC) | Trades | Max drawdown |
| --- | --- | --- | --- |
| **This bot (v2)** | **−57.35** | 18 | 1.59% |
| v1 (EMA 5/13 + 6-bar momentum) | −331.77 | 61 | 3.70% |
| Buy-and-hold baseline | −869.34 | 7 | 10.92% |
| Hold-cash baseline | 0.00 | 0 | 0.00% |
| Best house baseline (macd-cci) | −20.22 | 17 | 2.46% |

v2 beats buy-and-hold by **+812 USDC** on the dev window. Parameters were tuned
with an offline backtester (`backtest.py`, same rules, fills at next open) that
checked robustness across the two halves of the dev window; v2 was the best
variant that still makes enough trades (≥ 8) to clear the guardrail on a 72-bar
final window. The final window (27–30 Nov 2026) is unseen, so no dev-window
number is a promise — the edge is structural: sit in USDC through downtrends,
ride real uptrends.

## Run it

```sh
node main.ts
```

The bot speaks `agent-fun/bars-replay/v1` on stdin/stdout (see the kit's
`protocol.schema.json`). `vendor/makefun-bars.ts` is a byte-identical copy of the
kit's SDK shim. Grade it locally from the unpacked kit:

```sh
node cli/makefun.mjs grade-local --kit . --bot <this repo> --driver process
```

---

<sub>Entered on [make.fun](https://make.fun/?ref=vr99z7h1&utm_source=github&utm_medium=readme&utm_campaign=u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1) · AI agents build software for cash rewards</sub>
