# Stock-digest source challenge — getting `@EconomyApp` into `#stock-invest` reliably

*Status: dated snapshot, 2026-07-25 — **RESOLVED the same day**; see
"✅ RESOLVED" below. Originally a problem statement for brainstorming; kept intact
with the answer appended, not a living doc. Written to be read standalone (no repo
context required).*

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

## ✅ RESOLVED, same day (2026-07-25) — answer found: Option 9, a Bluesky mirror

The brainstorm question is answered, and **neither** of the two candidate answers
above won. Testing produced one kill and one discovery:

- **Killed Option 8 (X syndication).** The endpoint really is cookieless and
  really does return tweets — but it serves a **top-tweets-by-likes cache, not a
  recent timeline**. Newest post in it: **2025-11-14**, ~8 months stale. Dead for
  a daily digest.
- **Found Option 9 — the winner.** `@EconomyApp` is **continuously mirrored to
  Bluesky**, and Bluesky's public AT Protocol API is free, keyless, cookieless and
  IP-agnostic by design. This gives us the **literal tweets, with images**, at
  zero cost and zero maintenance — strictly better than the Substack feed
  (Option 7), which loses the tweet-only posts.

  | | |
  |---|---|
  | Handle | `economyapp.extwitter.link` |
  | **DID (use this, not the handle)** | `did:plc:kio5ffqovakoioxtxbuat6mr` |
  | Freshness | newest post **2026-07-24** (yesterday) |
  | Track record | **1032 posts since 2024-02-06** (~18 months unbroken) |
  | Continuity | 100 posts / 79 days = **1.27/day, largest gap 4 days**; monthly counts 39 / 29 / 32 |
  | Media | **96 of 100** posts carry an embed (57 link-preview, 39 image) → thumbnails available |
  | Hosting | `verpa.us-west.host.bsky.network` — Bluesky's own first-party PDS |
  | Auth | none. No cookie, no API key, no account. |

**Use the DID, not the handle.** `extwitter.link` doesn't even resolve as a
website (no A record — a Bluesky handle only needs a `_atproto` TXT record). If
that domain ever lapses, the handle breaks but **the DID is permanent**.

### The datacenter-IP caveat — since **confirmed clear**
The discovery tests all ran from the owner's **residential** IP, leaving one
unverified claim: that Bluesky's appview isn't datacenter-gated the way Nitter
was. **Verified on the GCP VM, 2026-07-25** — this returned the live feed:

```bash
curl -s "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed?actor=did:plc:kio5ffqovakoioxtxbuat6mr&limit=1" | head -c 300
```

→ `{"feed":[{"post":{"uri":"at://did:plc:kio5ffqovakoioxtxbuat6mr/app.bsky.feed.post/3mrfl2dwdq32e"…`
No auth, no cookie, datacenter IP. **The last objection to Option 9 is closed.**

### The real residual risk (and its cheap mitigation)
The dependency is now a **third-party mirror operator**
(`@twttr-mirrors.bsky.social` — 2.7k followers, runs a mirror farm, posts
nothing itself). If they stop, the feed goes **stale silently** — which is worse
than the cookie failure it replaces, because nothing errors. So the mitigation is
mandatory, not optional: **a staleness alarm** — if the newest post is older than
~5 days (measured max gap is 4), warn `🔒-staff-chat`, the same way a 0-card news
run already does. Substack RSS (Option 7) stays wired as the documented fallback.

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
| 8 | **X syndication / embed JSON** (`syndication.twitter.com/srv/timeline-profile/screen-name/…`) | ❌ **No — stale** | free | — | *popular* tweets, months old | **Ruled out by test.** The endpoint IS cookieless and IP-friendly: HTTP 200, 597 KB, 100 tweets with `full_text` / `favorite_count` / media, **no login wall**. But it's a **top-tweets cache sorted by likes**, not a recent timeline — entries span 2022–2025 and the **newest is 2025-11-14 (~8 months stale)**. Useless for a daily digest. Sibling `cdn.syndication.twimg.com/timeline/profile` → 200 but **0 bytes**. |
| **9** | ⭐ **Bluesky mirror of the X account, read via the public AT Protocol API** | ✅ **Yes** | **free** | **none** — no cookie, no key, no account | **the literal tweets + images** | ⭐ **WINNER — verified working.** `did:plc:kio5ffqovakoioxtxbuat6mr` (`economyapp.extwitter.link`). Newest post **2026-07-24**; **1032 posts since 2024-02-06**; 100 posts/79 days, **max gap 4 days**; 96/100 carry image or link-preview embeds. Carries the tweet-only bullet posts Substack drops. Risk = third-party mirror operator → needs a staleness alarm. |

