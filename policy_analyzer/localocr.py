"""
로컬 OCR 기반 증권 추출 — 데이터를 외부(해외 포함)로 보내지 않고 서버 안에서 처리.

  PDF -> 페이지 이미지(PyMuPDF) -> Tesseract 한글 OCR -> 표 휴리스틱 파싱
  이미지(JPG/PNG)는 바로 OCR.

한계: OCR 특성상 금액·이름이 틀릴 수 있습니다. 반드시 설계사가 검토·수정해야 합니다.
필요 패키지: pytesseract, pymupdf  /  시스템: tesseract-ocr, tesseract-ocr-kor
"""

import io
import re

import pytesseract
from PIL import Image

PDF_TYPE = "application/pdf"
IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

# 담보명 -> 분류 (extractor.CATEGORIES 와 동일 체계). 위에서부터 먼저 맞는 것.
CATEGORY_RULES = [
    ("특정암", ["특정암"]),
    ("유사암·소액암", ["소액암", "유사암", "갑상선암진단", "제자리암", "경계성"]),
    ("암치료비", ["로봇수술", "방사선", "항암", "표적", "카티", "car-t", "약물", "수술비특약"]),
    ("암통원", ["암직접치료통원", "통원특약", "통원"]),
    ("일반암", ["일반암", "암진단"]),
    ("뇌혈관질환", ["뇌혈관"]),
    ("뇌졸중·뇌출혈", ["뇌졸중", "뇌출혈"]),
    ("허혈성심장질환", ["허혈성"]),
    ("급성심근경색", ["급성심근경색", "심근경색"]),
    ("실손의료비", ["실손", "실손의료", "입원의료비", "통원의료비"]),
    ("질병수술비", ["질병수술"]),
    ("상해수술비", ["상해수술", "재해수술"]),
    ("입원일당", ["입원일당", "입원비", "입원특약"]),
    ("질병사망", ["질병사망"]),
    ("상해사망", ["재해사망", "상해사망"]),
    ("일반사망", ["사망", "주계약", "주 계 약"]),
    ("질병후유장해", ["질병후유장해", "질병장해"]),
    ("상해후유장해", ["상해후유장해", "재해후유장해", "후유장해"]),
    ("치매·간병", ["치매", "간병", "장기요양"]),
    ("운전자", ["운전자", "교통사고처리", "벌금", "변호사"]),
    ("배상책임", ["배상책임", "일상생활"]),
    ("치아", ["치아", "임플란트", "크라운", "충전"]),
]

AMOUNT_RE = re.compile(r"([\d,]+(?:\.\d+)?)\s*(억|만원|만|원)")
NAME_PREFIX_RE = re.compile(r"^\s*\d{5,}\s+")          # 앞의 상품/특약 코드 제거
NOISE = re.compile(r"[*#“”\"'·…]+")
EXCLUSION_HINT = re.compile(r"부담보|부담 보|할증|특정부위|특정 부위|부담보기간")


def classify(name: str) -> str:
    low = name.lower()
    for cat, keys in CATEGORY_RULES:
        if any(k in low for k in keys):
            return cat
    return "기타"


def parse_amount(text: str) -> int | None:
    """'10,000만원' -> 100000000, '6,000만원' -> 60000000. 못 찾으면 None."""
    m = AMOUNT_RE.search(text.replace(" ", ""))
    if not m:
        return None
    try:
        num = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    unit = m.group(2)
    if unit.startswith("억"):
        return int(num * 100_000_000)
    if unit.startswith("만"):
        return int(num * 10_000)
    return int(num)


def ocr_image(data: bytes) -> str:
    img = Image.open(io.BytesIO(data))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    return pytesseract.image_to_string(img, lang="kor+eng")


def ocr_pdf(data: bytes) -> str:
    import fitz  # PyMuPDF
    out = []
    with fitz.open(stream=data, filetype="pdf") as doc:
        for page in doc:
            pix = page.get_pixmap(dpi=200)
            out.append(pytesseract.image_to_string(Image.open(io.BytesIO(pix.tobytes("png"))), lang="kor+eng"))
    return "\n".join(out)


