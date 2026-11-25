// Momentum bot for agent-fun/bars-replay/v1: beat buy-and-hold on Solana hourly bars.
//
// Strategy: per-symbol EMA crossover, long only, all 7 symbols in the universe.
//   - Indicators: EMA(5) and EMA(13) over each symbol's closes, seeded as the mean of
//     the first `span` closes, then the usual 2 / (span + 1) update. They read every
//     batch from bar 0 (the harness sends no warm-up), so the bot only starts trading
//     once both EMAs are seeded (bar 13 at the earliest).
//   - Entry: EMA(5) crosses above EMA(13) on a bar's close (filled at the next bar's
//     open), on a symbol not already held, and only when the 12-bar momentum is
//     positive (close above the close 12 bars ago) and the slow EMA itself is
//     rising (higher than 6 bars ago), so we buy strength in an established
//     uptrend, not a dead-cat bounce inside a downtrend. A bar with zero volume
//     signals nothing.
//   - Exit, the whole position: EMA(13) crosses back above EMA(5) (the down signal
//     is held while EMA(13) stays above EMA(5), so a refused sell is re-sent), or the
//     position is worth >= 10% over its cost (take profit), or >= 5% under it
//     (stop loss). Exits protect the downside that made buy-and-hold -8.7% on the
//     dev window, while entries keep the bot invested through real uptrends.
//   - Sizing: equal slices of equity over the slots (min(universe, max open
//     positions)), capped by the 25% per-symbol weight cap and the cash on hand;
//     below the minimum notional nothing is bought. At most max_orders_per_bar
//     orders per reply, sells first, then buys in universe order.
//   - Slippage is capped at 100 bps past the close the order was decided on, so a
//     sell into a liquidity gap is refused (and re-sent) rather than filled low.
//   - Deterministic: float64 EMAs on + - * / only, no clock, no randomness, no
//     environment reads, so the two grading runs produce identical action logs.
//
// Local grade-local results (dev window, 136 hourly bars, --driver process):
//   v1: EMA(5/13) + 6-bar momentum filter, stop -4%, take profit +8%:
//       net PnL -331.77 USDC, 61 trades, max drawdown 3.70%
//   v2 (this version): EMA(5/13) + 12-bar momentum + slow-EMA slope filter,
//       stop -5%, take profit +10%:
//       net PnL -57.35 USDC, 18 trades, max drawdown 1.59%, Sharpe -0.04
//   Buy-and-hold on the same window: net PnL -869.34 USDC, 7 trades.
//   v2 beats buy-and-hold by +812 USDC on the dev window. An offline
//   backtester (backtest.py, same rules, fills at next open) was used to check
//   parameter robustness across the two halves of the dev window before v2
//   was picked; v2 was the best of the tested variants that still makes
//   enough trades (>= 8) to clear the guardrail on a 72-bar final window.

import { BPS, runBars, type Bar, type BarsState, type BuyInput, type SellInput } from './vendor/makefun-bars.ts';

const FAST_BARS = 5;
const SLOW_BARS = 13;
const MOMENTUM_BARS = 12;
/** Slow-EMA slope filter: the slow EMA must be higher than 6 bars ago to enter. */
const SLOPE_BARS = 6;
/** Take profit: position worth 110% of its cost. */
const TAKE_PROFIT_NUM = 110n;
const TAKE_PROFIT_DEN = 100n;
/** Stop loss: position worth 95% of its cost. */
const STOP_NUM = 95n;
const STOP_DEN = 100n;
const MAX_SLIPPAGE_BPS = 100;

interface Ema {
  span: number;
  warm: number[];
  value: number | null;
}

interface Series {
  fast: Ema;
  slow: Ema;
  /** Recent closes, oldest first, up to MOMENTUM_BARS + 1 entries. */
  closes: number[];
  /** Recent slow-EMA values, oldest first, up to SLOPE_BARS + 1 entries. */
  slowHist: number[];
  /** fast - slow after the previous bar, once both EMAs are set. */
  gap: number | null;
  /** fast - slow after this bar. */
  now: number | null;
  /** Down signal: set on a cross of EMA(13) above EMA(5), held while it stays above. */
  down: boolean;
}

const series = new Map<string, Series>();

