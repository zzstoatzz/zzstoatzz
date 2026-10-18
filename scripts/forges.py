# /// script
# requires-python = ">=3.12"
# ///
"""Render forges-{dark,light}.svg: this year's activity on github and tangled, side by side."""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

GITHUB_LOGIN = "zzstoatzz"
TANGLED_HANDLE = "zzstoatzz.io"
AUTHOR_NAMES = {"zzstoatzz", "nate nowack"}
NON_CODE = {"Markdown", "Text", "JSON", "YAML", "TOML", "reStructuredText", "Nix", "Dockerfile", "Docker", "Shell", "Just"}
OUT = Path(__file__).resolve().parent.parent

LANG_COLORS = {
    "Python": "#3572A5", "Zig": "#ec915c", "TypeScript": "#3178c6", "JavaScript": "#f1e05a",
    "Svelte": "#ff3e00", "Rust": "#dea584", "Go": "#00ADD8", "Swift": "#F05138", "HTML": "#e34c26",
}
OTHER_COLOR = "#8b949e"

THEMES = {
    "dark": dict(bg="#0d1117", fg="#e6edf3", muted="#8b949e", rule="#30363d", gh="#39d353", tg="#f0a63a"),
    "light": dict(bg="#ffffff", fg="#1f2328", muted="#59636e", rule="#d1d9e0", gh="#1a7f37", tg="#bc6c00"),
}


def get(url: str, *, headers: dict[str, str] | None = None, data: bytes | None = None, timeout: float = 20) -> bytes:
    req = urllib.request.Request(url, data=data, headers={"user-agent": "zzstoatzz-profile", **(headers or {})})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == 4:
                raise
            time.sleep(2 ** attempt)
    raise AssertionError("unreachable")


def get_json(url: str, **kw) -> dict:
    return json.loads(get(url, **kw))


def github(start: dt.date, end: dt.date) -> tuple[dict[str, int], dict[str, tuple[str | None, int]]]:
    token = os.environ["GITHUB_TOKEN"]
    query = """query($login: String!, $from: DateTime!, $to: DateTime!) {
      user(login: $login) { contributionsCollection(from: $from, to: $to) {
        contributionCalendar { weeks { contributionDays { date contributionCount } } }
        commitContributionsByRepository(maxRepositories: 100) {
          repository { name owner { login } primaryLanguage { name } }
          contributions { totalCount }
        }
      } }
    }"""
    body = json.dumps({"query": query, "variables": {
        "login": GITHUB_LOGIN, "from": f"{start}T00:00:00Z", "to": f"{end}T23:59:59Z"}}).encode()
    resp = get_json("https://api.github.com/graphql", data=body,
                    headers={"authorization": f"bearer {token}", "content-type": "application/json"})
    if "errors" in resp:
        raise RuntimeError(resp["errors"])
    coll = resp["data"]["user"]["contributionsCollection"]
    days = {d["date"]: d["contributionCount"]
            for w in coll["contributionCalendar"]["weeks"] for d in w["contributionDays"]}
    repos = {}
    for r in coll["commitContributionsByRepository"]:
        repo = r["repository"]
        key = repo["name"].lower() if repo["owner"]["login"] == GITHUB_LOGIN else f'{repo["owner"]["login"]}/{repo["name"]}'.lower()
        lang = (repo["primaryLanguage"] or {}).get("name")
        repos[key] = (lang, r["contributions"]["totalCount"])
    return days, repos


def tangled_punchcard(year: int) -> dict[str, int]:
    page = get(f"https://tangled.org/{TANGLED_HANDLE}").decode()
    days = {d: int(n) for d, n in re.findall(r'title="(\d{4}-\d{2}-\d{2}): (\d+) commits?"', page)}
    if not any(d.startswith(f"{year}-") for d in days):
        raise RuntimeError("tangled punchcard not found on profile page")
    return days


def tangled_repos() -> list[tuple[tuple[str, ...], str, str]]:
    did = get_json(f"https://bsky.social/xrpc/com.atproto.identity.resolveHandle?handle={TANGLED_HANDLE}")["did"]
    doc = get_json(f"https://plc.directory/{did}")
    pds = next(s["serviceEndpoint"] for s in doc["service"] if s["id"] == "#atproto_pds")
    names: dict[tuple[str, str], list[str]] = {}
    cursor = None
    while True:
        q = {"repo": did, "collection": "sh.tangled.repo", "limit": "100"} | ({"cursor": cursor} if cursor else {})
        page = get_json(f"{pds}/xrpc/com.atproto.repo.listRecords?{urllib.parse.urlencode(q)}")
        for rec in page["records"]:
            name = rec["value"].get("name") or rec["uri"].rsplit("/", 1)[-1]
            ident = rec["value"].get("repoDid") or f"{did}/{name}"
            # a renamed repo keeps its repoDid but can leave a record under each name
            names.setdefault((f'https://{rec["value"]["knot"]}/xrpc', ident), []).append(name.lower())
        cursor = page.get("cursor")
        if not cursor or not page["records"]:
            return [(tuple(n), knot, ident) for (knot, ident), n in names.items()]


