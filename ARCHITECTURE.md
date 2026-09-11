# Architecture

Trading-Scanner separates data ingestion (on its own schedules) from
analysis (agents reading from a shared store) from decision-making
(a weighted orchestrator). Nothing in the analysis layer calls a
market data provider directly — it only reads from `store/`.

## Ingestion tiers (`ingestion/`, config in `config/schedule.yaml`)

| Tier | Cadence | Data | Store table(s) |
|---|---|---|---|
| 1 | Daily at the configured extraction time | Fundamental extraction for the stock watchlist | `fundamentals`, `corporate_action_flags` |
| 2 | Frequent interval, configurable start/end time | News & sentiment | `news_sentiment` |
| 3 | Once daily, per-job scheduled time | High-value insider transactions, congress trades, politician metadata, institutional holdings | `insider_transactions`, `congress_trades`, `politician_metadata`, `institutional_holdings` |
| 4 | Daily baseline plus Tier 2/3 catalyst events | Technical indicators, core OHLCV, index, options | `technical_snapshots`, `index_snapshots`, `option_snapshots` |

Daily reference ingestion applies configurable minimum-value filters before
writing insider, congressional, or institutional rows. Small transactions are
discarded rather than passed to the sentiment agent. The news workflow also
produces a deterministic three-row Market Sentiment & Flow rubric covering
news catalysts, insider/institutional flow, and macro/topic alignment.
Tier 4 stores a corresponding rubric covering technical trend,
intraday momentum/SMC, and options flow/volatility.

## Agent layer (`agents/`)

Four independent agents (technical, fundamental, sentiment, macro) run
in parallel, each reading only its relevant store tables for a symbol.
Each returns a fixed schema: `{agent_name, direction, confidence,
key_signals, rationale}`, logged to `agent_signals`.

## Orchestrator (`orchestrator/`)

Combines the four agent outputs with weighted scoring:

```
composite = Σ (weight_i × confidence_i × direction_i)   direction_i ∈ {-1, 0, +1}
```

Handles partial results (an agent that timed out or errored is marked
`available=False` and its weight is redistributed across the rest,
rather than silently dropped). Final calls are logged to
`final_signals`, which is also where trade-outcome tracking lives —
once `outcome_correct` starts filling in, agent weights can be tuned
against real accuracy instead of guessed.

## Skills (`skills/`)

`swing_trade_scanner` and `catalyst_confluence_scanner` are scheduled
consumers of the store (see `config/schedule.yaml` for their run
times) — they no longer make live API calls themselves. Each pulls
the freshest relevant slice of the store for its candidate list and
runs its existing scoring logic (the 17-point technical rubric, or the
SMC/momentum TVRemix funnel) against that data.
