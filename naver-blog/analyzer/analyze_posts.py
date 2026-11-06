"""네이버 블로그 글 구조 분석기.

홈판에서 본 글의 링크 목록(urls.txt)을 받아 글마다 '구조 수치'만 뽑습니다.
본문 텍스트와 사진은 저장하지 않습니다(저작권). 제목은 패턴 분석용으로만 저장합니다.

사용법:
    pip install requests beautifulsoup4
    python analyze_posts.py urls.txt            # → result/posts.csv, result/summary.md
    python analyze_posts.py urls.txt --delay 3  # 요청 간격(초), 기본 3초

urls.txt: 한 줄에 링크 하나. 아래 형식을 지원합니다.
    https://blog.naver.com/아이디/글번호
    https://m.blog.naver.com/아이디/글번호
    https://blog.naver.com/PostView.naver?blogId=아이디&logNo=글번호
    https://naver.me/xxxx  (앱 공유 단축링크, 자동으로 따라감)
"""
from __future__ import annotations

import argparse
import csv
import random
import re
import statistics
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

UA = ("Mozilla/5.0 (Linux; Android 14; SM-S921N) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36")
MAX_POSTS = 500  # 구조 분석에는 이 정도면 충분합니다. 대량 수집은 차단 원인이 됩니다.

FIELDS = [
    "url", "title", "title_len", "title_has_number", "title_is_question",
    "title_type", "chars_no_space", "paragraphs", "avg_paragraph_chars",
    "images", "subheadings", "quotes", "dividers", "tables", "links",
    "shopping_links", "emojis", "disclosure", "ends_with_question",
    "first_image_position", "tags",
]

EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF☀-➿]")
SHOP_RE = re.compile(r"shopping\.naver|smartstore|brand\.naver|naver\.me|coupang|link\.coupang|shoppingconnect", re.I)
DISCLOSURE_RE = re.compile(r"수수료|쇼핑커넥트|제휴|소정의|원고료|협찬|광고")


def normalize(url: str, session: requests.Session) -> tuple[str, str] | None:
    url = url.strip()
    if not url or url.startswith("#"):
        return None
    if "naver.me" in url:
        try:
            url = session.get(url, allow_redirects=True, timeout=10).url
        except requests.RequestException:
            return None
    p = urlparse(url)
    q = parse_qs(p.query)
    if "blogId" in q and "logNo" in q:
        return q["blogId"][0], q["logNo"][0]
    m = re.match(r"^/([^/]+)/(\d+)", p.path)
    if m and "blog.naver.com" in p.netloc:
        return m.group(1), m.group(2)
    return None


def classify_title(t: str) -> str:
    if re.search(r"vs|VS|비교", t):
        return "비교형"
    if re.search(r"\d+\s*(가지|개|선|단계|일|주|개월|년)", t):
        return "숫자형"
    if re.search(r"후기|써보|사용기|내돈내산|솔직", t):
        return "경험형"
    if re.search(r"방법|법$|하는 법|꿀팁|팁|해결|없애", t):
        return "방법형"
    if t.rstrip().endswith("?"):
        return "질문형"
    return "기타"


