# pumpfun-top5-forecast-01: forecast the top 5 new pump.fun coins by market cap, 24 hours out

Entry for the [make.fun bounty "Predict the top 5 new Solana coins by market cap, 24 hours out"](https://make.fun/bounty/pumpfun-top5-forecast-01) (prize pool $200).

## What it does

`main.py` (Python 3.13, standard library only, no network) reads the house's
10-minute market-cap snapshots of every pump.fun coin launched since the
opening (`$MAKEFUN_INPUT_DIR/history.ndjson` + `task.json`) and writes one
forecast to `$MAKEFUN_OUTPUT_DIR/forecast.json`: per-coin probabilities for
each of the top 5 places, the top 5 in order, and per-place market-cap size
histograms over the 82 log10 bins.

Method: a seeded Monte Carlo (1,200 trials) over the seven answer reads.

- Each ranked coin gets a future log-cap path: recent momentum, shrunk
  toward a venue prior and decaying toward a slow long-run bleed, a
  horizon-scaled death hazard (most new coins collapse to a ~10-30 SOL
  floor), and volatility that grows with the horizon but is capped so the
  maximum is never won by a lottery ticket.
- Coins born after the history ends cannot be named, yet they take a large
  share of top-5 places (in the practice rehearsal, 2 of the top 5 at the
  last read were born after the cut). They are simulated as synthetic
  newcomers: launch rate and peak-cap tail fitted from the history's own
  launches, rising fast to a peak then bleeding off.
- Place frequencies over the trials become the probabilities (the residual
  mass is left unnamed, which is exactly the newcomers' share); the k-th
  largest cap per read becomes each place's size histogram.

Nothing is hard-coded to the practice window: horizons, places and bins are
read from `task.json`, so the same code runs the 2-hour dev rehearsal and
the 24-hour final. Runs are deterministic: two runs on the same history
produce byte-identical output.

## Run it locally

```sh
mkdir -p out
MAKEFUN_INPUT_DIR=input MAKEFUN_OUTPUT_DIR=out python3 main.py
```

where `input/` holds the kit's `history.ndjson` and `task.json`.

## Local backtest (practice history, truncated)

Scored with a local scorer that reproduces the rules' worked example
exactly. Training on marks up to 20:30, answers 20:40/20:50: this entry
-29.67 vs the house baseline starter -33.48 (log score, higher is better).
Training on marks up to 20:10, answers 20:30/20:40/20:50: -56.02 vs
-64.88. The gap grows with horizon, which is where the final is decided
(7-13 hours out).
