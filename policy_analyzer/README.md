# 보험증권 보장분석 (설계사용)

설계사가 고객 보험증권(PDF/사진)을 올리면 Claude 가 읽어서 정리하고, 고객별로 저장합니다.

- **고객 정보**: 이름, 생년월일, 나이, 성별, 주소, 연락처 (주민번호 뒷자리는 자동으로 가림)
- **가입 현황**: 보험사, 상품명, 증권번호, 계약일·만기, 납입기간, 월 보험료
- **보장 합산**: 암·뇌·심장·수술·입원·실손·사망·후유장해 등 항목별 합계와 기준 대비 부족 여부
- **체크 포인트**: 실손 중복, 보장 부족/미가입, 좁은 범위 담보(뇌출혈만·급성심근경색만), 갱신형, 짧은 만기, 부담보, 계약자≠피보험자
- **고객용 리포트**: 인쇄 / PDF 저장
- **고객 직접 등록**: 증권 없이도 고객 정보(이름·생년월일·연락처·주소·메모) 등록
- **자유 메모장**: 고객마다 날짜 구분 없이 자유롭게 적는 메모(대화 내용·특이사항). 고객용 리포트에는 나오지 않음
- **상담 기록**: 날짜·방식(전화/방문/카톡)·내용·다음 연락일·할 일. 첫 화면에 지난 일정과 7일 이내 '연락 예정' 표시, 상담 내용도 검색 가능 (고객용 리포트에는 나오지 않음)
- **계약 관리**: 계약별 구분(내 계약 / 다른 설계사 / 타사), 납입 상태(정상·미납·실효·해지), 이체일, 확인일, 메모. 실효·해지 계약은 보장 합산에서 제외. 증권을 다시 올려도 관리 정보 유지
- **할 일·알림**: 연락 예정 + 미납·실효 계약 + 90일 이내 만기·갱신 계약을 첫 화면에 표시. 납입 상태는 보험사에서 자동으로 가져오지 않으므로 고객 앱 캡처 등으로 확인한 내용을 직접 기록
- 설계사 계정별로 자기 고객만 보임. 같은 이름+생년월일 고객에게 자동으로 묶임. 같은 증권번호를 다시 올리면 교체.

## 증권 분석 방식 (해외 전송 여부)
`EXTRACT_BACKEND` 환경변수로 선택합니다.

| 값 | 설명 | 데이터 해외 전송 | 정확도 |
|---|---|---|---|
| **local** (기본) | 서버 안에서 Tesseract OCR 로 처리 | **없음** | 보통 (금액·담보 오인식 가능 → 검토 필요) |
| claude | Claude API 로 처리 | **있음 (미국)** | 높음 |
| mock | 샘플 데이터 (시험용) | 없음 | - |

소속사가 고객정보 해외 전송을 제한한다면 **local** 을 쓰세요. OCR 자동인식은 완벽하지 않으므로,
업로드 후 각 계약 카드의 "수정"에서 금액·담보·날짜를 반드시 확인하세요. 이 검토 과정을 전제로 설계된 도구입니다.

> local 백엔드는 시스템에 Tesseract 한글팩이 필요합니다:
> `sudo apt-get install -y tesseract-ocr tesseract-ocr-kor`  (macOS: `brew install tesseract tesseract-lang`)

## 설치·실행
```bash
pip install -r requirements.txt
cp .env.example .env          # ANTHROPIC_API_KEY, DATA_KEY 입력
python manage.py add-planner kim "김설계"    # 설계사 계정 생성 (비밀번호 입력, 첫 로그인 때 OTP 등록)
uvicorn app:app --host 0.0.0.0 --port 8000
```
- API 키 없이 화면만 보려면 `.env` 에 `ANALYZER_MOCK=1`, 로컬 http 테스트는 `SECURE_COOKIE=0`
- 실제 운영은 HTTPS 서버에 올리세요.
- `DATA_KEY` 를 잃어버리면 저장된 고객 데이터를 복구할 수 없습니다. 따로 안전하게 보관하세요.

## 파일
| 파일 | 역할 |
|---|---|
| `app.py` | 웹 화면 |
| `extractor.py` | 증권 → 데이터 추출 (Claude API, 주민번호 마스킹) |
| `analysis.py` | 보장 합산·체크 포인트 규칙. **`BENCHMARKS` 기준 금액은 회사 기준에 맞게 수정** |
| `store.py` | 암호화 저장 (SQLite) |
| `manage.py` | 설계사 계정 관리 |

