#!/usr/bin/env python3
"""
WeChat Auto-publish Pipeline — two-stage daily workflow.

Stage 1  (8:00 CST)   python pipeline.py --stage fetch
  Fetch all sources → score → save Top 5 to Obsidian candidate files
  → send Bark/PushPlus with candidate directory

Stage 2  (12:00 CST)  python pipeline.py --stage publish
  Check Obsidian for selected candidate → translate → archive → publish
  If nothing selected, auto-publish the top-scoring candidate

Single-shot / manual:
  python pipeline.py                        # auto-select best & publish now
  MANUAL_URL=https://... python pipeline.py # publish a specific article
"""
import os, re, argparse
from db import init_db, mark_published
from fetcher import fetch_all, fetch_article_content
from scorer import select_best, score as score_article
from translator import translate, extract_chinese_title
from notifier import send_pushplus, send_bark

DRY_RUN    = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")
MANUAL_URL = os.getenv("MANUAL_URL", "").strip()

from config import (
    WECHAT_APP_ID, WECHAT_APP_SECRET,
)

WECHAT_ENABLED     = bool(WECHAT_APP_ID and WECHAT_APP_SECRET)

_TWEET_RE = re.compile(r'https?://(www\.)?(twitter|x)\.com/\S+/status/\d+', re.I)


def _resolve_manual_url(raw: str) -> str:
    """If raw is a tweet URL, extract the article link from the tweet body."""
    if not _TWEET_RE.match(raw):
        return raw
    print("[pipeline] Tweet URL — fetching to extract article link...")
    try:
        from fetcher import _URL_RE, _SKIP_DOMAINS
        import httpx
        resp = httpx.get(raw, timeout=15,
                         headers={"User-Agent": "Mozilla/5.0"},
                         follow_redirects=True)
        for url in _URL_RE.findall(resp.text):
            domain = url.split("/")[2].lower().lstrip("www.")
            if domain not in _SKIP_DOMAINS and "twitter.com" not in domain and "x.com" not in domain:
                print(f"[pipeline] Extracted: {url}")
                return url
    except Exception as e:
        print(f"[pipeline] Could not extract article from tweet: {e}")
    return raw


# ---------------------------------------------------------------------------
# Stage 1 — fetch & shortlist
# ---------------------------------------------------------------------------

def stage_fetch():
    """Fetch, score, save Top 5 to Obsidian, notify yorihan."""
    print("[pipeline] Stage 1: fetch & shortlist")
    init_db()

    articles = fetch_all()
    print(f"[pipeline] {len(articles)} new articles found")

    if not articles:
        msg = "今日无新文章，请手动补充。"
        if not DRY_RUN:
            send_bark("公众号日报", msg) or send_pushplus("公众号日报", msg)
        return

    # Score all, take Top 5
    scored = sorted(
        [(a, score_article(a)) for a in articles],
        key=lambda x: x[1], reverse=True
    )[:5]
    top5 = []
    for art, s in scored:
        art["score"] = s
        top5.append(art)

    print("[pipeline] Top 5 candidates:")
    for i, a in enumerate(top5, 1):
        print(f"  {i}. [{a['score']}] {a['title'][:60]} ({a['source']})")

    if DRY_RUN:
        print("[pipeline] DRY RUN — skipping Obsidian write and notification")
        return

    from obsidian_store import save_candidates, create_source_log
    print("[pipeline] Writing daily source log to Obsidian...")
    create_source_log(articles)
    candidate_dir = save_candidates(top5)

    lines = ["今日 Top 5 候选文章已存入 Obsidian，请选一篇（截止中午12点）：", ""]
    for i, a in enumerate(top5, 1):
        lines.append(f"{i}. [{a['score']}分] {a['title'][:50]}")
        lines.append(f"   来源：{a['source']} | {a['url']}")
        lines.append("")
    lines.append(f"候选目录：{candidate_dir}")
    lines.append("把候选 Markdown 的 frontmatter `status: candidate` 改为 `status: selected` 即可，不选则自动发第1名。")
    send_bark("📰 今日候选文章", "\n".join(lines)) or \
        send_pushplus("📰 今日候选文章", "\n".join(lines))

    print("[pipeline] Stage 1 done.")


# ---------------------------------------------------------------------------
# Stage 2 — translate & publish
# ---------------------------------------------------------------------------

