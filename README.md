# pumpfun-top5-forecast-01: forecast the top 5 new pump.fun coins by market cap, 24 hours out

Entry for the [make.fun bounty "Predict the top 5 new Solana coins by market cap, 24 hours out"](https://make.fun/bounty/pumpfun-top5-forecast-01) (prize pool $200).

## What it does

`main.py` (Python 3.13, standard library only, no network) reads the house's
10-minute market-cap snapshots of every pump.fun coin launched since the
opening (`$MAKEFUN_INPUT_DIR/history.ndjson` + `task.json`) and writes one
forecast to `$MAKEFUN_OUTPUT_DIR/forecast.json`: per-coin probabilities for
each of the top 5 places, the top 5 in order, and per-place market-cap size
histograms over the 82 log10 bins.

Method: a seeded Monte Carlo (3,600 trials) over the seven answer reads.

- Each ranked coin gets a future log-cap path: recent momentum, shrunk
  toward a venue prior and decaying toward a slow long-run bleed, a
  horizon-scaled death hazard (most new coins collapse to a ~10-30 SOL
  floor), and volatility that grows with the horizon but is capped so the
  maximum is never won by a lottery ticket.
- Coins born after the history ends cannot be named, yet they take a large
  share of top-5 places (in the practice rehearsal, 2 of the top 5 at the
  last read were born after the cut). They are simulated as synthetic
  newcomers: launch rate and peak-cap tail fitted from the history's own
  launches, rising fast to a peak then bleeding off. The two fits are the
  creative core of this entry, and both are measured, not guessed:
  - Launch rate: the raw count of >=100 SOL peaks divided by the window
    is heavily censored on a minutes-long history (a coin needs ~10-30
    min to reach its peak), so a short window gets a x3.0 censoring
    inflation; a long window uses a mature-cohort estimator (launch rate
    from the recorder's eligible counts times the share of coins launched
    >=30 min ago that ever peaked >=100 SOL). On the practice this
    predicts 38 big launches/hour for the full 50-min window vs 35/hour
    actually realised, while the raw-count estimators give 52-105/hour.
  - Peak tail: the history's own mature peaks mixed with an exponential
    tail (mean 0.8 in log10). Among mature coins the mean excess over
    100 SOL is ~0.7 and 35% of big coins exceed 1,000 SOL.
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

## Local backtest (practice history, truncated, 4 cuts / 9 answer reads)

Scored with a local scorer that reproduces the rules' worked example
exactly. Log score, higher is better; delta is vs the house baseline
starter on the same truncated history.

| Cut | Answers | Model | Baseline | Delta |
| --- | --- | --- | --- | --- |
| 20:10 | 20:30/20:40/20:50 | -36.75 | -64.88 | +28.13 |
| 20:20 | 20:30/20:40/20:50 | -36.98 | -49.96 | +12.98 |
| 20:30 | 20:40/20:50 | -27.98 | -33.48 | +5.50 |
| 20:40 | 20:50 | -26.11 | -29.87 | +3.76 |
| Aggregate (9 reads) | | -33.70 | -49.04 | +15.34 |

The first version of this entry scored +5.82 on the same ladder; the
improvement comes from the newcomer-rate and peak-tail fits above (the
span floor and censoring fix alone took it to +10.98, the heavier tail
to +13.50, the x3.0 short-window censoring to +15.29). The gap grows
with horizon, which is where the final is decided (7-13 hours out).
Variants that did not help and were rejected: size-distribution
smoothing 0.95/0.99, newcomer cap 400 to 700, newcomer bleed -0.02 /
-0.08, and naming thresholds 0.0015 / 0.006 (all within noise of the
chosen version).
