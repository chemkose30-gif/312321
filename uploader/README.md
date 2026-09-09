# 올원업로드 (원클릭 멀티 업로더)

영상 하나를 올리면 **YouTube · TikTok · Instagram 릴스 · Facebook 페이지**에 동시에 게시하는 홈페이지형 웹앱입니다.
제목/설명/해시태그를 한 번만 쓰면 각 플랫폼 형식에 맞춰 변환되고, 채널별 진행률과 결과 링크를 한 화면에서 확인할 수 있습니다.

| 페이지 | 내용 |
| --- | --- |
| `#home` | 소개 히어로, 연결 현황 통계, 사용법 3단계, 최근 업로드 |
| `#upload` | 영상 업로드 → 내용 작성 → 올릴 채널 선택 → 게시/진행률 |
| `#accounts` | 플랫폼별 계정 추가·별칭 지정·연결 끊기, **채널 세트** 관리 |
| `#history` | 최근 30건의 업로드와 채널별 결과 |

비밀번호를 걸면 모든 화면 앞에 로그인 페이지가 붙습니다(우측 상단에 로그아웃 버튼).
비밀번호는 `계정 관리 → 비밀번호 잠금`에서 브라우저로 바로 정하거나 바꿀 수 있습니다.

- 백엔드: FastAPI + httpx (플랫폼 공식 API 직접 호출)
- 프론트: 의존성 없는 단일 HTML (`static/index.html`)
- 저장소: SQLite (액세스 토큰은 `APP_SECRET` 기반 Fernet으로 암호화 저장)

## 여러 아이디 운영

계정을 목록에 올리는 방법은 두 가지입니다.

| 방법 | 하는 일 | 게시 가능? |
| --- | --- | --- |
| **+ 내 아이디 추가** | 유튜브·틱톡·인스타 아이디(핸들)를 직접 입력해 목록에 등록. 로그인 불필요 | 로그인 연결 후 가능 (`로그인 연결 필요` 배지 표시) |
| **로그인으로 연결** | 플랫폼 OAuth 로그인. 아이디·프로필이 자동으로 채워짐 | 바로 가능 |

먼저 아이디만 쭉 적어 목록과 세트를 만들어 두고, 나중에 플랫폼별로 로그인 연결을 마치는 순서로 쓸 수 있습니다.
아이디만 등록된 계정을 골라 게시하면 전송 대신 "로그인 연결을 마치세요" 안내로 실패 처리됩니다(다른 채널 게시는 그대로 진행).

- 같은 플랫폼에 **아이디를 몇 개든** 등록할 수 있습니다. 이미 있는 계정은 덮어쓰지 않고 목록에 쌓입니다.
- Meta(Instagram/Facebook)는 로그인 한 번으로 보유한 **모든 페이지와 연결된 IG 계정이 한꺼번에** 등록됩니다.
- 계정마다 **별칭**(선택)을 달 수 있어(예: `메인 채널`, `B사 클라이언트`) 목록이 길어져도 구분됩니다. 별칭을 비워두면 아이디가 그대로 표시됩니다.
- 업로드 화면에서는 계정별 체크박스로 고르고, 플랫폼 단위 **전체 선택/해제**도 가능합니다. 선택한 모든 채널로 동시에 전송하며, 플랫폼 옵션(공개범위 등)은 그 플랫폼에서 선택한 계정 전부에 동일하게 적용됩니다.
- 데모 모드에서는 `데모 계정 만들기`를 누를 때마다 가짜 계정이 하나씩 늘어나므로 다중 계정 화면을 미리 확인할 수 있습니다.

### 채널 세트

자주 함께 올리는 계정들을 **세트**로 묶어두면 업로드할 때 클릭 한 번으로 전부 선택됩니다.

- `계정 관리 → 채널 세트`에서 이름을 붙이고 플랫폼을 가로질러 계정을 골라 만듭니다. (예: `브랜드A 전체` = 유튜브 메인 + 틱톡 부계정 + 인스타)
- 업로드 화면 3단계 위의 세트 칩을 누르면 해당 채널이 한 번에 켜지고, 다시 누르면 해제됩니다. 여러 세트를 겹쳐서 적용할 수도 있습니다.
- 지금 체크해 둔 조합은 `+ 현재 선택 N개 세트로 저장`으로 바로 세트가 됩니다.
- 세트에 담긴 계정의 연결을 끊으면 그 계정만 세트에서 자동으로 빠집니다(세트 자체는 유지).