def stage_publish():
    """Translate and publish the selected (or top-scoring) candidate."""
    print("[pipeline] Stage 2: translate & publish")

    chosen = None

    if MANUAL_URL:
        print(f"[pipeline] MANUAL MODE — {MANUAL_URL}")
        article_url = _resolve_manual_url(MANUAL_URL)
        chosen = {"url": article_url, "title": article_url, "source": "Manual", "score": 100}

    else:
        from obsidian_store import get_selected_candidate, get_top_candidate_today
        chosen = get_selected_candidate()
        if chosen:
            print(f"[pipeline] User selected: {chosen['title']}")
        else:
            print("[pipeline] No selection — auto-publishing top candidate")
            chosen = get_top_candidate_today()

    if not chosen:
        # Last resort: run live scoring
        print("[pipeline] Falling back to live scoring...")
        init_db()
        articles = fetch_all()
        chosen = select_best(articles)

    if not chosen:
        msg = "今日无可发布文章。"
        if not DRY_RUN:
            send_bark("公众号日报", msg) or send_pushplus("公众号日报", msg)
        return

    _translate_and_publish(chosen)


# ---------------------------------------------------------------------------
# Shared core: translate + write to Obsidian / WeChat
# ---------------------------------------------------------------------------

def _translate_and_publish(article: dict):
    print(f"[pipeline] Publishing: {article['title']}")
    print(f"[pipeline] Fetching full content: {article['url']}")

    content = fetch_article_content(article["url"])
    full_text = content["text"]
    cover_url = content["cover_url"]
    images    = content["images"]
    print(f"[pipeline] Cover: {cover_url or '(none)'}, images: {len(images)}")

    if DRY_RUN:
        print("[pipeline] DRY RUN — skipping translation and submission")
        return

    print("[pipeline] Translating...")
    translated    = translate(article, full_text)
    chinese_title = extract_chinese_title(translated)
    print(f"[pipeline] Chinese title: {chinese_title}")

    obsidian_path = ""

    # Format all WeChat themes (used for both Obsidian archive and WeChat draft)
    print("[pipeline] Formatting WeChat themes...")
    from formatter import format_all_themes, format_article
    wechat_themes = format_all_themes(translated, chinese_title)

    print("[pipeline] Writing to Obsidian...")
    from obsidian_store import write_to_obsidian
    obsidian_path = write_to_obsidian(
        title=chinese_title,
        translated_md=translated,
        source_url=article["url"],
        cover_url=cover_url,
        images=images,
        wechat_themes=wechat_themes,
    )

    if WECHAT_ENABLED:
        from wechat import create_draft
        print("[pipeline] Posting WeChat draft (green theme)...")
        html, _ = format_article(translated, chinese_title)
        draft_id = create_draft(
            chinese_title, html,
            source_url=article["url"],
            cover_media_id=os.getenv("WECHAT_COVER_MEDIA_ID", ""),
        )
        print(f"[pipeline] WeChat draft: {draft_id}")

    if not WECHAT_ENABLED and not obsidian_path:
        print("[pipeline] WARNING: Neither WeChat nor Obsidian archive configured.")
        print(f"[pipeline] Translated content:\n{translated[:500]}...")

    mark_published(article["url"], chinese_title)

    if obsidian_path:
        msg = f"「{chinese_title}」已存入 Obsidian：{obsidian_path}"
    else:
        msg = f"「{chinese_title}」翻译完成，请查看微信草稿箱。"
    send_bark("✅ 今日文章已发布", msg) or send_pushplus("✅ 今日文章已发布", msg)
    print("[pipeline] Done.")


# ---------------------------------------------------------------------------
# Single-shot (no --stage arg)
# ---------------------------------------------------------------------------

def run_single():
    """Backward-compatible: fetch → select → publish in one shot."""
    init_db()

    if MANUAL_URL:
        print(f"[pipeline] MANUAL MODE — {MANUAL_URL}")
        article_url = _resolve_manual_url(MANUAL_URL)
        best = {"url": article_url, "title": article_url, "source": "Manual", "score": 100}
    else:
        articles = fetch_all()
        print(f"[pipeline] Found {len(articles)} new articles")
        if not articles:
            send_bark("公众号日报", "今日无新文章。") or send_pushplus("公众号日报", "今日无新文章。")
            return
        best = select_best(articles)
        if not best:
            return

    _translate_and_publish(best)


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="WeChat Auto-publish Pipeline")
    parser.add_argument(
        "--stage", choices=["fetch", "publish"],
        help="fetch=collect Top5 to Obsidian (8am); publish=translate & post (12pm)"
    )
    args = parser.parse_args()

    try:
        if args.stage == "fetch":
            stage_fetch()
        elif args.stage == "publish":
            stage_publish()
        else:
            run_single()
    except Exception as e:
        if not DRY_RUN:
            msg = f"Pipeline 报错：{str(e)[:200]}"
            send_bark("公众号日报 ❌", msg) or send_pushplus("公众号日报 ❌", msg)
        raise
