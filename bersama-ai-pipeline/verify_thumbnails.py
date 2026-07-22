"""Verify the GENERIC thumbnail extractor against the owner's example URLs.
These are TEST FIXTURES only — the production extractor in news.py is fully generic
(no hardcoded posts/domains). Run: cd bersama-ai-pipeline && python verify_thumbnails.py"""
import sys
from pipeline import news  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

CASES = [
    ("Threads @prompt_case", "https://www.threads.com/@prompt_case/post/DbD0x9EFCXQ", "cdninstagram / jpg (microlink)"),
    ("Threads @fox.hsiao",   "https://www.threads.com/@fox.hsiao/post/Da6_9CGmW27", "cdninstagram / jpg (microlink)"),
    ("aiposthub article",    "https://www.aiposthub.com/google-gemini-flash-lite-cyber-gemini-4/", "ghost.io og:image webp"),
    ("openai ads",           "https://ads.openai.com/", "ctfassets chatgpt-ads-get-started.png (skip 1x1)"),
    ("openai HF incident",   "https://openai.com/index/hugging-face-model-evaluation-security-incident/", "ctfassets ...16x9.png (&amp; unescaped)"),
    ("fireworks blog",       "https://fireworks.ai/blog/kimik3-fable", "cdn.sanity png (skip SVG og, decode _next/image)"),
    ("GitHub repo",          "https://github.com/tirth8205/code-review-graph", "GitHub og:image (social card / avatar)"),
    ("X / Twitter post",     "https://x.com/AnthropicAI/status/2079256626771665098", "tweet media / card (og:image or microlink)"),
]

ok = True
for name, url, expect in CASES:
    img = news._fetch_image(url)
    meta_img = news.fetch_url_meta(url).get("image", "")
    usable = news._usable_image(img)
    notsvg = (not img.lower().split("?")[0].endswith(".svg")) if img else False
    passed = bool(img and usable and notsvg)
    ok = ok and passed
    print(f"[{'OK ' if passed else 'FAIL'}] {name}")
    print(f"        expect: {expect}")
    print(f"        _fetch_image -> {(img or '(none)')[:140]}")
    print(f"        share meta   -> {(meta_img or '(none)')[:140]}")
    print()

print("=" * 50)
print("ALL OK" if ok else "SOME FAILED — review above")
sys.exit(0 if ok else 1)
