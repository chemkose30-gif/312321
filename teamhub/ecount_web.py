"""이카운트 웹 화면에서 판매현황 엑셀을 자동으로 받아 옴 (브라우저 자동 조작, Playwright).

이카운트 Open API 에는 판매현황 조회가 없어서, 사람이 하던 순서를 그대로 따라 함:
  1) 로그인 화면(https://login{zone}.ecount.com/)에서 회사코드·아이디·비밀번호 입력 → 로그인
  2) 로그인 뒤 주소에 판매현황 화면 주소(#menuType=...&prgId=... 부분)를 붙여 이동
  3) 검색(F8) → Excel 버튼 → 내려받은 파일을 돌려줌
단계마다 화면을 캡처해 두어, 이카운트 화면이 바뀌어 멈추면 어디서 멈췄는지 볼 수 있게 함.
"""
import os
import re
import time
from pathlib import Path

if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH") and Path("/opt/teamhub/pw-browsers").exists():
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/teamhub/pw-browsers"

COM_SEL = ["#com_code", "input[name='com_code']", "input[name='COM_CODE']", "input[placeholder*='회사코드']", "input[title*='회사코드']"]
ID_SEL = ["#id", "input[name='id']", "input[name='USER_ID']", "input[placeholder*='아이디']", "input[title*='아이디']"]
PW_SEL = ["#passwd", "input[name='passwd']", "input[name='password']", "input[type='password']"]
LOGIN_BTN = ["#save", "button:has-text('로그인')", "a:has-text('로그인')", "input[type='submit']", "button[type='submit']"]
# 로그인 뒤 뜨는 안내·중복 로그인 확인 창
OK_BTN = ["button:has-text('확인')", "a:has-text('확인')", "button:has-text('예')", "button:has-text('계속')"]
SEARCH_BTN = ["button:has-text('검색(F8)')", "a:has-text('검색(F8)')", "button:has-text('검색')", "a:has-text('검색')"]
EXCEL_BTN = ["button:has-text('Excel(화면)')", "a:has-text('Excel(화면)')", "button:has-text('Excel')", "a:has-text('Excel')",
             "button:has-text('엑셀')", "a:has-text('엑셀')"]


class Fail(Exception):
    pass


def _visible(frame, sels):
    """프레임 안에서 처음 보이는 요소."""
    for s in sels:
        try:
            loc = frame.locator(s)
            for i in range(min(loc.count(), 5)):
                el = loc.nth(i)
                if el.is_visible():
                    return el
        except Exception:
            continue
    return None


def _find(page, sels):
    """페이지와 모든 iframe 에서 찾기 (이카운트 화면은 iframe 안에 있는 경우가 많음)."""
    for fr in [page.main_frame] + [f for f in page.frames if f != page.main_frame]:
        el = _visible(fr, sels)
        if el:
            return el
    return None


