# Stock-digest source challenge — getting `@EconomyApp` into `#stock-invest` reliably

*Status: dated snapshot, 2026-07-25. A problem statement for brainstorming — not a
living doc. Written to be read standalone (no repo context required).*

---

## TL;DR — the question for the brainstormer

We want a **daily, unattended, reliable, ~free** pipeline that turns one X/Twitter
account's posts (`@EconomyApp`) into Discord cards in `#stock-invest`. The hard
block: **X forbids free anonymous scraping from datacenter IPs**, and our runtimes
(GCP VM, GitHub Actions) are datacenter IPs. We've ruled out the obvious free
paths empirically. We have a working-but-maintenance-heavy option (RSSHub + a
login cookie) and a newly-discovered reliable option (the account's Substack RSS)
that changes the tradeoff. **We want fresh eyes on: is there a reliable free way
to get the literal tweets in 2026, or should we just take the Substack feed?**

---

## The system (context)

- **BersamaAi** — a Malaysia-based AI community on Discord (English-speaking).
- A Python content pipeline (`bersama-ai-pipeline/`) that already turns Reddit /
  HN / GitHub / RSS into topic-routed Discord "news cards" via a GLM judge, plus a
  YouTube summarizer. Posting = Discord webhooks (one per channel).
- **Two runtimes, both datacenter IPs:**
  - **GCP VM** (always-on; runs the bot, the on-demand portal, the YouTube summarizer).
  - **GitHub Actions** (runs the news digest every 3h; cron-friendly, free minutes).
- **Cost ethos: ~US$3/mo total** (GLM via Z.ai). Solutions should stay near-free.
- The account in question, **`@EconomyApp`** ("App Economy Insights"), posts
  English data-dense stock/earnings analysis (`$TSLA`, `$INTC` quarterly
  breakdowns — revenue/EPS/guidance). ~2–5 posts/day around earnings, fewer
  otherwise. Its tweets frequently link to its **Substack:
  `appeconomyinsights.com`** (publication name "How They Make Money").

## The hard constraint

**X/Twitter blocks all free ANONYMOUS scraping from datacenter ASNs.** Anonymous
access works only from residential IPs, or with authentication. This is structural
in 2026 (X killed the free API in 2023 and has progressively gated anonymous web
access). Any solution must either (a) run from a residential IP, (b) authenticate,
(c) pay, or (d) use an intermediary whose own infra takes the hit.

## What we already built

`pipeline/x_digest.py` — a daily digest that fetches a per-account RSS/Atom feed,
dedups by post id (`state/x_seen_<screen>.json`), and posts each new item as one
verbatim card (no LLM) to the account's channel. **Its parser is already
source-agnostic** (RSS 2.0 + Atom: title/description/link/guid/pubDate +
`media:content`/`enclosure`/`<img>`). It can read Nitter, RSSHub, RSS.app, or a
Substack feed unchanged. So **the choice of source is a config/wiring decision,
not a rewrite** — whichever source wins, the card logic is ready.

---

## Options, scored honestly

| # | Approach | Reliable / unattended? | Cost | Maintenance | What you actually get | Status / evidence |
|---|---|---|---|---|---|---|
| 1 | **Anonymous scraper from datacenter** (Nitter, snscrape, public RSS-Bridge, X guest-token GraphQL) | ❌ No | free | — | literal tweets | **Ruled out by test.** On the GCP VM: all 4 Nitter instances failed (conn-reset / 403 / refused); public `rss-bridge` Twitter bridge returns 1 degraded entry w/o a Bearer token; `rsshub.app` twitter route 404s; **yt-dlp has no X profile-timeline extractor at all** (read its source). |
| 2 | **RSSHub + `auth_token` cookie** (on the VM, web-API method) | ⚠️ Works, but **cookie expires** | free | re-grab `auth_token` + restart RSSHub every few weeks–months | literal tweets | Code is built. RSSHub `/twitter/user/:id` + `TWITTER_AUTH_TOKEN` confirmed in RSSHub source. **This is the reliability concern that triggered this doc.** |
| 3 | RSSHub username/password **auto-login** (auto-refresh cookie) | ❌ Disabled | free | — | literal tweets | RSSHub source: `TWITTER_USERNAME`/`PASSWORD`/`AUTHENTICATION_SECRET` env vars are **commented out** in current RSSHub — X's login defenses broke it. |
| 4 | **Managed RSS bridge** (RSS.app, FetchRSS, Politepol…) | ✅ Yes | ~US$8/mo (RSS.app Basic) | none | literal tweets | Their infra scrapes X; you read RSS from any IP. Most hands-off. ~$8/mo is heavy vs. the ~$3/mo LLM baseline but trivial in absolute terms. |
| 5 | **Official X API** (Basic tier) | ✅✅ Yes | US$200/mo | none | literal tweets | Overkill for one account. Free tier is post-only. |
| 6 | **Run the fetch from a residential IP** (always-on home machine / residential VPS) | ⚠️ Depends on that machine's uptime | free (existing PC) / low | keep the box online | literal tweets (anonymous Nitter works from residential — verified) | No cookies needed. But needs a reliable always-on residential host; the owner's main box is a workstation, not a server. |
| 7 | **The account's own Substack RSS** (`appeconomyinsights.com/feed`) | ✅ Yes | free | **none** | **the full articles, not the tweets** | **Verified working: HTTP 200, 13 items, images present, newest 2026-07-24.** Substack RSS is a first-class, IP-agnostic, no-auth feature. The tweets link here anyway. |
| 8 | **X syndication / embed JSON** (`cdn.syndication.twimg.com/timeline/profile…`) | ❓ Untested | free | maybe none | literal tweets | The embedded-timeline widget loads tweets via a syndication endpoint that *may* be cookieless and IP-agnostic (it's designed for public embeds). Not yet tested. **Open question.** |

## The reliability concern (why we're pausing)

Option 2 (RSSHub + cookie) is the path we'd wired up. It works today, but an
`auth_token` cookie expires (weeks–months, or whenever X forces re-login). When it
does, the daily run silently fails until a human re-grabs the cookie and restarts
RSSHub. That's a recurring manual tax on a job that's supposed to be unattended —
exactly the kind of thing that rots. So either we accept the tax (ideally with a
burner X account so the owner's real account isn't tied to it), automate the
refresh (Option 3 — currently not viable), or pick a source with no auth expiry.

## Leading recommendation (current thinking)

**Option 7 — Substack RSS — as the primary source**, run on GitHub Actions (no VM,
no cookies, no maintenance):

- Reliable, free, zero maintenance, works from GitHub Actions' datacenter IP.
- Delivers the *substance* the tweets point to (the full earnings write-ups), and
  Substack posts are richer than tweet bullets.
- **Tradeoff:** it's the articles, not literal tweets — so standalone tweet-only
  takes (e.g. a pure `$INTC` bullet post with no Substack link) won't appear.
  Cadence ~2–3 posts/week (Substack) vs. several/day (X around earnings).
- If literal-tweet coverage matters, supplement with Option 4 (paid bridge) or
  revisit Option 8.

This removes the cookie-expiry problem entirely for `@EconomyApp`. The broader
"literal X posts from a server" problem (for this account's tweet-only posts, or
future X accounts) remains open — see questions below.

---

## Sharp questions for the brainstormer

1. **Is there a reliable, free, cookieless, IP-agnostic way to read a public X
   account's recent posts in July 2026?** Specifically: does the syndication/embed
   timeline endpoint (`cdn.syndication.twimg.com/timeline/profile?…` or
   `syndication.twitter.com/srv/timeline-profile/screen-name/<user>`) return
   usable tweet JSON **without auth, from a datacenter IP**? Are there other
   endpoints (e.g. the i/api GraphQL with a minted guest token) that still work
   from Azure/GCP? If yes, this becomes a free, no-maintenance, literal-tweet
   source and beats everything.
2. **Cheapest *reliable* managed bridge** for a single X account, daily, in 2026?
   (Concrete service + price + whether it needs the paid X API under the hood.)
   Is there anything cheaper/more reliable than RSS.app Basic (~$8/mo)?
3. **Is automating X-cookie refresh actually viable** given X's 2026 login
   defenses (captchas, 2FA, phone/email gates, suspicious-login locks)? Any
   currently-maintained tool (Playwright-based, etc.) that logs in with
   username/password and emits a fresh `auth_token` on a schedule? Or does this
   always decay into manual intervention?
4. **Substack-vs-tweets:** given `@EconomyApp`'s real content is a Substack with a
   stable RSS feed, is "pull the Substack, not the tweets" the pragmatic winner?
   What concretely is lost (the standalone tweet-only bullet posts)? Is there a
   way to get both cheaply?
5. **Creative angles we might be missing:** does `@EconomyApp` cross-post to
   another platform with a real API (Bluesky/AT, LinkedIn, a podcast RSS)? Is
   there a Discord-native bot that reliably mirrors an X account to a channel
   today? Any "X → webhook" automation (IFTTT/Make/Zapier) whose X trigger still
   actually works on the free tier in 2026?

## Evidence appendix (what was actually tested, 2026-07-25)

- **Nitter on the GCP VM** — 4/4 instances failed:
  `nitter.net` (RemoteDisconnected), `xcancel.com` (parse error / "RSS reader not
  whitelisted"), `nitter.poast.org` (HTTP 403), `nitter.privacydev.net`
  (connection refused). Same 4 from a residential IP: `nitter.net` returns 20
  items. → datacenter-IP-gated.
- **Public RSS-Bridge** (`bridge.suumitsu.eu`, `rss-bridge.org/bridge01/`) Twitter
  bridge → HTTP 400/500; `rssbridge.vern.cc` → 200 but 1 entry, no titles
  (degraded without a Bearer token).
- **Public RSSHub** (`rsshub.app/twitter/user/...` and `/x/user/...`) → 404 / 403.
- **yt-dlp** — read `lib/routes/twitter/...`: no profile-timeline extractor
  exists (only individual tweets); profile URLs → `Unsupported URL`.
- **RSSHub source** (`lib/routes/twitter/api/web-api/utils.ts`, `lib/config.ts`):
  route `/twitter/user/:id`; web-API auth via `TWITTER_AUTH_TOKEN`
  (`config.twitter.authToken`) — RSSHub derives ct0/gt from it;
  `TWITTER_USERNAME`/`PASSWORD` auto-login env vars are commented out.
- **Substack RSS** (`https://www.appeconomyinsights.com/feed`) → HTTP 200,
  596 KB, 13 `<item>`s, each with `<enclosure>`/media, newest `Fri, 24 Jul 2026`.
  Channel title "How They Make Money". Reachable from a residential IP (and, being
  Substack, from any IP).
