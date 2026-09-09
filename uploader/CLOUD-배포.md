# 클라우드에 올려서 24시간 돌리기

내 PC를 켜둘 필요 없이 항상 켜져 있고, https 주소가 생겨서 **인스타그램 릴스 게시 조건도 자동으로 충족**됩니다.
(인스타는 "외부에서 접근 가능한 영상 URL"을 요구하는데, 로컬 PC로는 이걸 만들기 번거롭습니다.)

두 가지 중 하나를 고르세요.

| | Render | Fly.io |
| --- | --- | --- |
| 방식 | 웹 화면에서 클릭만 | 명령어(CLI) 몇 줄 |
| 비용 | 월 $7 정도 (Starter, 디스크 포함) | 소형 머신 월 $3~5 수준 |
| 추천 | **처음이라면 이쪽** | 터미널이 익숙하면 |

> 무료 플랜(Render Free 등)은 접속이 없으면 잠들고 디스크도 없어서, 계정 연결과 업로드 기록이
> 재배포 때마다 사라집니다. 24시간 운영에는 맞지 않습니다.

---

## A. Render (웹에서 클릭)

**준비**: GitHub 계정, 이 저장소.

1. <https://render.com> 가입 → **New +** → **Blueprint**
2. GitHub 저장소 `312321` 연결 → 브랜치 `claude/multi-platform-upload-site-hw8baf` 선택
   (저장소 루트의 `render.yaml` 을 자동으로 읽습니다)
3. 환경변수 입력 창에서 **`APP_PASSWORD`** 에 로그인 비밀번호를 넣습니다 (8자 이상).
   - `APP_SECRET` 은 자동 생성됩니다.
   - `PUBLIC_BASE_URL` 은 비워두세요. Render 가 주는 주소로 앱이 알아서 잡습니다.
4. **Apply** → 몇 분 뒤 `https://이름.onrender.com` 주소가 나옵니다.
5. 그 주소로 접속 → 3번에서 정한 비밀번호로 로그인.

## B. Fly.io (명령어)

```bash
# 1) flyctl 설치 후 로그인
fly auth login

# 2) uploader 폴더에서
cd uploader
fly launch --no-deploy --copy-config      # 앱 이름과 지역(nrt=도쿄) 확인
fly volumes create uploader_data --size 3 # 데이터 보관용 디스크

# 3) 비밀번호와 암호화 키 설정
fly secrets set APP_PASSWORD='정한비밀번호' APP_SECRET="$(openssl rand -base64 32)"

# 4) 배포
fly deploy
fly open        # 브라우저에서 열기
```

`fly.toml` 에 `auto_stop_machines = false`, `min_machines_running = 1` 로 되어 있어 **잠들지 않습니다.**

---

## 안 되는 곳 (자주 묻는 것)

| 서비스 | 왜 안 되는가 |
| --- | --- |
| **Vercel / Netlify** | 서버리스 방식이라 ① 요청 본문이 **4.5MB로 제한**되어 영상 업로드 자체가 불가, ② 파일 시스템이 휘발성이라 SQLite·영상이 남지 않음, ③ 요청이 끝나면 프로세스가 사라져 게시 진행률·상태 폴링이 끊김 |
| **GitHub Actions** | 작업당 최대 6시간, 외부 접속 주소 없음, 상시 서비스 호스팅은 약관 위반 |
| **GitHub Pages** | 정적 파일 전용. 파이썬 서버를 돌릴 수 없음 |

이 앱은 **큰 영상 파일을 받아서 몇 분간 붙잡고 플랫폼에 올리는** 성격이라,
"항상 떠 있는 프로세스 + 디스크"가 필요합니다. 컨테이너·VM 방식(Render, Fly, Railway, VPS)을 쓰세요.

---

## 배포 후 반드시 할 일 — 플랫폼 리디렉션 URI 등록

실제 게시를 하려면 각 플랫폼 개발자 콘솔에 **새 주소**를 등록해야 합니다.
앱에 로그인한 뒤 `계정 관리` 맨 아래 **리디렉션 URI 설정**에 정확한 주소가 표시되니 그대로 복사하세요.

| 플랫폼 | 등록할 곳 | 주소 형태 |
| --- | --- | --- |
| YouTube | Google Cloud Console → OAuth 클라이언트 | `https://내주소/api/oauth/youtube/callback` |
| TikTok | TikTok for Developers → 앱 설정 | `https://내주소/api/oauth/tiktok/callback` |
| Instagram + Facebook | Meta for Developers → Facebook 로그인 | `https://내주소/api/oauth/meta/callback` |

그리고 각 플랫폼 키를 환경변수에 넣고 재배포하세요.
(`YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET`,
`META_APP_ID`, `META_APP_SECRET`)

## 알아둘 점

- **`APP_PASSWORD` 는 반드시 배포 전에 넣으세요.** 최초 설정 화면은 프록시를 거친 요청(=클라우드)에서는
  열리지 않으므로, 클라우드에서는 환경변수로 지정한 비밀번호로만 로그인합니다.
- 비밀번호를 바꾸려면 **Environment 에서 `APP_PASSWORD` 값을 수정**하세요. 저장하면 자동 재시작되며
  환경변수 값이 기존 값보다 우선합니다.
- 계정·세트·업로드 기록은 디스크(`/data`)에 저장됩니다. 디스크를 떼면 재배포 때 사라집니다.
- 업로드한 영상 원본은 7일 뒤 자동 정리됩니다(6시간마다 검사).
- 배포가 재시작되면 진행 중이던 게시는 "서버가 재시작되어 중단되었습니다"로 표시됩니다. 다시 올리면 됩니다.