def analyze_html(html: str, url: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    main = soup.select_one(".se-main-container")
    if main is None:  # 구버전 에디터
        main = soup.select_one("#postViewArea, .post-view")
    if main is None:
        return None

    title_el = soup.select_one(".se-title-text, .se-documentTitle .se-text-paragraph, .pcol1, h3.se_textarea")
    title = title_el.get_text(" ", strip=True) if title_el else ""

    texts = [p.get_text(" ", strip=True) for p in main.select(".se-component.se-text .se-text-paragraph")]
    paragraphs = [t for t in texts if t]
    body = "\n".join(paragraphs)
    chars = len(re.sub(r"\s", "", body))

    comps = main.select(".se-component")
    first_img = next((i for i, c in enumerate(comps) if "se-image" in c.get("class", [])), -1)

    hrefs = [a.get("href", "") for a in main.select("a[href]")]
    tags = [t.get_text(strip=True).lstrip("#") for t in soup.select(".post_tag a, .wrap_tag a, .item_tag")]

    return {
        "url": url,
        "title": title,
        "title_len": len(title),
        "title_has_number": int(bool(re.search(r"\d", title))),
        "title_is_question": int(title.rstrip().endswith("?")),
        "title_type": classify_title(title),
        "chars_no_space": chars,
        "paragraphs": len(paragraphs),
        "avg_paragraph_chars": round(chars / len(paragraphs), 1) if paragraphs else 0,
        "images": len(main.select(".se-component.se-image, .se-imageStrip img, .se-imageGroup img")),
        "subheadings": len(main.select(".se-component.se-sectionTitle")),
        "quotes": len(main.select(".se-component.se-quotation")),
        "dividers": len(main.select(".se-component.se-horizontalLine")),
        "tables": len(main.select(".se-component.se-table")),
        "links": len(main.select(".se-component.se-oglink")) + len(hrefs),
        "shopping_links": sum(1 for h in hrefs if SHOP_RE.search(h)),
        "emojis": len(EMOJI_RE.findall(body)),
        "disclosure": int(bool(DISCLOSURE_RE.search(body[:300]))),
        "ends_with_question": int(bool(paragraphs) and "?" in paragraphs[-1][-20:]),
        "first_image_position": first_img,
        "tags": len(tags),
    }


def fetch(blog_id: str, log_no: str, session: requests.Session) -> str | None:
    url = f"https://m.blog.naver.com/PostView.naver?blogId={blog_id}&logNo={log_no}"
    try:
        r = session.get(url, timeout=15)
    except requests.RequestException as e:
        print(f"  ! 요청 실패: {e}", file=sys.stderr)
        return None
    if r.status_code != 200:
        print(f"  ! HTTP {r.status_code}", file=sys.stderr)
        return None
    return r.text


def summarize(rows: list[dict]) -> str:
    def stat(key: str) -> str:
        v = [r[key] for r in rows]
        q = statistics.quantiles(v, n=4) if len(v) >= 4 else [min(v), statistics.median(v), max(v)]
        return f"| {key} | {statistics.median(v):g} | {q[0]:g} ~ {q[-1]:g} | {min(v):g} ~ {max(v):g} |"

    n = len(rows)
    out = [f"# 구조 분석 결과 (글 {n}개)\n",
           "| 항목 | 중앙값 | 중간 50% 범위 | 최소~최대 |", "|---|---|---|---|"]
    for k in ["title_len", "chars_no_space", "paragraphs", "avg_paragraph_chars", "images",
              "subheadings", "quotes", "dividers", "tables", "links", "shopping_links",
              "emojis", "first_image_position", "tags"]:
        out.append(stat(k))

    def pct(key: str) -> str:
        return f"{sum(r[key] for r in rows) / n * 100:.0f}%"

    out += ["", "## 비율",
            f"- 제목에 숫자 포함: {pct('title_has_number')}",
            f"- 질문형 제목: {pct('title_is_question')}",
            f"- 상단 수수료·협찬 고지: {pct('disclosure')}",
            f"- 질문으로 마무리: {pct('ends_with_question')}",
            "", "## 제목 유형"]
    types: dict[str, int] = {}
    for r in rows:
        types[r["title_type"]] = types.get(r["title_type"], 0) + 1
    for t, c in sorted(types.items(), key=lambda x: -x[1]):
        out.append(f"- {t}: {c}개 ({c / n * 100:.0f}%)")
    return "\n".join(out) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("urls")
    ap.add_argument("--delay", type=float, default=3.0)
    ap.add_argument("--out", default="result")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(exist_ok=True)
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"})

    lines = Path(args.urls).read_text(encoding="utf-8").splitlines()
    seen: set[tuple[str, str]] = set()
    rows: list[dict] = []
    for line in lines:
        if len(rows) >= MAX_POSTS:
            print(f"최대 {MAX_POSTS}개까지만 분석합니다.")
            break
        key = normalize(line, session)
        if not key or key in seen:
            continue
        seen.add(key)
        print(f"[{len(rows) + 1}] {key[0]}/{key[1]}")
        html = fetch(*key, session)
        if html:
            row = analyze_html(html, f"https://blog.naver.com/{key[0]}/{key[1]}")
            if row:
                rows.append(row)
            else:
                print("  ! 본문 구조를 찾지 못함", file=sys.stderr)
        time.sleep(args.delay + random.uniform(0, 1.5))

    if not rows:
        sys.exit("분석된 글이 없습니다. 링크 형식이나 네트워크를 확인하세요.")
    with open(out / "posts.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    (out / "summary.md").write_text(summarize(rows), encoding="utf-8")
    print(f"\n완료: {len(rows)}개 → {out / 'posts.csv'}, {out / 'summary.md'}")


if __name__ == "__main__":
    main()