def run(cfg: dict, out_dir: Path, log) -> Path:
    """cfg: login_url, com_code, user_id, password, menu_hash, headless(bool). log(step, msg, shot_path|None).
    내려받은 엑셀 경로를 돌려줌. 실패하면 Fail."""
    from playwright.sync_api import sync_playwright
    out_dir.mkdir(parents=True, exist_ok=True)
    n = [0]

    def shot(page, step, msg):
        n[0] += 1
        p = out_dir / f"{n[0]:02d}.png"
        try:
            page.screenshot(path=str(p), full_page=False)
        except Exception:
            p = None
        log(step, msg, p.name if p else None)

    def dump(page):
        try:
            (out_dir / "page.html").write_text(page.content(), encoding="utf-8")
            for i, f in enumerate(page.frames[1:6], 1):
                (out_dir / f"frame{i}.html").write_text(f"<!-- {f.url} -->\n" + f.content(), encoding="utf-8")
        except Exception:
            pass

    for k in ("com_code", "user_id", "password", "menu_hash"):
        if not str(cfg.get(k) or "").strip():
            raise Fail({"com_code": "회사코드", "user_id": "아이디", "password": "비밀번호",
                        "menu_hash": "판매현황 화면 주소"}[k] + "가 설정되지 않았습니다.")
    with sync_playwright() as pw:
        exe = os.getenv("TEAMHUB_CHROMIUM") or None   # 따로 설치한 크롬을 쓸 때
        try:
            browser = pw.chromium.launch(headless=cfg.get("headless", True), args=["--no-sandbox"], executable_path=exe)
        except Exception as e:
            raise Fail("브라우저를 실행하지 못했습니다 — 서버에서 업데이트 명령을 다시 실행해 주세요. " + str(e).splitlines()[0][:200])
        ctx = browser.new_context(accept_downloads=True, locale="ko-KR", timezone_id="Asia/Seoul",
                                  viewport={"width": 1600, "height": 1000},
                                  user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                                             "Chrome/124.0 Safari/537.36")
        ctx.set_default_timeout(20000)
        page = ctx.new_page()
        page.on("dialog", lambda d: d.accept())   # alert/confirm 창은 확인
        try:
            # 1) 로그인
            page.goto(cfg.get("login_url") or "https://loginab.ecount.com/", wait_until="domcontentloaded", timeout=40000)
            page.wait_for_timeout(2500)
            com, uid, pwd = _find(page, COM_SEL), _find(page, ID_SEL), _find(page, PW_SEL)
            if not (com and uid and pwd):
                dump(page)
                shot(page, "로그인 화면", "회사코드·아이디·비밀번호 칸을 찾지 못했습니다.")
                raise Fail("로그인 칸을 찾지 못했습니다 (화면 캡처 확인).")
            com.fill(str(cfg["com_code"]).strip())
            uid.fill(str(cfg["user_id"]).strip())
            pwd.fill(str(cfg["password"]))
            shot(page, "로그인 화면", "회사코드·아이디·비밀번호 입력")
            btn = _find(page, LOGIN_BTN)
            if btn:
                btn.click()
            else:
                pwd.press("Enter")
            login_host = page.url.split("/")[2]
            # 로그인 화면을 벗어날 때까지 (중복 로그인 등 확인 창은 눌러 줌)
            for _ in range(30):
                page.wait_for_timeout(1000)
                if page.url.split("/")[2] != login_host or re.search(r"ec5|ECERP|ECMAIN", page.url, re.I):
                    break
                ok = _find(page, OK_BTN)
                if ok:
                    try:
                        ok.click()
                    except Exception:
                        pass
            else:
                dump(page)
                shot(page, "로그인", "로그인 화면을 벗어나지 못했습니다.")
                raise Fail("로그인에 실패했습니다 (비밀번호·보안인증·접속 IP 제한 확인, 화면 캡처 참고).")
            page.wait_for_timeout(5000)
            for _ in range(3):   # 공지 팝업 닫기
                page.keyboard.press("Escape")
                page.wait_for_timeout(300)
            shot(page, "로그인", "로그인 완료")
            # 2) 판매현황 화면으로
            h = str(cfg["menu_hash"]).strip()
            h = h.split("#", 1)[1] if "#" in h else h
            page.goto(page.url.split("#")[0] + "#" + h, wait_until="domcontentloaded")
            page.wait_for_timeout(7000)
            shot(page, "판매현황", "판매현황 화면 열기")
            # 3) 검색 (F8) → Excel
            sb = _find(page, SEARCH_BTN)
            if sb:
                try:
                    sb.click()
                except Exception:
                    page.keyboard.press("F8")
            else:
                page.keyboard.press("F8")
            page.wait_for_timeout(6000)
            shot(page, "검색", "검색 결과")
            ex = _find(page, EXCEL_BTN)
            if not ex:
                dump(page)
                raise Fail("Excel 버튼을 찾지 못했습니다 (화면 캡처 확인).")
            with page.expect_download(timeout=90000) as dl:
                ex.click()
                page.wait_for_timeout(1500)
                more = _find(page, ["button:has-text('Excel(화면)')", "a:has-text('Excel(화면)')"] + OK_BTN)   # 엑셀 종류를 묻는 창
                if more:
                    try:
                        more.click()
                    except Exception:
                        pass
            d = dl.value
            name = d.suggested_filename or f"sales_{int(time.time())}.xlsx"
            path = out_dir / ("download_" + re.sub(r"[^\w.\-]", "_", name))
            d.save_as(str(path))
            shot(page, "엑셀", f"엑셀 받음: {name}")
            return path
        except Fail:
            raise
        except Exception as e:
            dump(page)
            shot(page, "오류", str(e)[:300])
            raise Fail(str(e)[:300])
        finally:
            try:
                browser.close()
            except Exception:
                pass
