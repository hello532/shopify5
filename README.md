# Shopify Trend Radar

Practical Shopify product-monitoring and trend dashboard for local research workflows.

The app helps operators scan Shopify stores, inspect daily product updates, compare category and price-band trends, and turn signals into testable product actions.

## Key Features

- Fast local dashboard served by FastAPI
- Daily product update page at `/daily-products`
- Platform trend summary from local Shopify product snapshots
- Product quality and scrape precision audit
- V3 followable-products radar and decision helpers
- Local-first workflow with generated data excluded from git

## Run Locally

```bash
python -m uvicorn shopify_api_server:app --host 127.0.0.1 --port 8011
```

Then open:

- `http://127.0.0.1:8011/`
- `http://127.0.0.1:8011/daily-products`

## Tests

```bash
python -m unittest test_shopify_precision_suite.py
python -m py_compile shopify_api_server.py shopify_precision_suite.py auto_intelligence_loop.py shopify_ultimate.py social_amazon_auto_launch.py
```

## Data And Secrets

This repository intentionally excludes local runtime data and credentials:

- `output/`
- `logs/`
- `cache/`
- `.env*`
- `*.db`
- `*.sqlite`

Set any Shopify, Meta, Google Trends, or AI service credentials with environment variables in your own local environment.
