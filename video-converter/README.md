# 🎬 영상 변환기

직원들과 같이 쓰는 영상 변환 웹앱입니다.

- **유튜브 다운로드**: 링크를 넣으면 MP4(화질 선택) 또는 MP3로 받아요.
- **파일 → MP4 변환**: MKV, AVI, MOV, WMV, FLV, WEBM, TS 등을 MP4(H.264/AAC)로 바꿔요.
  - 이미 H.264 영상이면 재인코딩 없이 컨테이너만 바꿔서 **몇 초 만에** 끝나요.
  - 속도/화질을 고를 수 있어요: 빠르게 / 균형(추천) / 고화질
  - NVIDIA GPU가 있는 서버면 자동으로 GPU 인코딩(NVENC)을 써요.
- 공용 비밀번호로 접속을 제한해요.
- 동시에 처리하는 작업 수를 제한해서, 여러 명이 동시에 올려도 서버가 멈추지 않고 순서대로 처리돼요.

## 서버에 배포하기 (VPS)

### 1. 서버 만들기

- **지역**: 서울(Seoul) — 업로드 속도가 체감 속도를 좌우하므로 가까운 곳이 좋아요
- **사양**: 4 vCPU / 8GB RAM 정도부터 시작 (자주 쓰면 8 vCPU)
- **CPU 종류**: 가능하면 "CPU Optimized / Dedicated CPU". 순간 성능만 보장하는 "burstable" 요금제(AWS Lightsail, t 시리즈 등)는 인코딩처럼 오래 CPU를 쓰면 느려지니 피하세요
- **디스크**: 50GB 이상
- **OS**: Ubuntu 24.04
- 클라우드 방화벽을 쓴다면 **22, 80, 443 포트**를 열어주세요

### 2. 설치 (명령어 3줄)

서버에 SSH로 접속한 뒤:

```bash
git clone <이 저장소 주소> app && cd app
git checkout feat/video-converter   # main에 합치기 전이라면 (폴더 이동보다 먼저)
cd video-converter
sudo bash setup.sh
```

끝나면 접속 주소와 비밀번호가 출력돼요. 예:

```
접속 주소 : https://203-0-113-7.sslip.io
비밀번호  : fwEMoUGHyu5R5zav
```

도메인을 사지 않아도 `서버IP.sslip.io` 주소로 HTTPS가 자동 적용돼요.
나중에 도메인을 사면 `.env`의 `DOMAIN`만 바꾸고 `docker compose up -d` 하면 돼요.

### 설정 (.env)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `APP_PASSWORD` | (자동 생성) | 접속 비밀번호. 퇴사자가 생기면 바꿔주세요 |
| `DOMAIN` | 서버IP.sslip.io | 접속 주소 |
| `MAX_UPLOAD_MB` | 4096 | 업로드 최대 크기 (MB) |
| `MAX_CONCURRENT` | CPU 코어 수 ÷ 2 (최소 2) | 동시 처리 작업 수 |
| `DISABLE_GPU` | 0 | 1로 하면 GPU가 있어도 CPU로 인코딩 |

`.env`를 수정한 뒤에는 `docker compose up -d`로 적용하세요.

### 자주 쓰는 명령어

```bash
docker compose logs -f video-converter   # 로그 보기
docker compose restart                   # 재시작
git pull && docker compose up -d --build # 업데이트 (yt-dlp도 최신으로)
```

### GPU 서버

NVIDIA 드라이버와 `nvidia-container-toolkit`을 설치하고, `docker-compose.yml`에서 `deploy:` 부분의 주석을 풀어주세요.
화면 아래에 "GPU 가속 사용 중"이 표시되면 적용된 거예요.

## 유튜브 "봇 확인(Sign in to confirm you're not a bot)" 차단 해결

유튜브는 클라우드 서버 IP를 봇으로 의심해 막는 경우가 많아요. 두 가지 방법이 있어요.

1. **쿠키 등록 (간단)**: 화면 아래 **⚙️ 유튜브 쿠키 설정**을 열고 안내대로 cookies.txt를 등록하세요.
   - 반드시 **보조 구글 계정**을 쓰세요. 다운로드가 많으면 해당 계정이 제한될 수 있어요.
   - 쿠키는 시간이 지나면 만료돼요. 다시 차단 메시지가 나오면 새로 등록하세요.
2. **프록시 (확실)**: 쿠키로도 안 되면 `.env`의 `YTDLP_PROXY`에 가정/사무실 회선의 프록시를 넣어 유튜브 요청만 우회시킬 수 있어요.

## 내 PC에서 실행하기

```bash
cd video-converter
pip install -r requirements.txt
python server.py
```

http://localhost:8000 을 여세요. ffmpeg를 따로 설치하지 않아도 돼요.

## 참고

- **유튜브는 클라우드 서버 IP를 자주 차단해요.** 서버에서 유튜브 다운로드가 안 되면 파일 변환만 서버에서 쓰고, 유튜브는 PC에서 실행해서 쓰세요.
- 유튜브 다운로드가 갑자기 안 되면 yt-dlp를 업데이트하세요: `docker compose build --no-cache && docker compose up -d`
- 변환된 파일은 다운로드 후 자동 삭제되고, 받지 않은 파일도 1시간 뒤에 정리돼요.
- 본인 영상이나 저작권 문제가 없는 콘텐츠에만 사용하세요.
