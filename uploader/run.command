#!/usr/bin/env bash
# 맥에서 더블클릭으로 실행할 수 있는 파일입니다.
cd "$(dirname "$0")"
exec ./run.sh "$@"
