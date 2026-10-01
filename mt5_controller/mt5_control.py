"""
MT5 시스템트레이딩(Algo Trading) 컨트롤러 (Windows 전용)

기능
  - MT5 터미널 실행 (이미 켜져 있으면 그대로 사용)
  - 시스템트레이딩 버튼 켜기 / 끄기 / 상태 확인
  - 버튼 하나로 조작하는 작은 GUI 창

사용법
  python mt5_control.py start     # MT5 실행 (+ 시스템트레이딩 켜기는 --on 추가)
  python mt5_control.py on        # 시스템트레이딩 켜기
  python mt5_control.py off       # 시스템트레이딩 끄기
  python mt5_control.py status    # 현재 상태 출력
  python mt5_control.py gui       # 버튼 창 열기 (인자 없이 실행해도 GUI)

동작 원리
  - 상태 확인: MetaTrader5 파이썬 패키지의 terminal_info().trade_allowed
    (= 툴바의 '시스템트레이딩' 버튼 상태)
  - 토글: MT5 메인 창에 WM_COMMAND(32851) 메시지를 보냄 (버튼 클릭과 동일).
    실패하면 Ctrl+E 단축키로 한 번 더 시도.
  - 토글 후 실제 상태를 다시 읽어 성공 여부를 확인.
"""

import argparse
import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

try:
    import MetaTrader5 as mt5
except ImportError:
    sys.exit("MetaTrader5 패키지가 없습니다. 먼저 실행: pip install -r requirements.txt")

import psutil

# 콘솔 인코딩(cp949)에서 이모지/한글 출력 오류 방지
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# ── 설정 ────────────────────────────────────────────────────────────────
# MT5 설치 경로. 환경변수 MT5_PATH 로도 지정 가능.
# ATFX MT5 기본 설치 경로가 다르면 아래 값을 바꾸세요.
MT5_PATH = os.environ.get(
    "MT5_PATH", r"C:\Program Files\ATFX MetaTrader 5\terminal64.exe"
)

ALGO_TRADING_CMD_ID = 32851  # MT5 '시스템트레이딩' 버튼 명령 ID
WM_COMMAND = 0x0111
VK_CONTROL = 0x11
VK_E = 0x45
KEYEVENTF_KEYUP = 0x0002

user32 = ctypes.WinDLL("user32", use_last_error=True)
EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


# ── MT5 연결 ────────────────────────────────────────────────────────────
def connect(launch: bool = True) -> bool:
    """MT5에 연결. launch=True면 꺼져 있을 때 실행까지 한다."""
    if mt5.terminal_info() is not None:
        return True
    running = bool(find_terminal_pids())
    if not running:
        if not launch:
            return False
        if not os.path.exists(MT5_PATH):
            print(f"[오류] MT5 경로를 찾을 수 없습니다: {MT5_PATH}")
            print("       mt5_control.py 의 MT5_PATH 를 실제 terminal64.exe 경로로 바꿔주세요.")
            return False
        # 파이썬과 분리된 독립 프로세스로 실행 (이 스크립트가 끝나도 MT5는 계속 켜져 있음)
        subprocess.Popen([MT5_PATH], creationflags=subprocess.DETACHED_PROCESS)
        time.sleep(5)
    mt5.shutdown()  # 이전 연결이 끊긴 상태일 수 있으니 정리 후 재연결
    kwargs = {"timeout": 60_000}
    if os.path.exists(MT5_PATH):
        kwargs["path"] = MT5_PATH
    if not mt5.initialize(**kwargs):
        print(f"[오류] MT5 연결 실패: {mt5.last_error()}")
        return False
    return True


def is_algo_on() -> bool | None:
    info = mt5.terminal_info()
    return None if info is None else bool(info.trade_allowed)


# ── 창 찾기 / 토글 ──────────────────────────────────────────────────────
def find_terminal_pids() -> list[int]:
    pids = []
    for p in psutil.process_iter(["pid", "name", "exe"]):
        name = (p.info["name"] or "").lower()
        if name in ("terminal64.exe", "terminal.exe"):
            pids.append(p.info["pid"])
    return pids


def find_main_window() -> int | None:
    """MT5 프로세스의 메인(최상위, 보이는) 창 핸들을 찾는다."""
    info = mt5.terminal_info()
    target_dir = os.path.normcase(info.path) if info else None

    pids = set()
    for pid in find_terminal_pids():
        try:
            exe_dir = os.path.normcase(os.path.dirname(psutil.Process(pid).exe()))
        except psutil.Error:
            continue
        # 여러 MT5가 떠 있으면 현재 연결된 터미널만 대상으로
        if target_dir is None or exe_dir == target_dir:
            pids.add(pid)

    found: list[int] = []

    def callback(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and user32.GetWindow(hwnd, 4) == 0:  # GW_OWNER == 0
            buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, buf, 512)
            if buf.value:
                found.append(hwnd)
        return True

    user32.EnumWindows(EnumWindowsProc(callback), 0)
    return found[0] if found else None


