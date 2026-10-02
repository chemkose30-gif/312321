"""Outlook(Office 365) 메일 연동 - 업무 지시/완료 알림, 메일에서 바로 상태 처리, 일일 리마인더."""
import hashlib
import hmac
import html
import os
import smtplib
import threading
import time
from datetime import datetime
from email.message import EmailMessage

SMTP_HOST = os.getenv("TEAMHUB_SMTP_HOST", "smtp.office365.com")
SMTP_PORT = int(os.getenv("TEAMHUB_SMTP_PORT", "587"))
SMTP_USER = os.getenv("TEAMHUB_SMTP_USER", "")
SMTP_PASSWORD = os.getenv("TEAMHUB_SMTP_PASSWORD", "")
MAIL_FROM = os.getenv("TEAMHUB_MAIL_FROM", SMTP_USER)
BASE_URL = os.getenv("TEAMHUB_BASE_URL", "http://localhost:8100").rstrip("/")
REMINDER_HOUR = int(os.getenv("TEAMHUB_REMINDER_HOUR", "9"))

PRIORITY_LABEL = {"urgent": "긴급", "high": "높음", "normal": "보통", "low": "낮음"}
STATUS_LABEL = {"todo": "대기", "doing": "진행중", "done": "완료", "hold": "보류"}


def enabled() -> bool:
    return bool(SMTP_USER and SMTP_PASSWORD)


def status_info() -> dict:
    return {"enabled": enabled(), "host": SMTP_HOST, "port": SMTP_PORT, "from": MAIL_FROM,
            "base_url": BASE_URL, "reminder_hour": REMINDER_HOUR}


# ---------------------------------------------------------------- 서명 링크
def sign(secret: str, task_id: int, user_id: int, status: str) -> str:
    msg = f"{task_id}:{user_id}:{status}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()[:32]


def verify(secret: str, task_id: int, user_id: int, status: str, sig: str) -> bool:
    return hmac.compare_digest(sign(secret, task_id, user_id, status), sig or "")


def action_url(secret: str, task_id: int, user_id: int, status: str) -> str:
    return f"{BASE_URL}/mail/task/{task_id}?u={user_id}&s={status}&sig={sign(secret, task_id, user_id, status)}"


# ---------------------------------------------------------------- 발송
def _send(to: str, subject: str, body_html: str):
    msg = EmailMessage()
    msg["From"] = MAIL_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content("HTML 메일을 지원하는 메일 프로그램(Outlook 등)에서 확인하세요.\n" + BASE_URL)
    msg.add_alternative(body_html, subtype="html")
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASSWORD)
        s.send_message(msg)