def ocr(files: list[tuple[str, str, bytes]]) -> str:
    texts = []
    for name, mime, data in files:
        texts.append(ocr_pdf(data) if mime == PDF_TYPE else ocr_image(data))
    return "\n".join(texts)


def guess_company(text: str) -> str | None:
    for comp in ["신한라이프", "삼성생명", "삼성화재", "한화생명", "교보생명", "메리츠화재", "현대해상",
                 "KB손해보험", "KB라이프", "DB손해보험", "DB생명", "NH농협생명", "NH농협손해보험",
                 "미래에셋생명", "흥국생명", "흥국화재", "동양생명", "라이나생명", "AIA생명", "하나생명",
                 "롯데손해보험", "MG손해보험", "캐롯손해보험", "농협", "오렌지라이프"]:
        if comp in text:
            return comp
    return None


SENTENCE_HINT = re.compile(r"(바랍니다|확인|설명|일치|하였|합니다|습니다|안내|주시기|입니다|경우)")


def guess_product(text: str) -> str | None:
    """'...보험 원(ONE)...' 같은 상품명 줄 추정. 문장·안내문은 제외."""
    # 1순위: '보험상품명 ...' 라벨 줄
    for line in text.split("\n"):
        if "보험상품명" in line:
            t = NOISE.sub("", line.split("보험상품명")[-1]).strip()
            t = re.sub(r"\s+", " ", t)
            if 6 <= len(t) <= 45:
                return t
    # 2순위: 괄호를 포함하거나 '보험'으로 끝나는 짧은 줄(문장 제외)
    for line in text.split("\n"):
        t = re.sub(r"\s+", " ", NOISE.sub("", line).strip())
        if (6 <= len(t) <= 45) and ("보험" in t) and not SENTENCE_HINT.search(t) \
                and not any(k in t for k in ("상품명", "코드", "주식회사", "협회")) \
                and (("(" in t) or t.endswith("보험")):
            return t
    return None


def parse_coverages(text: str) -> list[dict]:
    """코드로 시작하는 표 줄에서 담보명 + 가입금액 추출 (휴리스틱)."""
    covs = []
    seen = set()
    for raw in text.split("\n"):
        if not NAME_PREFIX_RE.match(raw):
            continue
        body = NAME_PREFIX_RE.sub("", raw)
        amt = parse_amount(body)
        # 담보명 = 금액 앞부분. 괄호 뒤 설명은 유지
        name = body
        m = AMOUNT_RE.search(body.replace(" ", ""))
        if m:
            # 원본에서 금액 토큰 위치 앞까지 이름으로
            idx = body.find(m.group(1).split(",")[0][:2]) if m.group(1)[:2] else -1
            if idx > 3:
                name = body[:idx]
        name = NOISE.sub("", name).strip(" -·")
        name = re.sub(r"\s+", " ", name)
        if len(name) < 2 or name in seen:
            continue
        seen.add(name)
        renewable = "갱신" in body
        covs.append({"name": name, "category": classify(name), "amount": amt,
                     "renewable": renewable if "갱신" in body or "비갱신" in body else None, "end_date": None})
    return covs


def extract(files: list[tuple[str, str, bytes]]) -> dict:
    text = ocr(files)
    covs = parse_coverages(text)
    warnings = ["로컬 OCR로 읽은 결과입니다. 금액·담보명·날짜가 틀릴 수 있으니 반드시 확인·수정하세요."]
    if EXCLUSION_HINT.search(text):
        warnings.append("부담보/할증 등 인수조건 문구가 보입니다. 계약을 열어 확인하세요.")
    if not covs:
        warnings.append("표에서 담보를 자동으로 찾지 못했습니다. 계약 정보를 직접 입력해 주세요.")
    return {
        "insured": {"name": None, "birth_date": None, "gender": None, "address": None, "phone": None},
        "policies": [{
            "company": guess_company(text), "product_name": guess_product(text), "policy_no": None,
            "contractor_name": None, "insured_name": None, "contract_date": None, "maturity_date": None,
            "payment_period": None, "payment_cycle": None, "monthly_premium": None, "is_renewable": None,
            "exclusions": [], "coverages": covs,
        }],
        "warnings": warnings,
        "ocr_text": text[:6000],   # 설계사가 참고할 수 있게 원문 일부 보관
    }
