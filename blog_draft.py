"""네이버 블로그 초안 생성기 (템플릿 기반)

두 가지 글 유형을 지원한다.
  - info        : 정보성 글 (주제 소개 → 소제목별 본문 → 요약 → FAQ)
  - brandconnect: 네이버 브랜드커넥트 상품 리뷰 글 (대가성 문구 + 상품 정보 + 장단점 + 구매 링크)

출력물은 스마트에디터 ONE에 붙여넣기 좋은 단순 HTML, 일반 텍스트, 태그 목록이다.
네이버는 블로그 글쓰기 API를 제공하지 않으므로 실제 발행은 사람이 복사·붙여넣기로 한다.

CLI:
    python blog_draft.py samples/blog_info.json
    python blog_draft.py samples/blog_brandconnect.json --out blog_output
"""
import argparse
import html
import json
import re
from datetime import date
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field

# 공정위 「추천·보증 등에 관한 표시·광고 심사지침」: 경제적 대가는 글 첫머리에 명확히 표시
DEFAULT_DISCLOSURE = "이 포스팅은 네이버 브랜드커넥트를 통해 판매 수수료를 제공받을 수 있습니다."

MIN_BODY_CHARS = 1500   # 정보성 글 권장 최소 분량 (공백 제외)
MAX_TAGS = 30           # 네이버 블로그 태그 최대 개수


class Section(BaseModel):
    heading: str
    body: str                       # 빈 줄(\n\n)로 문단 구분
    bullets: List[str] = []
    image_hint: Optional[str] = None  # 넣을 이미지 설명 (자리 표시)


class FAQ(BaseModel):
    q: str
    a: str


class Product(BaseModel):
    name: str
    link: str                       # 브랜드커넥트에서 발급받은 링크 (naver.me/… 등)
    price: Optional[str] = None
    brand: Optional[str] = None
    specs: dict = {}
    pros: List[str] = []
    cons: List[str] = []
    recommend_for: List[str] = []
    image_hint: Optional[str] = None


class DraftRequest(BaseModel):
    type: str = Field("info", pattern="^(info|brandconnect)$")
    keyword: str                    # 메인 검색 키워드 (제목·첫 문단·태그에 반영)
    title: Optional[str] = None     # 비우면 템플릿 제목 자동 생성
    intro: Optional[str] = None
    sections: List[Section] = []
    summary: List[str] = []
    faqs: List[FAQ] = []
    product: Optional[Product] = None
    tags: List[str] = []
    closing: Optional[str] = None
    disclosure: Optional[str] = None  # brandconnect 대가성 문구 (비우면 기본 문구)


class Draft(BaseModel):
    title: str
    html: str
    text: str
    tags: List[str]
    char_count: int
    warnings: List[str]


# ── HTML 조각 ─────────────────────────────────────────────

def _e(s: str) -> str:
    return html.escape(s, quote=True)


