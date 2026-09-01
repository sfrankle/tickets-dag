This looks good overall, but the retry loop in `src/api/retry.py` will spin forever on a 429. Can you cap it? Also the README still mentions `--legacy`.