## 보안
### 앱에 들어 있는 보호 장치
| 위협 | 대응 |
|---|---|
| 비밀번호 유출 | **2단계 인증(OTP 앱) 필수** — 첫 로그인 때 QR 등록, 같은 코드 재사용 불가 |
| 비밀번호 무차별 대입 | 5회 실패 시 15분 잠금 (IP·아이디 각각), 없는 아이디도 같은 응답 시간 |
| 자리 비운 사이 노출 | 30분 미사용 시 자동 로그아웃, 최대 8시간 |
| 다른 사이트에서 몰래 요청(CSRF) | SameSite 쿠키 + Origin 검사, 차단 시 기록 |
| 화면 끼워넣기·스크립트 삽입 | CSP, X-Frame-Options, 모든 출력 이스케이프 |
| 브라우저·프록시에 고객정보 캐시 | Cache-Control: no-store |
| 위조 파일 업로드 | 확장자가 아니라 파일 내용으로 PDF/이미지 판별, 크기 제한 |
| DB 파일 유출 | 고객·계약·상담 내용 암호화(DATA_KEY), DB 파일 권한 600, 삭제 시 덮어쓰기 |
| 다른 설계사 고객 열람 | 모든 조회에 설계사 범위 제한 |
| 내부자 오남용·사고 추적 | 로그인·조회·수정·삭제 **접속 기록** (내 계정 화면, `manage.py audit`) |
| API 문서 노출 | /docs, /openapi.json 비활성화 |
| 특정 장소에서만 사용 | `ALLOWED_IPS` 로 사무실 IP만 허용 가능 |

관리 명령: `python manage.py reset-2fa <아이디>` (휴대폰 분실), `python manage.py audit 200` (전체 기록)

### 서버에 올릴 때 반드시 할 것
앱만으로는 부족하고 서버 설정이 함께 되어야 합니다.
1. **HTTPS 필수** — nginx + Let's Encrypt 인증서. `SECURE_COOKIE=1`, `TRUST_PROXY=1`
2. **방화벽** — 80/443 외 모두 차단, SSH는 키 인증만 + 접속 IP 제한
3. **앱은 root 가 아닌 전용 계정으로 실행**, `.env`·DB 파일은 그 계정만 읽기 (`chmod 600`)
4. **DATA_KEY 는 DB 와 분리 보관** — DB 백업과 같은 곳에 두지 말 것
5. **백업은 암호화**해서 다른 장소에 보관, 복구 테스트
6. **OS·패키지 자동 보안 업데이트** (`unattended-upgrades`), `pip install -U -r requirements.txt` 정기 실행
7. (선택) Cloudflare 등 WAF/DDoS 방어 앞단에 두기

nginx 예시:
```nginx
server {
    listen 443 ssl http2;
    server_name example.com;
    ssl_certificate     /etc/letsencrypt/live/example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/example.com/privkey.pem;
    client_max_body_size 35m;
    limit_req zone=app burst=20 nodelay;     # http 블록에: limit_req_zone $binary_remote_addr zone=app:10m rate=5r/s;
    server_tokens off;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_read_timeout 300s;             # 증권 분석 대기
    }
}
server { listen 80; server_name example.com; return 301 https://$host$request_uri; }
```
앱은 `uvicorn app:app --host 127.0.0.1 --port 8000` 으로 **외부에 직접 노출하지 않고** nginx 뒤에서만 실행하세요.

### 한계
- 로그인 세션·잠금 정보는 서버 메모리에 있어 재시작하면 모두 로그아웃됩니다 (서버 1대 기준 설계).
- 설계사 PC·휴대폰이 악성코드에 감염되면 앱 보안으로는 막을 수 없습니다. 기기 백신·잠금 화면 필수.
- 개인정보보호법상 안전성 확보조치(접속기록 보관 기간, 내부관리계획 등)는 운영 주체가 별도로 갖춰야 합니다.

## 비용
증권 분석 1회마다 Claude API 요금이 듭니다 (증권 장수에 비례). 판매 가격 책정 전에 실제 증권 몇 건으로 사용량을 확인하세요.
console.anthropic.com 의 Usage 화면에서 볼 수 있습니다.

## 운영 전 확인 (법률 자문 아님)
- [ ] 고객에게 개인정보 수집·이용 동의 받기 (설계사 상담 동의서에 포함)
- [ ] 개인정보 처리방침에 국외이전 항목 공개 (아래 예시)
- [ ] 부담보 문구가 있는 증권은 건강정보(민감정보)가 드러날 수 있으므로 별도 동의 여부 검토
- [ ] 소속 GA/보험사 내부통제 규정 확인
- [ ] 개인정보 전문가에게 처리방침·동의서 검토

### 처리방침 국외이전 항목 예시
> **개인정보의 국외 이전**
> 회사는 보험증권 분석 서비스 제공(계약 이행)을 위해 아래와 같이 개인정보 처리를 국외에 위탁합니다.
> - 이전받는 자: Anthropic, PBC (privacy@anthropic.com)
> - 이전 국가: 미국
> - 이전 일시 및 방법: 증권 분석 요청 시 암호화된 네트워크로 전송
> - 이전 항목: 보험증권에 기재된 정보(성명, 생년월일, 성별, 주소, 연락처, 보험계약 내용)
> - 이용 목적: 보험증권 내용 인식 및 데이터 추출
> - 보유 기간: Anthropic 의 API 데이터 보존 정책에 따름 (계약 조건 확인 후 기재)
> - 거부 방법 및 효과: 담당 설계사에게 거부 의사를 밝힐 수 있으며, 거부 시 증권 자동 분석 서비스를 이용할 수 없습니다.
