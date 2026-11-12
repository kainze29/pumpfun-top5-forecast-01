"""Forecast the top 5 new pump.fun coins by market cap, 24 hours out.

Reads the house's 10-minute market-cap snapshots at $MAKEFUN_INPUT_DIR
(default /input): history.ndjson + task.json. Writes one forecast to
$MAKEFUN_OUTPUT_DIR/forecast.json (default /output/forecast.json):
probabilities (per coin per place), top5, sizes (per place per log10 bin).

Method (stdlib only, fully deterministic, seeded Monte Carlo):
  * Every ranked coin gets a future log10 market-cap path: its recent
    momentum, shrunk toward a venue prior and decaying toward a slow
    long-run bleed, plus a "death" mixture (most new coins collapse to a
    ~10-30 SOL floor) and volatility that grows with the horizon.
  * Coins born after the history ends cannot be named, yet they take a
    large share of top-5 places (in the practice 2 of the top 5 at the
    last read were born after the cut). They are simulated as synthetic
    newcomers: launch rate and peak-cap tail fitted from the history's
    own launches, launched uniformly between the cut and each answer
    read, rising fast to a peak then bleeding off.
  * One seeded Monte Carlo ranks known coins + synthetic newcomers at
    each of the seven answer reads; place frequencies become the
    probabilities (the residual mass is left unnamed, which is exactly
    the newcomers' share), and the k-th largest cap per read becomes
    each place's size histogram over the 82 bins.
  * Nothing is hard-coded to the practice window: horizons, places and
    bins are read from task.json, so the same code runs the 2-hour dev
    rehearsal and the 24-hour final.
"""
import bisect
import json
import math
import os
import random
from datetime import datetime, timezone

SEED = 20261112
N_TRIALS = 1200
MAX_CANDIDATES = 220
MAX_NEWCOMERS_PER_READ = 400
EMIT_P_MIN = 0.003
EMIT_BIN_MIN = 0.0015

