# MT5 시스템트레이딩 컨트롤러

MT5(ATFX)를 켜고, 원할 때 **시스템트레이딩(Algo Trading) 버튼을 켜거나 끄는** 프로그램입니다.
MT5가 설치된 **Windows PC**에서 실행하세요.

## 설치 (한 번만)
1. Python 3.10 이상 설치 (설치할 때 "Add Python to PATH" 체크)
2. 이 폴더의 `설치.bat` 더블클릭
3. `mt5_control.py` 안의 `MT5_PATH` 를 내 PC의 `terminal64.exe` 경로로 수정
   (MT5 바로가기 우클릭 → 속성 → 대상 에서 확인 가능)
4. MT5에서 로그인 정보 저장(비밀번호 저장) 되어 있어야 자동 로그인됩니다.

## 사용
| 파일 | 하는 일 |
|---|---|
| `컨트롤창.bat` | 버튼 창 열기 (MT5 켜기 / 시스템트레이딩 ON / OFF / 상태 표시) |
| `MT5켜기+시스템트레이딩ON.bat` | MT5 실행 후 시스템트레이딩 켜기 |
| `시스템트레이딩ON.bat` | 시스템트레이딩 켜기 |
| `시스템트레이딩OFF.bat` | 시스템트레이딩 끄기 |

명령줄: `python mt5_control.py start|on|off|status|gui`

## 동작 방식
- 상태는 MetaTrader5 파이썬 API(`terminal_info().trade_allowed`)로 읽습니다.
- 켜기/끄기는 MT5 창에 '시스템트레이딩' 버튼 명령을 보내고(안 되면 Ctrl+E), 실제로 바뀌었는지 다시 확인합니다.
- 시스템트레이딩을 끄면 EA(Gold Sniping)는 **새 주문을 못 내지만**, 이미 열린 포지션은 그대로 남습니다.