def send_async(db_factory, to: str, subject: str, body_html: str, task_id=None, kind="info"):
    """메일을 백그라운드로 발송하고 결과를 mail_log 에 남긴다."""
    if not to:
        return

    def run():
        if not enabled():
            result, error = "skipped", "메일 설정(TEAMHUB_SMTP_USER/PASSWORD)이 없습니다."
        else:
            try:
                _send(to, subject, body_html)
                result, error = "sent", ""
            except Exception as e:  # noqa: BLE001 - 실패 사유를 기록
                result, error = "failed", str(e)[:300]
        with db_factory() as c:
            c.execute(
                "INSERT INTO mail_log (task_id, kind, recipient, subject, result, error, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (task_id, kind, to, subject, result, error, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )

    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------- 템플릿
def _layout(title: str, inner: str) -> str:
    return f"""<div style="font-family:'Malgun Gothic',sans-serif;max-width:600px;margin:0 auto;color:#1f2937">
  <div style="background:#2563eb;color:#fff;padding:14px 20px;font-weight:bold;font-size:16px">TeamHub</div>
  <div style="padding:20px;border:1px solid #e5e7eb;border-top:0">
    <h2 style="margin:0 0 14px;font-size:18px">{html.escape(title)}</h2>{inner}
    <p style="margin-top:24px;font-size:12px;color:#6b7280">TeamHub 바로가기: <a href="{BASE_URL}">{BASE_URL}</a></p>
  </div></div>"""


def _button(url: str, label: str, color: str) -> str:
    return (f'<a href="{url}" style="display:inline-block;background:{color};color:#fff;text-decoration:none;'
            f'padding:10px 18px;border-radius:6px;font-weight:bold;margin-right:8px">{label}</a>')


def _task_table(t) -> str:
    rows = [("업무", f"<b>{html.escape(t['title'])}</b>"),
            ("지시자", html.escape(t["assigner_name"])),
            ("담당자", html.escape(t["assignee_name"])),
            ("우선순위", PRIORITY_LABEL.get(t["priority"], t["priority"])),
            ("마감일", html.escape(t["due_date"] or "-")),
            ("상태", STATUS_LABEL.get(t["status"], t["status"]))]
    trs = "".join(f'<tr><td style="padding:6px 10px;color:#6b7280;width:80px">{k}</td>'
                  f'<td style="padding:6px 10px">{v}</td></tr>' for k, v in rows)
    desc = (f'<div style="white-space:pre-wrap;background:#f4f6fb;padding:12px;border-radius:6px;margin-top:10px">'
            f'{html.escape(t["description"])}</div>') if t["description"] else ""
    return f'<table style="border-collapse:collapse;width:100%;font-size:14px">{trs}</table>{desc}'


def assigned_mail(secret: str, t) -> tuple:
    buttons = (_button(action_url(secret, t["id"], t["assignee_id"], "doing"), "▶ 진행 시작", "#2563eb") +
               _button(action_url(secret, t["id"], t["assignee_id"], "done"), "✔ 완료 처리", "#16a34a"))
    inner = (f"<p>{html.escape(t['assigner_name'])}님이 업무를 지시했습니다.</p>{_task_table(t)}"
             f'<p style="margin-top:18px">{buttons}</p>'
             '<p style="font-size:12px;color:#6b7280">버튼을 누르면 TeamHub에 바로 반영되고 지시자에게 알림이 갑니다.</p>')
    return f"[업무지시] {t['title']}", _layout("새 업무가 지시되었습니다", inner)


def status_mail(t, actor_name: str, new_status: str, via_mail: bool) -> tuple:
    label = STATUS_LABEL[new_status]
    how = " (메일에서 처리)" if via_mail else ""
    inner = f"<p>{html.escape(actor_name)}님이 업무 상태를 <b>[{label}]</b>(으)로 변경했습니다{how}.</p>{_task_table(t)}"
    return f"[업무{label}] {t['title']} - {actor_name}", _layout(f"업무 상태 변경: {label}", inner)


def reminder_mail(secret: str, name: str, tasks, today: str) -> tuple:
    items = ""
    for t in tasks:
        late = t["due_date"] and t["due_date"] < today
        due = f'<span style="color:{"#dc2626" if late else "#1f2937"}">{html.escape(t["due_date"] or "-")}{" (지연)" if late else ""}</span>'
        items += (f'<tr><td style="padding:8px;border-bottom:1px solid #e5e7eb"><b>{html.escape(t["title"])}</b><br>'
                  f'<small style="color:#6b7280">지시: {html.escape(t["assigner_name"])} · 마감 {due} · {STATUS_LABEL[t["status"]]}</small></td>'
                  f'<td style="padding:8px;border-bottom:1px solid #e5e7eb;text-align:right">'
                  f'{_button(action_url(secret, t["id"], t["assignee_id"], "done"), "완료", "#16a34a")}</td></tr>')
    inner = (f"<p>{html.escape(name)}님, 오늘까지 마감이거나 기한이 지난 업무가 {len(tasks)}건 있습니다.</p>"
             f'<table style="border-collapse:collapse;width:100%;font-size:14px">{items}</table>')
    return f"[업무 리마인더] 마감 임박/지연 업무 {len(tasks)}건", _layout("업무 리마인더", inner)


def assigner_summary_mail(name: str, tasks, today: str) -> tuple:
    items = "".join(
        f'<tr><td style="padding:8px;border-bottom:1px solid #e5e7eb">{html.escape(t["title"])}</td>'
        f'<td style="padding:8px;border-bottom:1px solid #e5e7eb">{html.escape(t["assignee_name"])}</td>'
        f'<td style="padding:8px;border-bottom:1px solid #e5e7eb;color:#dc2626">{html.escape(t["due_date"])}</td>'
        f'<td style="padding:8px;border-bottom:1px solid #e5e7eb">{STATUS_LABEL[t["status"]]}</td></tr>'
        for t in tasks)
    inner = (f"<p>{html.escape(name)}님이 지시한 업무 중 기한이 지났는데 완료되지 않은 업무가 {len(tasks)}건 있습니다.</p>"
             '<table style="border-collapse:collapse;width:100%;font-size:14px"><tr style="background:#f4f6fb">'
             '<th style="padding:8px;text-align:left">업무</th><th style="padding:8px;text-align:left">담당자</th>'
             '<th style="padding:8px;text-align:left">마감일</th><th style="padding:8px;text-align:left">상태</th></tr>'
             f"{items}</table>")
    return f"[미완료 업무] 지시한 업무 중 지연 {len(tasks)}건", _layout("지시한 업무 미완료 현황", inner)


# ---------------------------------------------------------------- 일일 리마인더 스케줄러
def start_scheduler(run_daily):
    """매일 REMINDER_HOUR 시 이후 하루 한 번 run_daily(today) 를 호출한다."""

    def loop():
        while True:
            try:
                now = datetime.now()
                if now.hour >= REMINDER_HOUR:
                    run_daily(now.strftime("%Y-%m-%d"))
            except Exception as e:  # noqa: BLE001
                print("[TeamHub] reminder error:", e)
            time.sleep(60)

    threading.Thread(target=loop, daemon=True).start()