## 1. 빠른 시작 (데모 모드)

API 키가 하나도 없어도 화면 전체를 사용해 볼 수 있습니다. 이때 업로드는 실제로 전송되지 않고 시뮬레이션됩니다.
필요 환경은 **Python 3.10 이상**뿐입니다.

**Windows** — 파일 탐색기에서 `uploader` 폴더의 **`run.bat`** 을 더블클릭하면 끝입니다.
(브라우저가 자동으로 열립니다. 끄려면 검은 창을 닫으세요.)

**macOS / Linux** — 터미널에서:

```bash
cd uploader
./run.sh            # 가상환경 생성 + 의존성 설치 + .env 준비까지 자동, 기본 8100 포트
# ./run.sh 8200     # 다른 포트로 띄우기
```

둘 다 첫 실행 때만 1~2분 준비 시간이 걸리고, 이후에는 바로 뜹니다.
브라우저에서 **http://localhost:8100** 을 열면 됩니다.

직접 실행하려면:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # 그대로 두면 데모 모드
.venv/bin/python -m uvicorn app.main:app --port 8100
```

`계정 관리`에서 **데모 계정 만들기**를 누르면 가짜 계정이 하나씩 생성되고(또는 **+ 내 아이디 추가**로 직접 입력), 업로드 탭에서 다중 채널 게시 흐름을 그대로 확인할 수 있습니다.

## 2. 실제 연동 설정

`.env`에 키를 채우고 서버를 재시작하면, 키가 있는 플랫폼만 자동으로 실제 API 모드로 바뀝니다(플랫폼별 독립).

### 공통

| 항목 | 설명 |
| --- | --- |
| `PUBLIC_BASE_URL` | 외부에서 접근 가능한 이 서버의 주소. OAuth 콜백과 **Instagram 영상 다운로드**에 쓰입니다. 로컬 개발 시 `ngrok http 8100` 등으로 만든 https 주소를 넣으세요. |
| `APP_SECRET` | 토큰 암호화 키. 바꾸면 저장된 토큰을 복호화할 수 없어 계정 재연결이 필요합니다. |

### YouTube

1. Google Cloud Console → **YouTube Data API v3** 사용 설정
2. OAuth 클라이언트(웹 애플리케이션) 생성 → 승인된 리디렉션 URI에 `{PUBLIC_BASE_URL}/api/oauth/youtube/callback` 등록
3. `.env`의 `YOUTUBE_CLIENT_ID` / `YOUTUBE_CLIENT_SECRET` 입력

> 앱이 심사 전(테스트 모드)이면 업로드된 영상은 강제로 비공개 상태가 됩니다. 공개 게시가 필요하면 OAuth 앱 심사를 받으세요.
> 기본 업로드 할당량은 하루 약 6회(영상당 1600 units / 10000 units)입니다.

### TikTok

1. TikTok for Developers에서 앱 생성 → **Content Posting API** 추가
2. Redirect URI에 `{PUBLIC_BASE_URL}/api/oauth/tiktok/callback` 등록
3. 스코프 `user.info.basic`, `video.publish`, `video.upload` 신청
4. `.env`의 `TIKTOK_CLIENT_KEY` / `TIKTOK_CLIENT_SECRET` 입력

> 심사(Audit) 전 앱은 **나만 보기(SELF_ONLY)** 로만 게시할 수 있습니다. 앱이 지원하는 공개 범위는 연결 시 TikTok에서 받아와 선택지에 그대로 표시됩니다.

### Instagram + Facebook (Meta 공용)

1. Meta for Developers에서 앱 생성 → **Facebook 로그인** 제품 추가
2. 유효한 OAuth 리디렉션 URI에 `{PUBLIC_BASE_URL}/api/oauth/meta/callback` 등록
3. `.env`의 `META_APP_ID` / `META_APP_SECRET` 입력
4. 필요한 권한: `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`, `publish_video`, `instagram_basic`, `instagram_content_publish`

한 번 로그인하면 관리 중인 **모든 Facebook 페이지**와, 각 페이지에 연결된 **Instagram 비즈니스/크리에이터 계정**이 자동으로 등록됩니다.

> Instagram 게시는 파일 업로드가 아니라 "공개 URL에서 영상 가져가기" 방식입니다. 이 앱은 업로드한 영상을 `{PUBLIC_BASE_URL}/media/{임의토큰}` 으로 임시 공개하므로, **`PUBLIC_BASE_URL`이 반드시 외부에서 접근 가능해야** 합니다. 개인 계정은 지원되지 않고 비즈니스/크리에이터 계정 + 페이지 연결이 필요합니다.

## 3. 동작 방식

```
브라우저 --(1회 업로드)--> FastAPI(data/uploads)
                              ├─ YouTube   : resumable upload → videos.insert
                              ├─ TikTok    : publish/video/init → PUT → status/fetch 폴링
                              ├─ Instagram : media(REELS, video_url) → 상태 폴링 → media_publish
                              └─ Facebook  : {page-id}/videos 멀티파트 업로드
