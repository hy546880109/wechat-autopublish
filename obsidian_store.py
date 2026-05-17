"""File-based Obsidian archive for candidates and translated articles."""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from config import OBSIDIAN_ARCHIVE_DIR


def _archive_root() -> Path:
    return Path(OBSIDIAN_ARCHIVE_DIR).expanduser()


def _today() -> str:
    return date.today().isoformat()


def _slug(text: str, fallback: str = "untitled") -> str:
    slug = re.sub(r"[\\/:*?\"<>|#\[\]\n\r\t]+", "-", text.strip())
    slug = re.sub(r"\s+", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-. ")
    return (slug or fallback)[:80]


def _yaml_scalar(value) -> str:
    text = "" if value is None else str(value)
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _frontmatter(fields: dict) -> str:
    lines = ["---"]
    for key, value in fields.items():
        if isinstance(value, (int, float)):
            lines.append(f"{key}: {value}")
        else:
            lines.append(f"{key}: {_yaml_scalar(value)}")
    lines.append("---")
    return "\n".join(lines) + "\n\n"


def _parse_frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end == -1:
        return {}

    fields = {}
    for line in text[4:end].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        value = value.strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        elif re.fullmatch(r"-?\d+(\.\d+)?", value):
            value = float(value) if "." in value else int(value)
        fields[key.strip()] = value
    return fields


def _candidate_dir(today: str | None = None) -> Path:
    return _archive_root() / "candidates" / (today or _today())


def _article_dir(today: str | None = None) -> Path:
    day = today or _today()
    return _archive_root() / "articles" / day[:4]


def _article_from_candidate(path: Path) -> dict:
    fields = _parse_frontmatter(path)
    return {
        "title": fields.get("title", path.stem),
        "url": fields.get("url", ""),
        "source": fields.get("source", ""),
        "score": fields.get("score", 0),
    }


def save_candidates(articles: list[dict]) -> str:
    """Write today's Top 5 candidates as editable Markdown files."""
    today = _today()
    out_dir = _candidate_dir(today)
    out_dir.mkdir(parents=True, exist_ok=True)
    for old_file in out_dir.glob("[0-9][0-9]-*.md"):
        old_file.unlink()

    for idx, art in enumerate(articles, 1):
        title = art.get("title", "Untitled")
        path = out_dir / f"{idx:02d}-{_slug(title)}.md"
        fields = {
            "status": "candidate",
            "date": today,
            "rank": idx,
            "score": art.get("score", 0),
            "title": title,
            "source": art.get("source", ""),
            "url": art.get("url", ""),
        }
        body = [
            _frontmatter(fields),
            f"# {title}",
            "",
            f"- Score: {art.get('score', 0)}",
            f"- Source: {art.get('source', '')}",
            f"- URL: {art.get('url', '')}",
            "",
            "将 `status: candidate` 改为 `status: selected`，中午发布时会优先选中这篇。",
            "",
        ]
        path.write_text("\n".join(body), encoding="utf-8")

    print(f"[obsidian] Saved {len(articles)} candidates -> {out_dir}")
    return str(out_dir)


def create_source_log(articles: list[dict]) -> None:
    """Write today's fetched article list grouped by source."""
    today = _today()
    out_dir = _candidate_dir(today)
    out_dir.mkdir(parents=True, exist_ok=True)

    by_source: dict[str, list[dict]] = {}
    for art in articles:
        by_source.setdefault(art.get("source", "未知来源"), []).append(art)

    lines = [
        _frontmatter({
            "status": "source-log",
            "date": today,
            "total": len(articles),
            "sources": len(by_source),
        }),
        f"# {today} 已查找文章清单",
        "",
        f"共抓取 {len(articles)} 篇文章，来自 {len(by_source)} 个来源。",
        "",
    ]
    for src, items in sorted(by_source.items()):
        lines.extend([f"## {src} ({len(items)}篇)", ""])
        for art in items:
            title = art.get("title", "(无标题)")
            url = art.get("url", "")
            lines.append(f"- [{title}]({url})" if url else f"- {title}")
        lines.append("")

    (out_dir / "source-log.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"[obsidian] Source log created -> {out_dir / 'source-log.md'}")


def _candidate_files() -> list[Path]:
    return sorted(p for p in _candidate_dir().glob("*.md") if p.name != "source-log.md")


def get_selected_candidate() -> dict | None:
    """Return today's candidate marked status: selected, or None."""
    for path in _candidate_files():
        fields = _parse_frontmatter(path)
        if str(fields.get("status", "")).lower() == "selected":
            return _article_from_candidate(path)
    return None


def get_top_candidate_today() -> dict | None:
    """Return today's highest-scoring candidate when no file is selected."""
    candidates = []
    for path in _candidate_files():
        fields = _parse_frontmatter(path)
        status = str(fields.get("status", "")).lower()
        if status in ("candidate", "selected"):
            candidates.append((fields.get("score", 0), fields.get("rank", 999), path))
    if not candidates:
        return None
    _score, _rank, path = sorted(candidates, key=lambda item: (-item[0], item[1]))[0]
    return _article_from_candidate(path)


def _replace_image_placeholders(md: str, images: list[str]) -> str:
    def repl(match: re.Match) -> str:
        idx = int(match.group(1))
        if idx >= len(images):
            return ""
        return f"![]({images[idx]})"

    return re.sub(r"^<<<IMG:(\d+)>>>$", repl, md, flags=re.MULTILINE)


def _html_filename(theme_key: str, title: str) -> str:
    return f"wechat-{_slug(theme_key)}-{_slug(title)}.html"


def write_to_obsidian(
    title: str,
    translated_md: str,
    source_url: str,
    cover_url: str = "",
    images: list[str] | None = None,
    wechat_themes: list[tuple[str, str, str]] | None = None,
) -> str:
    """Write translated article Markdown and WeChat HTML theme files."""
    today = _today()
    images = images or []
    out_dir = _article_dir(today)
    out_dir.mkdir(parents=True, exist_ok=True)

    article_path = out_dir / f"{today}-{_slug(title)}.md"
    html_links: list[tuple[str, str]] = []

    if wechat_themes:
        html_dir = out_dir / f"{today}-{_slug(title)}-wechat-html"
        html_dir.mkdir(parents=True, exist_ok=True)
        for theme_key, label, html in wechat_themes:
            html_path = html_dir / _html_filename(theme_key, title)
            html_path.write_text(html, encoding="utf-8")
            html_links.append((label, html_path.relative_to(out_dir).as_posix()))

    body_md = _replace_image_placeholders(translated_md, images)
    host = urlparse(source_url).netloc
    fields = {
        "status": "archived",
        "date": today,
        "title": title,
        "source_url": source_url,
        "source_host": host,
        "cover_url": cover_url,
    }

    lines = [_frontmatter(fields), f"# {title}", ""]
    if cover_url:
        lines.extend([f"![]({cover_url})", ""])
    lines.extend([body_md, "", "---", "", f"原文链接：{source_url}", ""])
    if html_links:
        lines.extend(["## 微信排版 HTML", ""])
        for label, rel_path in html_links:
            lines.append(f"- [{label}]({rel_path})")
        lines.append("")

    article_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[obsidian] Article archived -> {article_path}")
    return str(article_path)
