# 🎬 영상 변환기

로컬에서 실행하는 웹앱입니다.

- **유튜브 다운로드**: 링크를 넣으면 MP4(화질 선택) 또는 MP3로 받아요.
- **파일 → MP4 변환**: MKV, AVI, MOV, WMV, FLV, WEBM, TS 등 영상 파일을 MP4(H.264/AAC)로 바꿔요.
  이미 H.264 영상이면 재인코딩 없이 컨테이너만 바꿔서 빠르게 끝나요.

## 실행 방법

```bash
cd video-converter
pip install -r requirements.txt
python server.py
```

브라우저에서 http://localhost:8000 을 여세요.

ffmpeg를 따로 설치하지 않아도 돼요(`imageio-ffmpeg`에 포함된 ffmpeg를 사용하고, 시스템에 ffmpeg가 있으면 그걸 우선 사용해요).

## 참고

- 유튜브 다운로드가 갑자기 안 되면 `pip install -U yt-dlp`로 업데이트하세요.
- 변환된 파일은 다운로드 후 자동 삭제되고, 받지 않은 파일도 1시간 뒤에 정리돼요.
- 본인 영상이나 저작권 문제가 없는 콘텐츠에만 사용하세요.
