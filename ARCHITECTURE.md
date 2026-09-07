# Architecture

Trading-Scanner separates data ingestion (on its own schedules) from
analysis (agents reading from a shared store) from decision-making
(a weighted orchestrator). Nothing in the analysis layer calls a
market data provider directly — it only reads from `store/`.

## Ingestion tiers (`ingestion/`, config in `config/schedule.yaml`)

| Tier | Cadence | Data | Store table(s) |
|---|---|---|---|
| 1 | Interval, configurable start/end time | News & sentiment | `news_sentiment` |
| 2 | Once daily, per-job scheduled time | Insider transactions, congress trades, politician metadata, institutional holdings | `insider_transactions`, `congress_trades`, `politician_metadata`, `institutional_holdings` |
| 3 | Event-triggered (earnings calendar or corporate-action watcher) | Fundamentals | `fundamentals`, `corporate_action_flags` |
| 4 | Interval, market hours | Technical indicators, core OHLCV, index, options | `technical_snapshots`, `index_snapshots`, `option_snapshots` |

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
