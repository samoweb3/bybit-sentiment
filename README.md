# Bybit Sentiment

A static GitHub Pages report of the top recent X posts about Bybit, Ben Zhou, and @benbybit.

The report uses public X API metrics. TypeSafe Jev classifies each post as Positive or Negative.

## Refresh the data

Run `scripts/build_data.py` with the X API and TypeSafe credentials in the process environment. The script writes `data/posts.json`.

## Local preview

Serve the repository root with any static HTTP server. Open `index.html` through the server so the page can load `data/posts.json`.
