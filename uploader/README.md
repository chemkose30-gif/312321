# 원클릭 멀티 업로더

영상 하나를 올리면 **YouTube · TikTok · Instagram 릴스 · Facebook 페이지**에 동시에 게시하는 웹앱입니다.
제목/설명/해시태그를 한 번만 쓰면 각 플랫폼 형식에 맞춰 변환되고, 플랫폼별 진행률과 결과 링크를 한 화면에서 확인할 수 있습니다.

- 백엔드: FastAPI + httpx (플랫폼 공식 API 직접 호출)
- 프론트: 의존성 없는 단일 HTML (`static/index.html`)
- 저장소: SQLite (액세스 토큰은 `APP_SECRET` 기반 Fernet으로 암호화 저장)

## 1. 빠른 시작 (데모 모드)

API 키가 하나도 없어도 화면 전체를 사용해 볼 수 있습니다. 이때 업로드는 실제로 전송되지 않고 시뮬레이션됩니다.

```bash
cd uploader
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # 그대로 두면 데모 모드
.venv/bin/python -m uvicorn app.main:app --port 8100
# http://localhost:8100
```

`계정 연결` 탭에서 각 플랫폼의 **연결하기**를 누르면 데모 계정이 생성되고, 업로드 탭에서 전체 흐름을 확인할 수 있습니다.

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

- 플랫폼 전송은 `asyncio.gather`로 **동시에** 진행되고, 하나가 실패해도 나머지는 계속됩니다(작업 상태: 성공 / 일부 성공 / 실패).
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
└─ static/index.html     # 단일 페이지 UI
```

### 주요 API

| 메서드 | 경로 | 설명 |
| --- | --- | --- |
| `GET` | `/api/platforms` | 플랫폼 설정 상태와 연결된 계정 |
| `GET` | `/api/oauth/{platform}/start` | OAuth 시작(키 없으면 데모 계정 생성) |
| `GET` | `/api/oauth/{provider}/callback` | OAuth 콜백 |
| `DELETE` | `/api/accounts/{id}` | 계정 연결 해제 |
| `POST` | `/api/media` | 영상 업로드 |
| `POST` | `/api/media/{id}/thumbnail` | 썸네일 업로드(YouTube용) |
| `POST` | `/api/jobs` | 선택한 플랫폼에 동시 게시 시작 |
| `GET` | `/api/jobs`, `/api/jobs/{id}` | 진행 상황 / 기록 |
| `GET` | `/media/{token}` | 업로드 영상 공개 서빙(Range 지원) |

## 4. 알아둘 점

- 각 플랫폼의 게시 권한은 **앱 심사**를 통과해야 일반 사용자에게 열립니다. 심사 전에는 개발자 본인 계정으로만 테스트할 수 있습니다.
- 인증은 이 앱을 로컬/사내에서 단독 실행하는 것을 전제로 하며, 로그인 기능이 없습니다. 외부에 공개 배포하려면 앞단에 인증(리버스 프록시 등)을 두세요.
- 영상 규격(길이·비율·용량)은 플랫폼마다 다릅니다. UI에서 세로/길이에 따른 주의 문구를 표시하지만, 최종 판정은 각 플랫폼이 합니다.