def primary_language(langs: list[dict]) -> str | None:
    if not langs:
        return None
    top = max(langs, key=lambda l: l["size"])["name"]
    if top in NON_CODE:
        return None
    return max((l for l in langs if l["name"] not in NON_CODE), key=lambda l: l["size"])["name"]


def tangled_repo_stats(names: tuple[str, ...], knot: str, ident: str, start: dt.date) -> tuple[tuple[str, ...], str | None, int] | None:
    def xrpc(method: str, **params) -> dict:
        return get_json(f"{knot}/sh.tangled.repo.{method}?{urllib.parse.urlencode({'repo': ident, **params})}", timeout=15)

    try:
        ref = xrpc("getDefaultBranch")["name"]
        try:
            lang = primary_language(xrpc("languages", ref=ref).get("languages") or [])
        except urllib.error.HTTPError:
            lang = None
        mine, offset = 0, 0
        while True:
            commits = xrpc("log", ref=ref, limit="100", cursor=str(offset)).get("commits") or []
            recent = [c for c in commits if dt.datetime.fromisoformat(c["committer"]["When"]).date() >= start]
            mine += sum(1 for c in recent if c["author"]["Name"].lower() in AUTHOR_NAMES
                        and dt.datetime.fromisoformat(c["author"]["When"]).date() >= start)
            if len(commits) < 100 or not recent:
                return names, lang, mine
            offset += 100
    except Exception as exc:  # one unreachable knot or empty repo should not sink the card
        print(f"skip tangled repo {names[0]}: {exc}", file=sys.stderr)
        return None


def languages(gh_repos: dict[str, tuple[str | None, int]], tg_repos: list[tuple[tuple[str, ...], str | None, int]]) -> list[tuple[str, float]]:
    merged: dict[str, tuple[str | None, int]] = dict(gh_repos)
    for names, lang, n in tg_repos:
        name = next((x for x in names if x in merged), names[0])
        prev = merged.get(name)
        # mirrored repos appear on both forges; count the larger side once
        if prev is None or n > prev[1]:
            merged[name] = (lang or (prev[0] if prev else None), n)
    weights: Counter[str] = Counter()
    for lang, n in merged.values():
        if lang and lang not in NON_CODE and n:
            weights[lang] += n
    total = sum(weights.values()) or 1
    top = [(l, n / total) for l, n in weights.most_common(3)]
    rest = 1 - sum(p for _, p in top)
    return top + ([("other", rest)] if rest > 0.005 else [])


