# 찰나 1분 이야기 - 쇼츠 템플릿

상단 로고 + 제목, 가운데 영상과 자막, 하단 댓글 카드로 된 1080x1920 세로 쇼츠를 만듭니다.

## 준비
- Python 3, Pillow (`pip install pillow`), ffmpeg

## 사용법
1. 직접 녹화한 클립(예: 롤 리플레이 녹화)을 `clip.mp4`로 이 폴더에 넣습니다.
2. `example_config.json`을 복사해서 제목, 자막, 댓글을 바꿉니다.
   - 큰 제목에서 `[대괄호]`로 감싼 부분은 빨간색으로 강조됩니다.
   - 자막과 댓글의 `start`, `end`는 초 단위입니다.
3. 실행합니다.
   ```
   python make_short.py example_config.json --preview preview.png  # 화면 미리보기
   python make_short.py example_config.json                        # 영상 만들기
   ```

로고를 바꾸려면 `assets/logo.jpg`를 교체하세요. 폰트는 Black Han Sans, Noto Sans KR(SIL OFL)입니다.
