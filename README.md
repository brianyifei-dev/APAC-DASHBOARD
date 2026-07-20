# AUS Dashboard (HOSTPLUS ETF board)

Auto-refreshing dashboard for the HOSTPLUS ETF tab: ASX-listed category/theme/global/income ETFs plus US market benchmarks.

Same architecture as the US theme board:
- `scripts/build_data.py` fetches via yfinance (ASX tickers use .AX suffix through the `yahoo` field in universe.json)
- `.github/workflows/refresh.yml` crons at 21:15 & 22:15 UTC (~08:15 Sydney)
- GitHub Pages serves `docs/`

Setup: push to a new repo -> Settings > Actions > General > Read and write permissions -> Actions > Run workflow -> Settings > Pages > main /docs.