def _paras(body: str) -> List[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", body or "") if p.strip()]


def _p(text: str) -> str:
    return "<p>" + _e(text).replace("\n", "<br>") + "</p>"


def _h(text: str) -> str:
    # 스마트에디터는 h 태그를 그대로 살리지 않으므로 굵은 큰 글씨 문단으로 표현
    return f'<p><b><span style="font-size:19px">{_e(text)}</span></b></p>'


def _ul(items: List[str]) -> str:
    return "<ul>" + "".join(f"<li>{_e(i)}</li>" for i in items) + "</ul>"


def _img(hint: str) -> str:
    return f'<p style="color:#999">[이미지: {_e(hint)}]</p>'


def _quote(text: str) -> str:
    return f"<blockquote><p>{_e(text)}</p></blockquote>"


def _link(label: str, url: str) -> str:
    return f'<p><b><a href="{_e(url)}" target="_blank">{_e(label)}</a></b></p>'


HR = "<hr>"


# ── 텍스트 조각 (html과 같은 순서로 쌓는다) ────────────────

class _Builder:
    def __init__(self):
        self.html: List[str] = []
        self.text: List[str] = []

    def p(self, s):
        self.html.append(_p(s)); self.text.append(s)

    def h(self, s):
        self.html.append(_h(s)); self.text.append(f"■ {s}")

    def ul(self, items):
        if items:
            self.html.append(_ul(items)); self.text.append("\n".join(f"• {i}" for i in items))

    def img(self, hint):
        if hint:
            self.html.append(_img(hint)); self.text.append(f"[이미지: {hint}]")

    def quote(self, s):
        self.html.append(_quote(s)); self.text.append(f"「{s}」")

    def link(self, label, url):
        self.html.append(_link(label, url)); self.text.append(f"{label}\n{url}")

    def hr(self):
        self.html.append(HR); self.text.append("────────")


# ── 템플릿 ────────────────────────────────────────────────

def _render_sections(b: _Builder, req: DraftRequest):
    for s in req.sections:
        b.h(s.heading)
        b.img(s.image_hint)
        for para in _paras(s.body):
            b.p(para)
        b.ul(s.bullets)


def _render_tail(b: _Builder, req: DraftRequest):
    if req.summary:
        b.hr()
        b.h("한눈에 정리")
        b.ul(req.summary)
    if req.faqs:
        b.hr()
        b.h("자주 묻는 질문")
        for f in req.faqs:
            b.p(f"Q. {f.q}")
            b.p(f"A. {f.a}")
    if req.closing:
        b.hr()
        for para in _paras(req.closing):
            b.p(para)


def _info(req: DraftRequest, b: _Builder) -> str:
    title = req.title or f"{req.keyword} 총정리 | 꼭 알아야 할 핵심만"
    intro = req.intro or f"오늘은 많은 분들이 궁금해하시는 '{req.keyword}'에 대해 핵심만 골라 정리해 보겠습니다."
    for para in _paras(intro):
        b.p(para)
    if req.sections:
        b.quote("목차  " + " / ".join(f"{i}. {s.heading}" for i, s in enumerate(req.sections, 1)))
    _render_sections(b, req)
    _render_tail(b, req)
    return title


def _brandconnect(req: DraftRequest, b: _Builder) -> str:
    p = req.product
    title = req.title or f"{p.name} 솔직 후기 | {req.keyword} 장단점 비교"
    b.quote(req.disclosure or DEFAULT_DISCLOSURE)
    intro = req.intro or f"'{req.keyword}' 알아보시는 분들을 위해 {p.name}을(를) 직접 살펴보고 장단점을 정리했습니다."
    for para in _paras(intro):
        b.p(para)
    b.img(p.image_hint)

    b.h("제품 정보")
    info = []
    if p.brand:
        info.append(f"브랜드: {p.brand}")
    info.append(f"제품명: {p.name}")
    if p.price:
        info.append(f"가격: {p.price} (작성일 기준, 변동될 수 있음)")
    info += [f"{k}: {v}" for k, v in p.specs.items() if str(v).strip()]
    b.ul(info)
    b.link(f"▶ {p.name} 최저가 확인하기", p.link)

    _render_sections(b, req)

    if p.pros:
        b.h("좋았던 점")
        b.ul(p.pros)
    if p.cons:
        b.h("아쉬운 점")
        b.ul(p.cons)
    if p.recommend_for:
        b.h("이런 분께 추천해요")
        b.ul(p.recommend_for)

    _render_tail(b, req)
    b.hr()
    b.link(f"▶ {p.name} 구매 링크", p.link)
    return title


def _tags(req: DraftRequest) -> List[str]:
    seen, out = set(), []
    base = [req.keyword] + req.tags
    if req.product:
        base += [req.product.name] + ([req.product.brand] if req.product.brand else [])
    for t in base:
        t = re.sub(r"[\s#]+", "", t)
        if t and t not in seen:
            seen.add(t); out.append(t)
    return out[:MAX_TAGS]


def build_draft(req: DraftRequest) -> Draft:
    if req.type == "brandconnect" and not req.product:
        raise ValueError("brandconnect 글에는 product 정보가 필요합니다.")

    b = _Builder()
    title = _brandconnect(req, b) if req.type == "brandconnect" else _info(req, b)
    text = "\n\n".join(b.text)
    countable = re.sub(r"\[이미지: [^\]]*\]|https?://\S+", "", text)
    char_count = len(re.sub(r"\s", "", countable))
    tags = _tags(req)

    warnings = []
    if char_count < MIN_BODY_CHARS:
        warnings.append(f"본문이 {char_count}자(공백 제외)입니다. {MIN_BODY_CHARS}자 이상을 권장합니다.")
    if req.keyword.replace(" ", "") not in title.replace(" ", ""):
        warnings.append("제목에 메인 키워드가 들어있지 않습니다.")
    if not any(s.image_hint for s in req.sections) and not (req.product and req.product.image_hint):
        warnings.append("이미지 자리가 없습니다. 직접 찍은 사진을 넣으면 노출에 유리합니다.")
    if len(req.keyword) and text.count(req.keyword) > 10:
        warnings.append("메인 키워드가 10회 넘게 반복됩니다. 과도한 반복은 저품질 판정 요인이 될 수 있습니다.")
    if req.type == "brandconnect":
        if not re.match(r"^https?://", req.product.link):
            warnings.append("상품 링크가 http(s) 주소가 아닙니다. 브랜드커넥트에서 발급받은 링크를 넣어주세요.")
        if req.disclosure is not None and not req.disclosure.strip():
            warnings.append("대가성 문구가 비어 있습니다. 공정위 지침상 글 첫머리에 반드시 표시해야 합니다.")

    return Draft(title=title, html="\n".join(b.html), text=text,
                 tags=tags, char_count=char_count, warnings=warnings)


def save_draft(draft: Draft, out_dir: Path) -> Path:
    slug = re.sub(r"[^\w가-힣]+", "-", draft.title).strip("-")[:40]
    d = out_dir / f"{date.today():%Y%m%d}-{slug}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "post.html").write_text(draft.html, encoding="utf-8")
    (d / "post.txt").write_text(f"{draft.title}\n\n{draft.text}\n\n" + " ".join(f"#{t}" for t in draft.tags),
                                encoding="utf-8")
    (d / "meta.json").write_text(json.dumps(
        {"title": draft.title, "tags": draft.tags, "char_count": draft.char_count, "warnings": draft.warnings},
        ensure_ascii=False, indent=2), encoding="utf-8")
    return d


def main():
    ap = argparse.ArgumentParser(description="네이버 블로그 초안 생성")
    ap.add_argument("input", help="DraftRequest 형식의 JSON 파일")
    ap.add_argument("--out", default="blog_output", help="출력 폴더 (기본: blog_output)")
    args = ap.parse_args()

    req = DraftRequest(**json.loads(Path(args.input).read_text(encoding="utf-8")))
    draft = build_draft(req)
    d = save_draft(draft, Path(args.out))
    print(f"제목: {draft.title}")
    print(f"분량: {draft.char_count}자 / 태그 {len(draft.tags)}개")
    for w in draft.warnings:
        print(f"⚠ {w}")
    print(f"저장: {d}")


if __name__ == "__main__":
    main()