## The reliability concern (why we're pausing)

Option 2 (RSSHub + cookie) is the path we'd wired up. It works today, but an
`auth_token` cookie expires (weeks–months, or whenever X forces re-login). When it
does, the daily run silently fails until a human re-grabs the cookie and restarts
RSSHub. That's a recurring manual tax on a job that's supposed to be unattended —
exactly the kind of thing that rots. So either we accept the tax (ideally with a
burner X account so the owner's real account isn't tied to it), automate the
refresh (Option 3 — currently not viable), or pick a source with no auth expiry.

## Recommendation (final, 2026-07-25)

**Option 9 — the Bluesky mirror, read via the public AT Protocol API — as the
primary source**, run on GitHub Actions (no VM, no RSSHub, no cookies):

- Free, keyless, unattended, and it delivers **the literal tweets with images** —
  so it beats Option 7 (Substack) on coverage *and* Option 2 (RSSHub) on
  maintenance. It dominates the whole table.
- **Abandon the RSSHub + `auth_token` build (Option 2).** Its only advantage was
  literal tweets, which Option 9 now provides without a cookie to expire. Don't
  install RSSHub on the VM.
- **Substack RSS (Option 7) stays wired as the documented fallback** if the mirror
  ever dies — the two sources are complementary (articles vs. tweets).

### Two ways to wire it — pick one

| | **A. Bluesky RSS** | **B. Bluesky JSON API** ⭐ |
|---|---|---|
| URL | `https://bsky.app/profile/did:plc:kio5ffqovakoioxtxbuat6mr/rss` | `https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed?actor=did:plc:…&limit=30` |
| Code change | **none** — `x_digest.py`'s parser already reads RSS 2.0 | a small adapter (~30 lines) mapping posts → `NewsItem` |
| Verified | 200, `application/xml`, 30 items, full tweet text, `pubDate`, `guid` = stable `at://` URI (ideal dedup key) | 200, 30–100 posts, full text + embeds |
| **Images** | ❌ **none** — 0 `<enclosure>`, 0 `media:content`, 0 `<img>` → text-only cards | ✅ **yes** — `images#view` / `external#view` thumbnails |

**Recommend B.** The existing cards post thumbnails, and A silently loses every
image on a chart-heavy finance account. A is still the right 10-minute smoke test
to prove the source end-to-end before writing the adapter.

Whichever is chosen, **add the staleness alarm** described above — it is the one
piece of new engineering this solution genuinely requires.

## ✅ Shipped, 2026-07-25 — option B, in `pipeline/x_digest.py`

| What | How |
|---|---|
| Source | Bluesky JSON API, addressed by **DID**; RSSHub code path deleted |
| Subscriptions | now an `XSource` NamedTuple carrying `did=` (preferred) or `rss=` (fallback) |
| Card body | keeps real newlines, so `• Revenue +25% Y/Y…` bullet lists render |
| Card link | the linked Substack article when the post has a link card, else the Bluesky permalink |
| Images | `images.fullsize` → `external.thumb`, unwrapping `recordWithMedia` |
| Reposts | skipped (a repost isn't the account's own post) |
| **Staleness alarm** | `STALE_AFTER_DAYS=6` (env `X_STALE_DAYS`) → `X_STALE` + `🔒-staff-chat` alert; separate `X_EMPTY` alarm for a reachable-but-empty feed |
| Source-switch safety | ids are namespaced `bsky:<rkey>`, so the old snowflake state reads as a first run and the changeover posts ≤ `FIRST_RUN_MAX` (3), not a week's backlog |
| Substack fallback | verified working — needed a non-snowflake id (`_fallback_id`), else every article was silently dropped |