# Priors (shrunk toward these when the history is thin, e.g. the 6-mark
# practice rehearsal; the final's 66 marks dominate them).
TAU_H = 1.5            # momentum persistence, hours
D_INF = -0.02          # long-run bleed of a surviving runner, log10/hour
D_INF_NEW = -0.04      # post-peak bleed of a newcomer, log10/hour
RISE_RATE = 1.5        # log10/hour a newcomer climbs toward its peak
PEAK_AGE_H = 0.30      # hours after launch a newcomer typically peaks
FLOOR_MU = 1.15        # log10 SOL of a dead coin (~14 SOL)
FLOOR_SD = 0.35
READ_NOISE = 0.08      # extra per-read jitter, log10


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def logcap_sol(raw):
    return math.log10(int(raw)) - 9.0


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    if n == 0:
        return 0.0
    return xs[n // 2] if n % 2 else 0.5 * (xs[n // 2 - 1] + xs[n // 2])


def clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


def main():
    in_dir = os.environ.get("MAKEFUN_INPUT_DIR", "/input")
    out_dir = os.environ.get("MAKEFUN_OUTPUT_DIR", "/output")
    with open(os.path.join(in_dir, "task.json")) as f:
        task = json.load(f)
    places = int(task.get("places", 5))
    edges = [float(e) for e in task.get("size", {}).get("edges", [])]
    if len(edges) != 81:
        edges = [round(2.0 + 0.05 * i, 2) for i in range(81)]
    reads = [parse_ts(s) for s in task.get("reads", [])]
    t0 = parse_ts(task["t0"]) if task.get("t0") else None

    marks = []
    with open(os.path.join(in_dir, "history.ndjson")) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if d.get("ok") is True and isinstance(d.get("rows"), list):
                marks.append(d)
    if not marks:
        raise SystemExit("the history holds no ok mark")
    marks.sort(key=lambda m: m["at"])
    t_last = parse_ts(marks[-1]["at"])
    if not reads:
        # Fallback: seven hourly reads starting 7h after the last mark.
        reads = [t_last.replace(minute=0, second=0, microsecond=0)]
        from datetime import timedelta
        reads = [reads[0] + timedelta(hours=h) for h in range(7)]
    horizons = [(r - t_last).total_seconds() / 3600.0 for r in reads]
    horizons = [max(h, 0.05) for h in horizons]

    # ---- build per-coin series -------------------------------------------
    series = {}   # mint -> dict
    history_mints = set()
    for m in marks:
        t = parse_ts(m["at"])
        for r in m["rows"]:
            mint = r["mint"]
            history_mints.add(mint)
            c = series.get(mint)
            if c is None:
                c = series[mint] = {"obs": [], "created": None,
                                    "venue": r.get("venue"),
                                    "complete": bool(r.get("complete")),
                                    "mayhem": bool(r.get("mayhem"))}
            c["obs"].append((t, logcap_sol(r["market_cap_lamports"])))
            c["venue"] = r.get("venue")
            c["complete"] = bool(r.get("complete"))
            c["mayhem"] = bool(r.get("mayhem"))
            try:
                c["created"] = parse_ts(r["created_at"])
            except Exception:
                pass

    # ---- global statistics from the history ------------------------------
    # Consecutive-mark moves, by venue and size bucket.
    pool_moves, curve_moves = [], []
    vol_by_bucket = {}
    launches = []          # (created_at, peak logcap) for coins born in-window
    prev = {}
    for m in marks:
        t = parse_ts(m["at"])
        for r in m["rows"]:
            mint, lc = r["mint"], logcap_sol(r["market_cap_lamports"])
            if mint in prev:
                dt = (t - prev[mint][0]).total_seconds() / 3600.0
                if dt > 0:
                    d = (lc - prev[mint][1]) / dt  # log10 per hour
                    rec = (d, lc, r.get("venue"))
                    if r.get("venue") == "pool":
                        pool_moves.append(rec)
                    elif r.get("venue") == "curve":
                        curve_moves.append(rec)
                    b = 3 if lc >= 3 else 2 if lc >= 2.5 else 1 if lc >= 2 else 0
                    vol_by_bucket.setdefault(b, []).append(abs(lc - prev[mint][1]))
            prev[mint] = (t, lc)
    pool_drift = median([d for d, _, _ in pool_moves]) if pool_moves else 0.0
    curve_drift = median([d for d, _, _ in curve_moves]) if curve_moves else -0.05
    pool_drift = clamp(pool_drift, -0.2, 0.4)
    curve_drift = clamp(curve_drift, -0.4, 0.2)

    def vol10(lc):
        # Median absolute 10-minute move in this size bucket, when the
        # history has enough pairs; else a prior from the same buckets.
        # Small dead coins barely move (median |d| ~ 0): their risk is a
        # jump, carried by the slope/death mixture, not by a wide sigma.
        # A wide sigma on hundreds of tiny coins would let the Monte
        # Carlo's maximum be won by a lottery ticket at 10^6 SOL.
        b = 3 if lc >= 3 else 2 if lc >= 2.5 else 1 if lc >= 2 else 0
        prior = {0: 0.08, 1: 0.25, 2: 0.25, 3: 0.10}[b]
        xs = vol_by_bucket.get(b, [])
        v = median([abs(x) for x in xs]) * 1.5 if len(xs) >= 5 else prior
        return clamp(v, 0.05, 0.30)

    # Peak caps of coins launched during the observed window.
    for mint, c in series.items():
        created = c["created"] or (c["obs"][0][0] if c["obs"] else t0)
        if created is None:
            continue
        peak = max(l for _, l in c["obs"])
        launches.append(peak)
    big_peaks = sorted(p for p in launches if p >= 2.0)
    span_h = (max((t_last - t0).total_seconds() / 3600.0, 0.5)
              if t0 is not None else 1.0)
    rate_big = 1.5 * len(big_peaks) / span_h  # x1.5: coins launched late
    # in the window are censored, their peaks not yet observed
    rate_big = clamp(rate_big, 5.0, 150.0)

    # ---- per-coin forecast parameters ------------------------------------
    coins = []
    for mint, c in series.items():
        obs = c["obs"]
        if not obs:
            continue
        l0 = obs[-1][1]
        venue = c["venue"] or "curve"
        # Recent momentum: OLS slope over the last up-to-6 observations.
        recent = obs[-6:]
        slope = None
        if len(recent) >= 2:
            tref = recent[0][0]
            xs = [(t - tref).total_seconds() / 3600.0 for t, _ in recent]
            ys = [l for _, l in recent]
            mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
            den = sum((x - mx) ** 2 for x in xs)
            if den > 1e-9:
                slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
        prior = (clamp(pool_drift, -0.1, 0.15) if venue == "pool" else (
            0.10 if venue == "migrating" else curve_drift))
        if slope is None:
            slope_s = prior
        else:
            w = len(recent) / (len(recent) + 3.0)
            slope_s = clamp(w * slope + (1 - w) * prior, -0.6, 0.6)
        created = c["created"] or obs[0][0]
        age_h = max((t_last - created).total_seconds() / 3600.0, 0.0)
        # Death hazard (per hour): most new coins collapse, but the
        # hazard scales with the horizon, a big graduated runner with
        # rising momentum survives far longer than a small curve coin.
        # P(dead by horizon h) = 1 - exp(-hazard * h).
        hazard = 0.10 if venue == "pool" else (0.35 if venue == "migrating" else 0.60)
        if slope_s > 0.15:
            hazard *= 0.55
        elif slope_s < -0.15:
            hazard *= 1.4
        if l0 >= 3.2:
            hazard *= 0.6
        elif l0 < 2.0:
            hazard *= 1.3
        if age_h > 4:
            hazard *= 0.8
        hazard = clamp(hazard, 0.02, 1.5)
        coins.append({"mint": mint, "l0": l0, "slope": slope_s,
                      "venue": venue, "vol": vol10(l0), "hazard": hazard,
                      "age_h": age_h})
    coins.sort(key=lambda c: -c["l0"])
    # Candidate set: the largest coins plus every sizeable graduated one.
    cands = coins[:MAX_CANDIDATES]
    if len(coins) > MAX_CANDIDATES:
        extra = [c for c in coins[MAX_CANDIDATES:]
                 if c["venue"] == "pool" and c["l0"] >= 2.0][:40]
        cands = cands + extra
    n_c = len(cands)
    if n_c < places:
        raise SystemExit("the history ranks fewer coins than the places")

    n_reads = len(reads)

    # Precompute per-coin drift path per horizon: mu(h) and sigma(h).
    mus, sigmas = [], []
    for c in cands:
        mu_r, sg_r = [], []
        for h in horizons:
            decay = TAU_H * (1.0 - math.exp(-h / TAU_H))
            mu = c["l0"] + c["slope"] * decay + D_INF * (h - decay)
            sg = 0.10 + c["vol"] * math.sqrt(6.0 * h)
            mu_r.append(mu)
            sg_r.append(min(sg, 0.45))
        mus.append(mu_r)
        sigmas.append(sg_r)

    def newcomer_peak():
        # Peak-cap tail of a newcomer (>= 100 SOL). Most draws come from
        # the history's own observed peaks (jittered); a minority from an
        # exponential tail above 100 SOL (mean 0.4 in log10), because a
        # short history censors peaks and a longer launch window breeds
        # a somewhat bigger maximum than the observed one. The tail is
        # kept modest on purpose: the house baseline's practice size
        # score (-19.03, centred on the last mark's caps with sd 0.35)
        # shows the true caps stayed within roughly a factor 2-3 of the
        # last mark's, so a tail that routinely put place 1 at 25,000
        # SOL would be dishonest, not bold.
        if big_peaks and rng.random() < 0.6:
            return rng.choice(big_peaks) + rng.uniform(-0.05, 0.05)
        return min(2.0 + rng.expovariate(1.0 / 0.4), 4.8)

    # ---- Monte Carlo ------------------------------------------------------
    rng = random.Random(SEED)
    place_counts = [[0] * n_c for _ in range(places)]
    place_caps = [[] for _ in range(places)]
    for _ in range(N_TRIALS):
        z = [rng.gauss(0.0, 1.0) for _ in range(n_c)]
        u_death = [rng.random() for _ in range(n_c)]
        floor = [rng.gauss(FLOOR_MU, FLOOR_SD) for _ in range(n_c)]
        for ri, h in enumerate(horizons):
            known = []
            for i, c in enumerate(cands):
                if u_death[i] < 1.0 - math.exp(-c["hazard"] * h):
                    v = floor[i] + rng.gauss(0.0, 0.10)
                else:
                    v = mus[i][ri] + sigmas[i][ri] * z[i] + rng.gauss(0.0, READ_NOISE)
                known.append((v, i))
            hours_new = h
            lam = rate_big * hours_new
            n_new = int(round(rng.gauss(lam, math.sqrt(max(lam, 1.0)))))
            n_new = max(0, min(n_new, MAX_NEWCOMERS_PER_READ))
            new_caps = []
            for _ in range(n_new):
                peak = newcomer_peak()
                launch_age = rng.uniform(0.0, hours_new)
                if launch_age < PEAK_AGE_H:
                    v = peak - RISE_RATE * (PEAK_AGE_H - launch_age)
                else:
                    v = peak + D_INF_NEW * (launch_age - PEAK_AGE_H)
                v += rng.gauss(0.0, 0.15)
                new_caps.append(v)
            # Merge: rank known coins against newcomers.
            new_caps.sort(reverse=True)
            neg = [-x for x in new_caps]  # ascending, for bisect
            known_sorted = sorted(known, key=lambda kv: -kv[0])
            # For each known coin, its place = 1 + #newcomers above it
            # + #known above it.
            for rank_i, (v, i) in enumerate(known_sorted):
                above = bisect.bisect_left(neg, -v)
                place = rank_i + above + 1
                if 1 <= place <= places:
                    place_counts[place - 1][i] += 1
            all_caps = [v for v, _ in known] + new_caps
            all_caps.sort(reverse=True)
            top = all_caps[:places]
            for k in range(places):
                place_caps[k].append(top[k])

    # ---- build the forecast ----------------------------------------------
    probabilities = []
    denom = N_TRIALS * n_reads  # tallies accumulate over all reads; the
    # forecast is one distribution scored at each read, so average.
    for k in range(places):
        row = []
        for i, c in enumerate(cands):
            p = place_counts[k][i] / denom
            if p >= EMIT_P_MIN:
                row.append({"mint": c["mint"], "place": k + 1, "p": round(p, 6)})
        row.sort(key=lambda r: (-r["p"], r["mint"]))
        probabilities.extend(row[:100])

    sizes = []
    for k in range(places):
        hist = [0] * (len(edges) + 1)
        for v in place_caps[k]:
            if v < edges[0]:
                b = 0
            elif v >= edges[-1]:
                b = len(edges)
            else:
                b = next(i for i, e in enumerate(edges) if v < e)
            hist[b] += 1
        total = len(place_caps[k])
        for b in range(len(hist)):
            p = 0.97 * (hist[b] / total) + 0.03 / (len(edges) + 1)
            if p >= EMIT_BIN_MIN:
                sizes.append({"place": k + 1, "bin": b, "p": round(p, 6)})
    sizes.sort(key=lambda r: (r["place"], r["bin"]))

    order_idx = sorted(range(n_c),
                       key=lambda i: (-sum(place_counts[k][i] * (places + 1 - (k + 1))
                                           for k in range(places)), cands[i]["mint"]))
    top5 = [cands[i]["mint"] for i in order_idx[:places]]
    if len(top5) < places:  # pragma: no cover - degenerate history
        pad = ["11111111111111111111111111111111",
               "22222222222222222222222222222222",
               "33333333333333333333333333333333",
               "44444444444444444444444444444444",
               "55555555555555555555555555555555"]
        for p in pad:
            if len(top5) >= places:
                break
            if p not in top5:
                top5.append(p)

    forecast = {"probabilities": probabilities, "top5": top5, "sizes": sizes}
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, task.get("output", "forecast.json"))
    with open(out_path, "w") as f:
        f.write(json.dumps(forecast, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
