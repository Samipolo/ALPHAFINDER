# A1 Trading AlphaFinder — Real Data Dashboard

## What Each Data Source Does

| Column | Source | Method |
|--------|--------|--------|
| **Trend** | yfinance (Yahoo Finance) | Real SMA20/50/200 from live daily OHLCV data |
| **Seasonality** | yfinance | Real 10-year monthly average returns calculated in Pandas |
| **COT** | CFTC Socrata Public API | Direct fetch from `publicreporting.cftc.gov` — no key needed |
| **Crowd (Retail)** | Myfxbook / Dukascopy | Scraped from public community outlook pages |
| **GDP** | FRED / Official releases | Scored from real QoQ GDP data per country |
| **mPMI / sPMI** | S&P Global PMI releases | Real PMI values per currency scored on standard scale |
| **Retail Sales** | FRED + equivalent | Real MoM retail sales change scored |
| **Consumer Conf** | FRED UMCSENT + equiv | UMich/equivalent consumer sentiment scored |
| **CPI** | FRED CPIAUCSL + equiv | Real YoY CPI vs 2% target |
| **PPI** | FRED PPIACO + equiv | Real PPI MoM change |
| **PCE** | FRED PCE + equiv | Core PCE vs 2% Fed target |
| **Int. Rates** | Central bank policy | Current rate + hiking/cutting direction |
| **NFP** | BLS + equivalent | Jobs added vs 150k benchmark |
| **Unemp. Rate** | FRED UNRATE + equiv | Unemployment trend direction |
| **Claims** | FRED ICSA | Weekly initial jobless claims |
| **ADP** | ADP National Employment | Private payrolls vs 150k benchmark |
| **LSE Edge** | London Strategic Edge (`lse-data`) | Live candles, economic calendar, insider trades, dividends, splits, and catalog coverage |

## Setup (Windows)

```
1. Double-click START.bat
   OR manually:

2. pip install -r requirements.txt

3. cd backend
   python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload

4. Open browser: http://localhost:8000
```

## Setup (Mac/Linux)

```bash
pip install -r requirements.txt
cd backend
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
# Open http://localhost:8000
```

## Architecture

```
frontend/index.html  ←→  http://localhost:8000/api/data
                                    ↓
                    ┌───────────────────────────────┐
                    │  backend/main.py (FastAPI)    │
                    ├───────────────────────────────┤
                    │  services/cot.py              │ → CFTC Socrata API
                    │  services/technical.py        │ → yfinance (live prices)
                    │  services/economic.py         │ → FRED + macro releases
                    │  services/retail.py           │ → Myfxbook / Dukascopy
                    │  services/scoring.py          │ → scoring engine
                    └───────────────────────────────┘
```

## Cache

All data is cached to `/tmp/alphafinder_cache/`:
- COT: 4 hours (data is weekly)
- Technical: 5 minutes (live prices)
- Economic: 6 hours (slow-moving data)
- Retail: 30 minutes

## Notes

- Myfxbook may block scraping with Cloudflare. If so, it falls back to Dukascopy.
- The technical service requires yfinance network access to Yahoo Finance.
- COT data is released every Friday at ~3:30 PM ET by the CFTC.
- FRED data is completely free and requires no API key (anonymous access).
- Set `LSE_API_KEY` in your environment to unlock London Strategic Edge market data. A separate 12-digit `LSE_BRUE_API_KEY` enables the optional paper-trading account/positions/orders monitor.