```

- 선택한 **계정 단위**로 전송 작업이 만들어져 `asyncio.gather`로 **동시에** 진행되고, 하나가 실패해도 나머지는 계속됩니다(작업 상태: 성공 / 일부 성공 / 실패).
- 진행률은 실제 전송 바이트 기준으로 기록되며 프론트가 1.5초 간격으로 폴링합니다.
- 업로드 원본은 7일이 지나면 서버 시작 시 자동 정리됩니다.

### 파일 구조

```
uploader/
├─ app/
│  ├─ main.py            # 라우트 (업로드/게시/OAuth/미디어 서빙)
│  ├─ jobs.py            # 게시 작업 오케스트레이션 + 데모 모드
│  ├─ db.py              # SQLite 저장소
│  ├─ config.py          # 환경설정 + 토큰 암호화
│  └─ platforms/         # 플랫폼별 어댑터
│     ├─ youtube.py  tiktok.py  instagram.py  facebook.py  meta.py
│     └─ base.py         # 스트리밍/캡션 헬퍼
└─ static/index.html     # 홈/업로드/계정/기록 4개 페이지 (해시 라우팅)
```

### 주요 API

| 메서드 | 경로 | 설명 |
| --- | --- | --- |
| `POST` | `/api/login` · `/api/logout` | 로그인 / 로그아웃 |
| `GET` | `/api/security` | 잠금 상태 조회 |
| `POST`/`DELETE` | `/api/password` | 비밀번호 설정·변경 / 잠금 해제 |
| `GET` | `/healthz` | 헬스체크 (호스팅 서비스용) |
| `GET` | `/api/platforms` | 플랫폼 설정 상태와 연결된 계정 |
| `GET` | `/api/oauth/{platform}/start` | OAuth 시작(키 없으면 데모 계정 생성) |
| `GET` | `/api/oauth/{provider}/callback` | OAuth 콜백 |
| `POST` | `/api/accounts/manual` | 아이디만 직접 등록(로그인 없이) |
| `PATCH` | `/api/accounts/{id}` | 계정 별칭 변경 |
| `DELETE` | `/api/accounts/{id}` | 계정 연결 해제 |
| `GET` | `/api/sets` | 채널 세트 목록 |
| `POST` | `/api/sets` | 세트 생성 |
| `PATCH` | `/api/sets/{id}` | 세트 이름·구성 변경 |
| `DELETE` | `/api/sets/{id}` | 세트 삭제 |
| `POST` | `/api/media` | 영상 업로드 |
| `POST` | `/api/media/{id}/thumbnail` | 썸네일 업로드(YouTube용) |
| `POST` | `/api/jobs` | 선택한 플랫폼에 동시 게시 시작 |
| `GET` | `/api/jobs`, `/api/jobs/{id}` | 진행 상황 / 기록 |
| `GET` | `/media/{token}` | 업로드 영상 공개 서빙(Range 지원) |

## 4. 외부에서 사용하기 (배포)

> **먼저 잠그세요.** 이 앱은 연결된 계정에 영상을 올릴 수 있으므로, 공개 주소에 그대로 두면
> 주소를 아는 누구나 내 채널에 게시할 수 있습니다.

비밀번호를 거는 방법은 두 가지이고, 둘 다 걸면 **브라우저에서 정한 값이 우선**합니다.

**① 브라우저에서 (파일 편집 없이)**
`계정 관리 → 비밀번호 잠금`에서 비밀번호를 정하면 바로 잠깁니다. 같은 화면에서 변경·해제도 됩니다.
비밀번호는 PBKDF2로 해싱해 SQLite에 저장되고 평문은 남지 않습니다.
잠금이 꺼져 있으면 헤더에 `🔓 비밀번호 미설정` 경고가 뜹니다.

**② 서버 환경변수로**

```bash
python3 -c "import secrets; print('APP_PASSWORD=' + secrets.token_urlsafe(12))"
python3 -c "import secrets; print('APP_SECRET=' + secrets.token_urlsafe(32))"
```

`.env`(또는 호스팅 서비스의 환경변수)에 `APP_PASSWORD`를 넣고 재시작하면 처음부터 잠긴 상태로 뜹니다.
**외부에 배포할 때는 이 방법을 권장합니다** — 잠금이 꺼진 채로 공개되면, 먼저 접속한 사람이 비밀번호를
정해버릴 수 있기 때문입니다. (환경변수로 건 잠금은 브라우저에서 끌 수 없고, 변경만 가능합니다.)

로그인 세션은 서명된 HttpOnly 쿠키로 `SESSION_DAYS`(기본 14일) 동안 유지되고, 실패가 5분에 8회를
넘으면 해당 IP를 잠시 막습니다. Instagram이 영상을 가져가는 `/media/{임의토큰}`만 잠금에서 제외됩니다.

### 방법 A — Render 등 컨테이너 호스팅 (권장)

리포지토리에 `uploader/Dockerfile`과 `uploader/render.yaml`이 있습니다.

1. Render에서 **New → Blueprint**로 이 리포지토리를 연결 (`render.yaml` 자동 인식)
2. 환경변수 입력: `APP_PASSWORD`, 플랫폼 API 키들 (`APP_SECRET`은 자동 생성)
3. 배포 후 발급된 주소(`https://xxx.onrender.com`)를 **`PUBLIC_BASE_URL`에 넣고 재배포**
4. 각 플랫폼 개발자 콘솔의 리디렉션 URI를 그 주소로 갱신
   (`https://xxx.onrender.com/api/oauth/youtube/callback` 등 — 앱의 `계정 관리` 하단에 그대로 표시됩니다)