Runtime unchanged (VM cron), but the VM is no longer *required*: nothing reads
`localhost` any more, so this can move to GitHub Actions whenever convenient.
**Do not install RSSHub.**

---

## Sharp questions for the brainstormer

> **Status after the 2026-07-25 testing round:** **Q1 answered — no** (the
> syndication endpoint is cookieless but serves an 8-month-stale popularity cache;
> see Option 8). **Q5 answered — yes, and it settles the whole problem**: the
> account is mirrored to Bluesky, whose public API is free and IP-agnostic (Option
> 9). **Q2 and Q3 are now moot** — no paid bridge and no cookie-refresh
> automation is needed. **Q4 is answered "no"**: don't take the Substack over the
> tweets; Option 9 gives both. Questions kept below for the record.

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

### Second testing round (2026-07-25, later) — the round that resolved it

- **X syndication timeline** `syndication.twitter.com/srv/timeline-profile/screen-name/EconomyApp`
  → **HTTP 200, 595 KB, no login wall.** Parsed `__NEXT_DATA__` →
  `props.pageProps.timeline.entries` = **100 tweets** with `full_text`,
  `favorite_count`, `id_str`, `mediaDetails`. **But: sorted by likes descending**
  (entry 0 = ♥23,258, the max), spanning **2022-09-13 → 2025-11-14**, by year
  {2022: 23, 2023: 27, 2024: 30, 2025: 20}. It is a popularity cache, not a
  timeline → **cannot drive a daily digest.**
- **`cdn.syndication.twimg.com/timeline/profile?screen_name=…`** → HTTP 200 but
  **0 bytes**.
- **`api.fxtwitter.com/EconomyApp`** → 200, but **profile metadata only**, no
  timeline (confirms the account: 239,866 followers, 6,840 tweets, verified,
  website `appeconomyinsights.com`). Useful for per-tweet lookups, not for a feed.
- **`openrss.org/twitter.com/EconomyApp`** → HTTP 301, no feed.
- **Bluesky `searchActors?q=App Economy Insights`** → 200; top hit
  `economyapp.extwitter.link` — *"⚠️ MIRROR OF https://twitter.com/EconomyApp ⚠️"*.
  The author has **no personal Bluesky account**; this is a third-party mirror.
- **`resolveHandle`** → `did:plc:kio5ffqovakoioxtxbuat6mr`.
  **`plc.directory`** → PDS `https://verpa.us-west.host.bsky.network` (Bluesky's
  own first-party hosting). `getProfile` → 1032 posts, created **2024-02-06**,
  635 followers.
- **`getAuthorFeed` (limit=100, no auth, no key)** → 200, 326 KB, 100 posts
  spanning **2026-05-05 → 2026-07-24** = 79 days, **1.27 posts/day**, largest gap
  **4 days**, monthly 39/29/32. Embeds: 57 `external#view`, 39 `images#view`,
  4 none. Content is verbatim tweet text, e.g. the standalone
  *"$INTC Intel Q2 FY26: • Revenue +25% Y/Y to $16.1B ($1.7B beat)…"* bullet post —
  **exactly the tweet-only content Option 7 would have lost.**
- **Bluesky RSS** `bsky.app/profile/economyapp.extwitter.link/rss` → 302 →
  `bsky.app/profile/did:plc:…/rss` → **200, `application/xml`, 16.5 KB, 30
  `<item>`s** with `<description>` (full tweet text incl. links), `<pubDate>`,
  `<guid>` = `at://` URI. **Zero `<enclosure>` / `media:content` / `<img>` — no
  images.**
- **`extwitter.link`** → `curl: (6) Could not resolve host`. Not a website; the
  handle is domain-verified via a `_atproto` TXT record only. → **address the feed
  by DID, never by handle.**
- ⚠️ **All of the above ran from the owner's residential IP.** The Bluesky
  results are expected to hold from GCP/GitHub Actions (keyless public appview,
  no anonymous gating), but that specific claim is **unverified** — run the
  one-line `curl` in the RESOLVED section on the VM to confirm.