function stepEma(e: Ema, close: number): void {
  if (e.value !== null) {
    e.value += (2 / (e.span + 1)) * (close - e.value);
    return;
  }
  e.warm.push(close);
  if (e.warm.length === e.span) e.value = e.warm.reduce((a, x) => a + x, 0) / e.span;
}

function push(bar: Bar): Series {
  let s = series.get(bar.symbol);
  if (s === undefined) {
    s = {
      fast: { span: FAST_BARS, warm: [], value: null },
      slow: { span: SLOW_BARS, warm: [], value: null },
      closes: [],
      slowHist: [],
      gap: null,
      now: null,
      down: false,
    };
    series.set(bar.symbol, s);
  }
  const c = Number(bar.c);
  stepEma(s.fast, c);
  stepEma(s.slow, c);
  s.closes.push(c);
  if (s.closes.length > MOMENTUM_BARS + 1) s.closes.shift();
  if (s.slow.value !== null) {
    s.slowHist.push(s.slow.value);
    if (s.slowHist.length > SLOPE_BARS + 1) s.slowHist.shift();
  }
  s.gap = s.now;
  s.now = s.fast.value !== null && s.slow.value !== null ? s.fast.value - s.slow.value : null;
  if (s.now === null || s.now >= 0) s.down = false;
  return s;
}

/** 12-bar momentum: the latest close above the close 12 bars ago. */
function momentumUp(s: Series): boolean {
  if (s.closes.length < MOMENTUM_BARS + 1) return false;
  return s.closes[s.closes.length - 1]! > s.closes[0]!;
}

/** Slow-EMA slope: the slow EMA is higher than it was SLOPE_BARS bars ago. */
function slopeUp(s: Series): boolean {
  if (s.slowHist.length < SLOPE_BARS + 1) return false;
  return s.slowHist[s.slowHist.length - 1]! > s.slowHist[0]!;
}

/** Whether a held position exits on this close: down signal, take profit or stop loss. */
function exits(state: BarsState, symbol: string, held: { qty: bigint; cost: bigint }, signal: boolean): boolean {
  if (signal) return true;
  const value = state.value(symbol, held.qty);
  if (value * TAKE_PROFIT_DEN >= held.cost * TAKE_PROFIT_NUM) return true;
  return value * STOP_DEN <= held.cost * STOP_NUM;
}

await runBars({
  onBatch(state, batch) {
    const crossUp = new Set<string>();
    const fresh = new Set<string>();
    for (const bar of batch.bars) {
      const s = push(bar);
      fresh.add(bar.symbol);
      if (s.gap === null || s.now === null || bar.v === '0') continue;
      if (s.now > 0 && s.gap <= 0 && momentumUp(s) && slopeUp(s)) crossUp.add(bar.symbol);
      if (s.now < 0 && s.gap >= 0) s.down = true;
    }
    if (state.warmingUp) return [];
    const { caps, fees } = state.hello;
    const sells: SellInput[] = [];
    const entries: string[] = [];
    for (const { symbol } of state.hello.symbols) {
      if (!fresh.has(symbol)) continue;
      const held = state.positions.get(symbol);
      if (held !== undefined) {
        if (exits(state, symbol, held, series.get(symbol)!.down)) {
          sells.push({ side: 'sell', symbol, qty: held.qty, max_slippage_bps: MAX_SLIPPAGE_BPS });
        }
      } else if (crossUp.has(symbol)) {
        entries.push(symbol);
      }
    }

    const orders: (SellInput | BuyInput)[] = sells.slice(0, caps.max_orders_per_bar);
    const slots = Math.min(state.hello.symbols.length, caps.max_open_positions);
    const stake = state.equity / BigInt(slots);
    const byWeight = (BigInt(caps.max_weight_bps) * state.equity) / BPS;
    let room = slots - state.positions.size;
    let cash = state.cash;
    for (const symbol of entries) {
      if (room <= 0 || orders.length >= caps.max_orders_per_bar) break;
      let slice = stake < byWeight ? stake : byWeight;
      if (cash < slice) slice = cash;
      if (slice === 0n || slice < BigInt(fees.min_notional_quote_units)) break;
      orders.push({ side: 'buy', symbol, notional_quote_units: slice, max_slippage_bps: MAX_SLIPPAGE_BPS });
      cash -= slice;
      room--;
    }
    return orders;
  },
});