디스크(`/data`)를 붙여야 계정·세트·업로드 기록이 재배포 후에도 유지됩니다.
Fly.io, Railway, Cloud Run 등 다른 서비스도 같은 Dockerfile로 올라갑니다.

### 방법 B — 내 서버 / VPS

```bash
cd uploader
export APP_PASSWORD='...' APP_SECRET='...' PUBLIC_BASE_URL='https://내도메인'
docker compose up -d          # 8100 포트, 데이터는 uploader-data 볼륨에 보존
```

HTTPS는 앞단에 Caddy/Nginx 같은 리버스 프록시를 두세요. OAuth와 Instagram 모두 https 주소를 요구합니다.

### 방법 C — 잠깐만 외부에 열기

로컬에서 돌리면서 임시 주소가 필요할 때:

```bash
cloudflared tunnel --url http://localhost:8100   # 또는 ngrok http 8100
```

출력된 https 주소를 `.env`의 `PUBLIC_BASE_URL`에 넣고 서버를 재시작하면 그대로 씁니다.
터널을 끄면 주소도 사라지므로 테스트용입니다.

## 5. 알아둘 점

- 각 플랫폼의 게시 권한은 **앱 심사**를 통과해야 일반 사용자에게 열립니다. 심사 전에는 개발자 본인 계정으로만 테스트할 수 있습니다.
- 로그인은 **비밀번호 하나를 공유하는 단일 사용자 방식**입니다. 여러 명이 각자 계정으로 쓰는 구조가 아니므로, 팀에서 함께 쓴다면 비밀번호를 공유하거나 앞단에 별도 인증(SSO 등)을 두세요.
- 영상 규격(길이·비율·용량)은 플랫폼마다 다릅니다. UI에서 세로/길이에 따른 주의 문구를 표시하지만, 최종 판정은 각 플랫폼이 합니다.
