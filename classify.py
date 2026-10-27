#!/usr/bin/env python3
"""Classify forum comments as acceptable / toxic / spam with Cloudflare's Clef model.

Usage:
    python classify.py comments.json clef-flash results.html
    python classify.py comments-v2.json clef results-v2.html

Credentials are read from the environment (CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN)
or, if not set, from cloud-flare.local.sh in the current folder. Nothing is hardcoded.
"""
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

# ---- Edit these to improve Clef's accuracy (especially for the v2 run) ----------
INSTRUCTIONS = (
    "Classify `comment`, a post written by a student in the forum of an online "
    "college course. Choose the single best label."
)
CRITERIA = {
    "acceptable": (
        "A normal forum post: a question, answer, opinion, joke or feedback about the "
        "course. Includes blunt or harsh criticism of the course, assignments or "
        "instructors, as long as it does not attack a person."
    ),
    "toxic": (
        "Insults, harassment, hate, threats or personal attacks aimed at a person or "
        "group, including students and staff."
    ),
    "spam": (
        "Promotion or advertising, unrelated links, offers, repeated or copy-pasted "
        "text, or anything pushing a product or service that has nothing to do with "
        "the course."
    ),
}
LOW_CONFIDENCE = 0.30  # answers below this are flagged in the HTML
# ----------------------------------------------------------------------------------


def load_credentials():
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    token = os.environ.get("CLOUDFLARE_API_TOKEN")
    if account and token:
        return account, token
    try:
        text = open("cloud-flare.local.sh").read()
    except FileNotFoundError:
        sys.exit("No credentials found. Create cloud-flare.local.sh (see README).")
    creds = dict(re.findall(r'export (\w+)="([^"]*)"', text))
    try:
        return creds["CLOUDFLARE_ACCOUNT_ID"], creds["CLOUDFLARE_API_TOKEN"]
    except KeyError:
        sys.exit("cloud-flare.local.sh must define CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN.")


def load_comments(path):
    data = json.load(open(path, encoding="utf-8"))
    if isinstance(data, dict):  # e.g. {"comments": [...]}
        data = next((v for v in data.values() if isinstance(v, list)), [])
    comments = []
    for i, item in enumerate(data, start=1):
        if isinstance(item, str):
            comments.append({"id": i, "text": item})
            continue
        cid = next((item[k] for k in ("id", "comment_id", "ID") if k in item), i)
        text = next(
            (item[k] for k in ("text", "comment", "body", "content", "message") if k in item),
            None,
        )
        if text is None:  # fall back to the longest string field
            strings = [v for v in item.values() if isinstance(v, str)]
            text = max(strings, key=len) if strings else ""
        comments.append({"id": cid, "text": text})
    return comments


def classify(text, model, account, token):
    payload = {
        "state": {"comment": text},
        "questions": {
            "label": {"type": "choice", "instructions": INSTRUCTIONS, "criteria": CRITERIA}
        },
    }
    url = f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/@cf/cloudflare/{model}"
    for attempt in range(6):
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.load(resp)
            if not data.get("success"):
                raise RuntimeError(f"API error: {data.get('errors')}")
            return data["result"]["answers"]["label"]
        except urllib.error.HTTPError as e:
            if e.code == 429:  # too many requests: wait and retry
                time.sleep(2 ** attempt)
                continue
            if e.code in (401, 403):
                sys.exit("Bad account ID or API token (HTTP %d)." % e.code)
            raise
    raise RuntimeError("Gave up after repeated 429 responses")


COLOURS = {"acceptable": "#1a7f37", "toxic": "#cf222e", "spam": "#9a6700"}


def write_html(rows, model, source, path):
    counts = {k: sum(r["choice"] == k for r in rows) for k in CRITERIA}
    cards = []
    for r in rows:
        low = r["confidence"] < LOW_CONFIDENCE
        probs = " · ".join(f"{k} {v:.0%}" for k, v in r["probabilities"].items())
        cards.append(
            f'<div class="card" data-label="{r["choice"]}" data-low="{int(low)}" '
            f'style="border-left-color:{COLOURS.get(r["choice"], "#666")}">'
            f'<div class="meta"><b>#{html.escape(str(r["id"]))}</b> '
            f'<span class="tag" style="background:{COLOURS.get(r["choice"], "#666")}">'
            f'{html.escape(r["choice"])}</span> '
            f'<span class="conf">confidence {r["confidence"]:.2f}'
            f'{" ⚠ low" if low else ""}</span></div>'
            f'<div class="text">{html.escape(r["text"])}</div>'
            f'<div class="probs">{html.escape(probs)}</div></div>'
        )
    summary = ", ".join(f"{k}: {v}" for k, v in counts.items())
    page = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Clef results - {html.escape(source)}</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:860px;margin:2rem auto;padding:0 1rem;background:#f6f8fa}}
.card{{background:#fff;border:1px solid #d0d7de;border-left:6px solid;border-radius:6px;padding:.7rem 1rem;margin:.6rem 0}}
.tag{{color:#fff;border-radius:4px;padding:1px 8px;font-size:.85rem}}
.conf,.probs{{color:#57606a;font-size:.85rem}} .text{{margin:.4rem 0}}
button{{margin-right:.4rem;padding:.3rem .8rem;cursor:pointer}}
</style></head><body>
<h1>Clef classification: {html.escape(source)}</h1>
<p>Model: <b>{html.escape(model)}</b> &middot; {len(rows)} comments &middot; {summary}</p>
<div>
<button onclick="f('all')">All</button>
<button onclick="f('acceptable')">Acceptable</button>
<button onclick="f('toxic')">Toxic</button>
<button onclick="f('spam')">Spam</button>
<button onclick="f('low')">Low confidence</button>
</div>
{''.join(cards)}
<script>
function f(k){{document.querySelectorAll('.card').forEach(c=>{{
c.style.display=(k==='all'||c.dataset.label===k||(k==='low'&&c.dataset.low==='1'))?'':'none';}});}}
</script></body></html>"""
    open(path, "w", encoding="utf-8").write(page)


def main():
    if len(sys.argv) != 4:
        sys.exit("Usage: python classify.py <comments.json> <clef-flash|clef> <output.html>")
    source, model, out = sys.argv[1:]
    account, token = load_credentials()
    comments = load_comments(source)
    rows = []
    for n, c in enumerate(comments, start=1):
        ans = classify(c["text"], model, account, token)
        rows.append({**c, "choice": ans["choice"], "probabilities": ans["probabilities"],
                     "confidence": ans["confidence"]})
        print(f"[{n}/{len(comments)}] #{c['id']}: {ans['choice']} ({ans['confidence']:.2f})")
    json.dump(rows, open(out.rsplit(".", 1)[0] + ".json", "w", encoding="utf-8"), indent=2)
    write_html(rows, model, source, out)
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()