#!/usr/bin/env bash
# 로컬 OCR 백엔드에 필요한 시스템 패키지 설치 (Ubuntu/Debian)
set -e
sudo apt-get update
sudo apt-get install -y tesseract-ocr tesseract-ocr-kor
echo "설치 완료. 확인:"
tesseract --version | head -1
tesseract --list-langs | grep -E "kor|eng" || true