def _send_ctrl_e(hwnd: int) -> None:
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.3)
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    user32.keybd_event(VK_E, 0, 0, 0)
    user32.keybd_event(VK_E, 0, KEYEVENTF_KEYUP, 0)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)


def _wait_state(want: bool, seconds: float = 5.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if is_algo_on() == want:
            return True
        time.sleep(0.25)
    return False


def set_algo(want: bool) -> bool:
    """시스템트레이딩 버튼을 want 상태로 만든다. 성공 여부 반환."""
    label = "켜기" if want else "끄기"
    current = is_algo_on()
    if current is None:
        print("[오류] MT5에 연결되어 있지 않습니다.")
        return False
    if current == want:
        print(f"이미 시스템트레이딩 {'ON' if want else 'OFF'} 상태입니다.")
        return True

    hwnd = find_main_window()
    if not hwnd:
        print("[오류] MT5 창을 찾지 못했습니다. MT5가 최소화가 아닌 상태로 켜져 있는지 확인하세요.")
        return False

    # 1차: 버튼 명령 직접 전송
    user32.PostMessageW(hwnd, WM_COMMAND, ALGO_TRADING_CMD_ID, 0)
    if _wait_state(want):
        print(f"시스템트레이딩 {label} 완료 ✅")
        return True

    # 2차: Ctrl+E 단축키
    if is_algo_on() != want:
        _send_ctrl_e(hwnd)
    if _wait_state(want):
        print(f"시스템트레이딩 {label} 완료 ✅ (Ctrl+E)")
        return True

    print(f"[오류] 시스템트레이딩 {label} 실패. 현재 상태: {status_text()}")
    return False


def status_text() -> str:
    state = is_algo_on()
    if state is None:
        return "MT5 연결 안 됨"
    return "ON (켜짐)" if state else "OFF (꺼짐)"


def start(turn_on: bool = False) -> bool:
    print("MT5 실행/연결 중...")
    if not connect(launch=True):
        return False
    acc = mt5.account_info()
    if acc:
        print(f"연결됨: {acc.login} / {acc.server} / 잔액 {acc.balance:.2f} {acc.currency}")
    print(f"시스템트레이딩: {status_text()}")
    if turn_on:
        # 차트/EA 로딩 시간을 조금 준다
        time.sleep(3)
        return set_algo(True)
    return True


# ── GUI ────────────────────────────────────────────────────────────────
def run_gui() -> None:
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.title("MT5 시스템트레이딩 컨트롤")
    root.geometry("320x230")
    root.resizable(False, False)
    root.attributes("-topmost", True)

    status_var = tk.StringVar(value="확인 중...")
    status_lbl = tk.Label(root, textvariable=status_var, font=("맑은 고딕", 14, "bold"))
    status_lbl.pack(pady=12)

    def refresh():
        if mt5.terminal_info() is None:
            connect(launch=False)
        state = is_algo_on()
        status_var.set(f"시스템트레이딩: {status_text()}")
        status_lbl.config(fg={True: "green", False: "red"}.get(state, "gray"))

    def do(action):
        def handler():
            ok = action()
            refresh()
            if not ok:
                messagebox.showerror("실패", "작업에 실패했습니다. 콘솔 메시지를 확인하세요.")
        return handler

    btns = tk.Frame(root)
    btns.pack(pady=4)
    tk.Button(btns, text="MT5 켜기", width=12, height=2,
              command=do(lambda: start(False))).grid(row=0, column=0, padx=6, pady=4)
    tk.Button(btns, text="새로고침", width=12, height=2,
              command=refresh).grid(row=0, column=1, padx=6, pady=4)
    tk.Button(btns, text="▶ 시스템트레이딩 ON", width=12, height=2, bg="#c8f7c5",
              command=do(lambda: connect(False) and set_algo(True))).grid(row=1, column=0, padx=6, pady=4)
    tk.Button(btns, text="■ 시스템트레이딩 OFF", width=12, height=2, bg="#f7c5c5",
              command=do(lambda: connect(False) and set_algo(False))).grid(row=1, column=1, padx=6, pady=4)

    def auto_refresh():
        refresh()
        root.after(3000, auto_refresh)

    auto_refresh()
    root.mainloop()


# ── CLI ────────────────────────────────────────────────────────────────
def main() -> int:
    if os.name != "nt":
        sys.exit("이 프로그램은 Windows(MT5가 설치된 PC)에서만 동작합니다.")

    parser = argparse.ArgumentParser(description="MT5 시스템트레이딩 컨트롤러")
    parser.add_argument("command", nargs="?", default="gui",
                        choices=["start", "on", "off", "status", "gui"])
    parser.add_argument("--on", action="store_true", help="start 후 시스템트레이딩도 켜기")
    args = parser.parse_args()

    if args.command == "gui":
        run_gui()
        return 0
    if args.command == "start":
        return 0 if start(args.on) else 1
    if not connect(launch=False):
        print("MT5가 실행 중이 아닙니다. 먼저 'start' 를 실행하세요.")
        return 1
    if args.command == "status":
        print(f"시스템트레이딩: {status_text()}")
        return 0
    return 0 if set_algo(args.command == "on") else 1


if __name__ == "__main__":
    sys.exit(main())
