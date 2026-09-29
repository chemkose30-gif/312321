# 🎬 영상 변환기

직원들과 같이 쓰는 영상 변환 웹앱입니다.

- **유튜브 다운로드**: 링크를 넣으면 MP4(화질 선택) 또는 MP3로 받아요.
- **파일 → MP4 변환**: MKV, AVI, MOV, WMV, FLV, WEBM, TS 등을 MP4(H.264/AAC)로 바꿔요.
  - 이미 H.264 영상이면 재인코딩 없이 컨테이너만 바꿔서 **몇 초 만에** 끝나요.
  - 속도/화질을 고를 수 있어요: 빠르게 / 균형(추천) / 고화질
  - NVIDIA GPU가 있는 서버면 자동으로 GPU 인코딩(NVENC)을 써요.
- 공용 비밀번호로 접속을 제한해요.
- 동시에 처리하는 작업 수를 제한해서, 여러 명이 동시에 올려도 서버가 멈추지 않고 순서대로 처리돼요.

## 서버에 배포하기 (Docker)

추천 서버: CPU 4~8코어 VPS (Vultr, DigitalOcean, AWS Lightsail 등). Docker가 설치되어 있어야 해요.

```bash
git clone <이 저장소> && cd <저장소>/video-converter
cp .env.example .env
nano .env                 # APP_PASSWORD를 꼭 바꾸세요
docker compose up -d --build
```

이제 `http://서버IP:8000` 으로 접속하면 브라우저가 비밀번호를 물어봐요.
사용자 이름은 아무거나 입력하고, 비밀번호에 `APP_PASSWORD` 값을 넣으면 돼요.

### 설정 (.env)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `APP_PASSWORD` | (없음) | 접속 비밀번호. 비워두면 누구나 접속 가능하니 서버에서는 꼭 설정 |
| `MAX_UPLOAD_MB` | 4096 | 업로드 최대 크기 (MB) |
| `MAX_CONCURRENT` | 2 | 동시 처리 작업 수. 4코어면 2, 8코어면 3~4 추천 |
| `DISABLE_GPU` | 0 | 1로 하면 GPU가 있어도 CPU로 인코딩 |

### GPU 서버

NVIDIA 드라이버와 `nvidia-container-toolkit`을 설치하고, `docker-compose.yml`에서 `deploy:` 부분의 주석을 풀어주세요.
화면 아래에 "GPU 가속 사용 중"이 표시되면 적용된 거예요.

### HTTPS (권장)

비밀번호가 오가므로 도메인이 있다면 HTTPS를 쓰는 걸 권장해요. 가장 쉬운 방법은 [Caddy](https://caddyserver.com/)예요:

```bash
# 도메인을 서버 IP로 연결한 뒤
caddy reverse-proxy --from convert.회사도메인.com --to localhost:8000
```

그리고 방화벽에서 8000 포트는 닫고 80/443만 열어두세요.

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
