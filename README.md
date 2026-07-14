# RedditMonitor

Loads proxies from MongoDB at startup (same `Proxies` DB as `UEControl.py`), then parses Reddit share links and resolves HelloFresh promo codes.

## Run (menu)

```bash
pip install -r requirements.txt
python main.py
```

```text
Loading proxies from MongoDB...
Options:
  1) Parse all links from Reddit
  q) Quit
```

Option **1** fetches the thread, finds every `hellofresh.com/gw/share` link, resolves promo codes, prints them, and writes `matches.json`.

## Continuous monitor (optional)

```bash
python monitor.py --interval 60
```

Also loads MongoDB proxies first. Seeds existing links without resolving; only **NEW** links are resolved.

## MongoDB

Uses the same URI pattern as UEControl (`Proxies` database). Override with:

```bash
export REDDIT_MONITOR_MONGO_URI='mongodb+srv://...'
```

Proxy docs look like: `{ "proxy": "host:port:user:pass", "blacklist": false }`.