def render(theme: dict[str, str], days: list[tuple[dt.date, int, int]], langs: list[tuple[str, float]]) -> str:
    W, H, L, chart_w, mid, amp = 880, 300, 8, 566, 152, 104
    weeks: list[list[tuple[dt.date, int, int]]] = []
    for d in days:
        if not weeks or d[0].isoweekday() == 7:
            weeks.append([])
        weeks[-1].append(d)
    wk = [(sum(x[1] for x in w), sum(x[2] for x in w)) for w in weeks]
    peak = max(max(g, t) for g, t in wk) or 1
    bw = chart_w / len(weeks)
    gh_total, tg_total = sum(d[1] for d in days), sum(d[2] for d in days)
    active = sum(1 for d in days if d[1] + d[2])
    streak = 0
    for d in reversed(days):
        if d[1] + d[2] == 0:
            if d is days[-1]:
                continue  # today may not have a push yet
            break
        streak += 1
    best = max(days, key=lambda d: d[1] + d[2])

    mono = "ui-monospace,SFMono-Regular,'SF Mono',Menlo,Consolas,monospace"
    sans = "-apple-system,BlinkMacSystemFont,'Segoe UI','Noto Sans',Helvetica,Arial,sans-serif"
    t = theme
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img">',
        f"<title>{gh_total:,} contributions on github and {tg_total:,} commits on tangled in {days[0][0].year}</title>",
        f'<style>.m{{font-family:{mono}}}.s{{font-family:{sans}}}</style>',
        f'<rect width="{W}" height="{H}" rx="6" fill="{t["bg"]}"/>',
        f'<text class="m" x="{L}" y="20" fill="{t["gh"]}" font-size="11" font-weight="600">▲ github.com/{GITHUB_LOGIN}</text>',
        f'<text class="m" x="{L}" y="{H - 10}" fill="{t["tg"]}" font-size="11" font-weight="600">▼ tangled.org/{TANGLED_HANDLE}</text>',
    ]
    for i, (g, tg) in enumerate(wk):
        x, hg, ht = L + i * bw, g / peak * amp, tg / peak * amp
        out.append(f'<rect x="{x + 1:.1f}" y="{mid - hg:.1f}" width="{bw - 2:.1f}" height="{hg:.1f}" rx="1.5" fill="{t["gh"]}" fill-opacity="{0.45 + 0.55 * g / peak:.2f}"/>')
        out.append(f'<rect x="{x + 1:.1f}" y="{mid + 1}" width="{bw - 2:.1f}" height="{ht:.1f}" rx="1.5" fill="{t["tg"]}" fill-opacity="{0.45 + 0.55 * tg / peak:.2f}"/>')
    out.append(f'<line x1="{L}" x2="{L + chart_w}" y1="{mid + 0.5}" y2="{mid + 0.5}" stroke="{t["rule"]}"/>')
    for i, w in enumerate(weeks):
        firsts = [d[0] for d in w if d[0].day == 1]
        if firsts or i == 0:
            m = (firsts or [w[0][0]])[0].strftime("%b").lower()
            out.append(f'<text class="m" x="{L + i * bw + 2:.1f}" y="36" fill="{t["muted"]}" font-size="9">{m}</text>')

    sx = 600

    def stat(y: int, big: str, small: str, color: str) -> None:
        out.append(f'<text class="s" x="{sx}" y="{y}" fill="{color}" font-size="26" font-weight="600">{big}</text>')
        out.append(f'<text class="m" x="{sx}" y="{y + 18}" fill="{t["muted"]}" font-size="11">{small}</text>')

    stat(44, f"{gh_total:,}", "contributions on github", t["gh"])
    stat(104, f"{tg_total:,}", "commits on tangled", t["tg"])
    stat(164, f"{active}/{len(days)}", f"days active · {streak}-day streak", t["fg"])
    out.append(f'<text class="m" x="{sx}" y="214" fill="{t["muted"]}" font-size="11">busiest: {best[0]:%b %-d} ({best[1] + best[2]})</text>')
    lx, lw = sx, 268
    for name, p in langs:
        color = LANG_COLORS.get(name, OTHER_COLOR)
        out.append(f'<rect x="{lx:.1f}" y="234" width="{lw * p:.1f}" height="8" fill="{color}"/>')
        lx += lw * p
    cx = sx
    for name, p in [x for x in langs if x[0] != "other"]:
        label = html.escape(f"{name.lower()} {round(p * 100)}%")
        out.append(f'<circle cx="{cx + 4}" cy="261" r="4" fill="{LANG_COLORS.get(name, OTHER_COLOR)}"/>')
        out.append(f'<text class="m" x="{cx + 12}" y="265" fill="{t["fg"]}" font-size="11">{label}</text>')
        cx += 26 + len(label) * 6.7
    out.append(f'<text class="m" x="{sx}" y="286" fill="{t["muted"]}" font-size="10">languages by commits, both forges</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"


def main() -> None:
    today = dt.datetime.now(dt.UTC).date()
    start = dt.date(today.year, 1, 1)
    gh_days, gh_repos = github(start, today)
    tg_days = tangled_punchcard(today.year)
    with ThreadPoolExecutor(2) as pool:
        tg_repos = [r for r in pool.map(lambda r: tangled_repo_stats(*r, start), tangled_repos()) if r]
    days = []
    d = start
    while d <= today:
        days.append((d, gh_days.get(d.isoformat(), 0), tg_days.get(d.isoformat(), 0)))
        d += dt.timedelta(days=1)
    langs = languages(gh_repos, tg_repos)
    print(f"github {sum(x[1] for x in days)} · tangled {sum(x[2] for x in days)} · "
          f"{len(tg_repos)} tangled repos · langs {[(l, round(p, 3)) for l, p in langs]}", file=sys.stderr)
    for theme, colors in THEMES.items():
        (OUT / f"forges-{theme}.svg").write_text(render(colors, days, langs))


if __name__ == "__main__":
    main()
