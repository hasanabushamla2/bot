# Runtime Evidence — Master Runtime Rebuild

## HEAD: ecf776a

## Module Wiring Proof

| Module | Imported | Instantiated | Called | Affects Decision | Runtime Test |
|--------|----------|-------------|--------|-----------------|--------------|
| RealTimeDataEngine | ✅ | ✅ | ✅ (process_ticker entry) | ✅ | ✅ |
| EventBus | ✅ | ✅ | ✅ (start/shutdown) | ⚠️ (not yet subscriber-based) | ✅ |
| OrderBookEngine | ✅ | ✅ | ✅ (process_ticker updates) | ✅ (bid/ask for fills) | ✅ |
| FeedHealthMonitor | ✅ | ✅ | ✅ (record_message) | ✅ (gates entry) | ✅ |
| UniverseManager | ✅ | ✅ | ✅ (register, update_liquidity) | ✅ (gates entry) | ✅ |
| AssetQualityFilter | ✅ | ✅ | ✅ (assess) | ✅ (gates entry) | ✅ |
| FeatureEngine | ✅ | ✅ | ✅ (update_price/book/vol) | ✅ | ✅ |
| GlobalScanner | ✅ | ✅ | ✅ (scan, to_strategy_signals) | ✅ | ✅ |
| StrategyRegistry | ✅ | ✅ | ✅ (register, initialize) | ✅ | ✅ |
| OpportunityEngine | ✅ | ✅ | ✅ (evaluate_batch) | ✅ | ✅ |
| RiskEngine | ✅ | ✅ | ✅ (update_state, assess) | ✅ | ✅ |
| CapitalTierManager | ✅ | ✅ | ✅ (determine_tier) | ✅ | ✅ |
| CapitalAllocator | ✅ | ✅ | ✅ (allocate) | ✅ | ✅ |
| PaperExecutionEngine | ✅ | ✅ | ✅ (simulate_fill buy/sell) | ✅ | ✅ |
| PaperAccount | ✅ | ✅ | ✅ (open/close_position) | ✅ | ✅ |
| PositionMonitor | ✅ | ✅ | ✅ (check_all, register) | ✅ | ✅ |
| AnalyticsTracker | ✅ | ✅ | ✅ (record_*) | ✅ | ✅ |

## Runtime Event Counters (from test run)

| Metric | Value |
|--------|-------|
| RealTimeDataEngine events | N/A (replay mode) |
| Feed health records | per ticker processed |
| Universe assets registered | len(_canonical_symbols) |
| Quality filter assessments | per eligible snapshot per scan |
| Scanner calls | 1 per scan tick |
| Strategy signals | variable |
| Opportunity evaluations | per signal batch |
| RiskEngine update_state calls | 1 per scan tick |
| RiskEngine assess calls | per opportunity |
| Allocator calls | per approved opportunity |
| PaperExecution calls | per entry + per exit |
| Account mutations | only via PaperExecution fills |

## E2E Test Coverage

| Scenario | Test | Result |
|----------|------|--------|
| Full orchestrator instantiation | test_instantiates_all_modules | PASS |
| Ticker→features pipeline | test_process_ticker_updates_features | PASS |
| Ticker→order_book | test_process_ticker_updates_order_book | PASS |
| Ticker→feed_health | test_process_ticker_updates_feed_health | PASS |
| Ticker→universe | test_process_ticker_updates_universe | PASS |
| Feed+scan no crash (rising) | test_feed_and_scan_does_not_crash | PASS |
| Feed+scan no crash (falling) | test_falling_prices_no_crash | PASS |
| Spot-only enforcement | test_spot_only_short_rejected | PASS |
| Hard stop preserved | test_hard_stop_030_preserved | PASS |
| Trail delta preserved | test_trailing_delta_002_preserved | PASS |
| No fixed TP | test_no_fixed_take_profit | PASS |
| No phantom capital | test_paper_account_no_phantom_capital | PASS |
| Accounting random seq | test_accounting_random | PASS |
| Buy uses ask | test_buy_uses_ask | PASS |
| Sell uses bid | test_sell_uses_bid | PASS |
| Partial fill | test_partial_fill_no_oversell | PASS |
| Micro win scenario | test_win_scenario | PASS |
| Micro loss scenario | test_loss_scenario (P&L < 0) | PASS |
| Micro cap enforced | test_cap_enforced | PASS |

