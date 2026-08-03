"""
deploy.py - 6단계 배포 레이어.
Gmail SMTP로 주간 큐레이션 결과를 HTML 이메일 발송(main.py [6]단계에서 send_weekly_email 호출).
인증정보는 GitHub Secrets(SMTP_USER/SMTP_APP_PASSWORD/EMAIL_RECIPIENTS)에서 읽음.
storage.py가 만든 domestic/international summarized+by_category 데이터를 그대로 렌더링.
발송 실패해도 예외 안 던지고 로그만 남기고 조용히 실패(파이프라인 안 죽음).
"""

import html
import os
import smtplib
from datetime import datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

# 이메일 디자인 토큰. 국내=파랑, 해외=보라(증감표의 초록=증가와 겹치지 않게).
ACCENT_DOMESTIC = "#1a73e8"
ACCENT_DOMESTIC_TINT = "#e8f0fe"
ACCENT_INTL = "#7c3aed"
ACCENT_INTL_TINT = "#f3ebfd"

HEADER_BG = "#0f2f5c"


def _escape(value) -> str:
    return html.escape(str(value))


def _format_week_label_kr(week_label: str) -> str:
    """"2026-31"(ISO 연도-주차) -> "2026년 7월 4주차". 월요일 기준 몇 번째 주인지 계산.
    형식이 예상과 다르면 원본 그대로 반환."""
    try:
        monday = datetime.strptime(f"{week_label}-1", "%G-%V-%u")
    except ValueError:
        return week_label
    week_of_month = ((monday.day - 1) // 7) + 1
    return f"{monday.year}년 {monday.month}월 {week_of_month}주차"


def _format_issue_html(item: dict, rank: int | None = None, accent: str = ACCENT_DOMESTIC) -> str:
    """
    이슈 하나 분량의 HTML 카드.
    대표 제목은 item["generated_title"](LLM 생성 헤드라인) 우선, 없으면 titles[0] fallback.
    rank가 있으면 원형 순위 배지 표시.
    """
    titles = item.get("titles", [])
    fallback_title = titles[0] if titles else "(제목 없음)"
    display_title = item.get("generated_title") or fallback_title
    rank_html = ""
    if rank is not None:
        rank_html = (f'<span style="display:inline-block; min-width:20px; height:20px; line-height:20px; '
                     f'text-align:center; border-radius:50%; background:{accent}; color:#fff; '
                     f'font-size:11px; font-weight:bold; margin-right:6px;">{rank}</span>')

    extra_html = ""
    if len(titles) > 1:
        extra_html = (f'<p style="margin:2px 0 4px 0; font-size:11px; color:#aaa;">'
                      f'(총 {len(titles)}건 기사를 종합)</p>')

    cross_html = ""
    if item.get("cross_axis_partner"):
        cross_html = (f'<p style="margin:2px 0 4px 0; font-size:12px; color:{accent};">'
                      f'🔗 반대 축에서도 다뤄짐: {_escape(item["cross_axis_partner"])}</p>')

    if item.get("summary"):
        body_html = f'<p style="margin:4px 0 8px 0; color:#333; font-size:13px; line-height:1.5;">{_escape(item["summary"])}</p>'
    else:
        reason = item.get("summary_skipped_reason", "사유 불명")
        body_html = (f'<p style="margin:4px 0 8px 0; color:#999; font-size:12px; font-style:italic;">'
                     f'(요약 생략 - {_escape(reason)})</p>')

    urls = item.get("urls", [])
    shown = urls[:3]
    more = f" 외 {len(urls) - 3}건" if len(urls) > 3 else ""
    links_html = ""
    if shown:
        link_tags = ", ".join(f'<a href="{_escape(u)}" style="color:{accent}; text-decoration:none;">원문</a>' for u in shown)
        links_html = f'<p style="margin:0; font-size:12px; color:#888;">원문 링크: {link_tags}{more}</p>'

    return f"""
    <div style="margin-bottom:12px; padding:12px 14px; border:1px solid #eee; border-radius:8px; background:#fff;">
      <p style="margin:0; font-weight:bold; font-size:14px; color:#111;">{rank_html}{_escape(display_title)}</p>
      {extra_html}
      {cross_html}
      {body_html}
      {links_html}
    </div>
    """


def _format_section_html(title: str, items: list[dict], accent: str = ACCENT_DOMESTIC) -> str:
    """축 색상 컬러바가 붙은 섹션 제목 + 순위 매긴 이슈 카드들."""
    title_html = (f'<h3 style="font-size:16px; color:#222; margin:20px 0 10px 0; '
                  f'padding-left:10px; border-left:4px solid {accent};">{_escape(title)}</h3>')
    if not items:
        return f'{title_html}<p style="color:#999; font-size:13px;">(이번 주 이슈 없음)</p>'
    body = "".join(_format_issue_html(item, rank=i, accent=accent) for i, item in enumerate(items, start=1))
    return f'{title_html}{body}'


def _format_category_html(label: str, by_category: dict[str, list[dict]],
                           accent: str = ACCENT_DOMESTIC, accent_tint: str = ACCENT_DOMESTIC_TINT) -> str:
    """카테고리별 Top N. 카테고리마다 알약 태그 + 그 안에서 1부터 순위 매김."""
    if not by_category:
        return ""
    blocks = []
    for category, items in by_category.items():
        tag_html = (f'<span style="display:inline-block; padding:4px 14px; border-radius:14px; '
                    f'background:{accent_tint}; color:{accent}; font-size:15px; font-weight:bold; '
                    f'margin:14px 0 8px 0;">{_escape(category)}</span>')
        blocks.append(f'<div>{tag_html}</div>')
        blocks.append("".join(_format_issue_html(item, rank=i, accent=accent) for i, item in enumerate(items, start=1)))
    title_html = (f'<h3 style="font-size:16px; color:#222; margin:24px 0 8px 0; '
                  f'padding-left:10px; border-left:4px solid {accent};">{_escape(label)}</h3>')
    return f'{title_html}{"".join(blocks)}'


def _format_category_comparison_axis_html(axis_data: dict[str, dict] | None, accent: str) -> str:
    """카테고리별 지난주 대비 증감 표(축 하나 분량). 2단 레이아웃에서 좌우 배치용."""
    if not axis_data:
        return '<p style="font-size:13px; color:#999; margin:4px 0;">(비교할 지난주 데이터 없음)</p>'
    rows = []
    for category, values in axis_data.items():
        delta = values["delta"]
        sign = "+" if delta >= 0 else ""
        color = "#1a7f37" if delta > 0 else ("#c0392b" if delta < 0 else "#888")
        rows.append(
            f'<tr>'
            f'<td style="padding:6px 8px; border-bottom:1px solid #f0f0f0; font-size:13px;">{_escape(category)}</td>'
            f'<td style="padding:6px 8px; border-bottom:1px solid #f0f0f0; font-size:13px; text-align:right;">{values["this_week"]}건</td>'
            f'<td style="padding:6px 8px; border-bottom:1px solid #f0f0f0; font-size:13px; text-align:right; color:#999;">{values["last_week"]}건</td>'
            f'<td style="padding:6px 8px; border-bottom:1px solid #f0f0f0; font-size:13px; text-align:right; '
            f'color:{color}; font-weight:bold;">{sign}{delta}</td>'
            f'</tr>'
        )
    return (
        f'<table style="width:100%; border-collapse:collapse;">'
        f'<tr style="background:#fafafa;">'
        f'<th style="text-align:left; padding:6px 8px; font-size:11px; color:#888;">카테고리</th>'
        f'<th style="text-align:right; padding:6px 8px; font-size:11px; color:#888;">이번 주</th>'
        f'<th style="text-align:right; padding:6px 8px; font-size:11px; color:#888;">지난주</th>'
        f'<th style="text-align:right; padding:6px 8px; font-size:11px; color:#888;">증감</th>'
        f'</tr>{"".join(rows)}</table>'
    )


def _two_column_table(left_html: str, right_html: str) -> str:
    """국내/해외 두 블록을 좌우로 배치. flexbox/grid 대신 table 사용(구형 Outlook 호환)."""
    return f"""
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="width:100%; border-collapse:collapse; table-layout:fixed;">
      <tr>
        <td width="50%" valign="top" style="padding-right:14px;">{left_html}</td>
        <td width="50%" valign="top" style="padding-left:14px;">{right_html}</td>
      </tr>
    </table>
    """


def render_email_html(week_label: str, domestic_summarized: list[dict], international_summarized: list[dict],
                       domestic_by_category: dict[str, list[dict]],
                       international_by_category: dict[str, list[dict]],
                       failed_sources: list[str],
                       category_comparison: dict[str, dict[str, dict]] | None = None) -> str:
    """
    scored.json 데이터로 이메일 본문 HTML 생성. 폭 1000px, 옅은 회색 배경 위
    흰색 콘텐츠 카드, 국내/해외 좌우 2단 레이아웃.
    """
    header_html = f"""
    <div style="background:{HEADER_BG}; padding:26px 32px; border-radius:10px 10px 0 0;">
      <p style="margin:0; font-size:11px; letter-spacing:2px; color:#9fc0ff; font-weight:bold;">NEWSLETTER</p>
      <h1 style="margin:6px 0 0 0; font-size:22px; color:#fff; font-weight:bold;">사료·축산업 뉴스 큐레이션</h1>
      <p style="margin:5px 0 0 0; font-size:13px; color:#c9dcff;">{_escape(_format_week_label_kr(week_label))}</p>
    </div>
    """

    section_header = lambda text: f'<h2 style="font-size:18px; color:#111; margin:26px 0 12px 0;">{_escape(text)}</h2>'

    parts = [
        '<div style="font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', Arial, sans-serif; '
        'background:#f2f4f7; padding:24px 0;">',
        '<div style="max-width:1000px; margin:0 auto; background:#fff; border-radius:10px; '
        'overflow:hidden; border:1px solid #e5e5e5;">',
        header_html,
        '<div style="padding:24px 32px; color:#333;">',
    ]

    if category_comparison:
        parts.append(section_header("카테고리별 지난주 대비 증감"))
        parts.append(_two_column_table(
            _format_category_comparison_axis_html(category_comparison.get("국내"), ACCENT_DOMESTIC),
            _format_category_comparison_axis_html(category_comparison.get("해외"), ACCENT_INTL),
        ))

    parts.append(section_header("주간 Top 이슈"))
    parts.append(_two_column_table(
        _format_section_html("국내", domestic_summarized, accent=ACCENT_DOMESTIC),
        _format_section_html("해외", international_summarized, accent=ACCENT_INTL),
    ))

    parts.append(section_header("카테고리별 Top N"))
    parts.append(_two_column_table(
        _format_category_html("국내", domestic_by_category, accent=ACCENT_DOMESTIC, accent_tint=ACCENT_DOMESTIC_TINT),
        _format_category_html("해외", international_by_category, accent=ACCENT_INTL, accent_tint=ACCENT_INTL_TINT),
    ))

    if failed_sources:
        parts.append(
            f'<p style="margin-top:24px; font-size:12px; color:#c0392b;">'
            f'참고 - 이번 실행에서 실패한 소스: {_escape(", ".join(failed_sources))}</p>'
        )

    parts.append(
        '<p style="margin-top:28px; padding-top:16px; border-top:1px solid #eee; '
        'font-size:11px; color:#aaa; text-align:center;">'
        '이 메일은 사료·축산업 뉴스 큐레이션 시스템이 매주 자동으로 발송합니다.<br>'
        'AI가 자동으로 생성한 요약·헤드라인이 포함되어 있어 실제 내용과 다를 수 있습니다. '
        '정확한 내용은 원문 링크를 확인해주세요.</p>'
    )

    parts.append('</div>')
    parts.append('</div>')
    parts.append('</div>')
    return "".join(parts)


def render_email_pdf(html_content: str) -> bytes | None:
    """
    render_email_html()이 만든 HTML을 PDF로 변환. Playwright/Chromium 사용 -
    WATT_collector.py가 이미 같은 브라우저를 쓰고 있어서(워크플로에도 이미
    설치돼 있음) requirements.txt/워크플로에 새 의존성을 추가할 필요가 없음.

    링크(원문 등)는 href 그대로 살아서 PDF에서도 클릭 가능(하이퍼링크
    주석으로 보존됨 - 화면에 보이는 글자가 "원문"으로 짧아도 무관).

    실패해도 예외를 던지지 않고 None만 반환 - PDF 변환 실패로 이메일 발송
    자체가 막히면 안 됨(HTML 본문 발송이 우선, PDF는 부가 기능).
    """
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.set_content(html_content, wait_until="networkidle")
                pdf_bytes = page.pdf(
                    format="A4", print_background=True,
                    margin={"top": "12mm", "bottom": "12mm", "left": "10mm", "right": "10mm"},
                )
            finally:
                browser.close()
        return pdf_bytes
    except Exception as e:
        print(f"[deploy] 🟡 주의 [DP-05] - PDF 변환 실패 - 이메일은 HTML 본문만 발송(첨부 없이): "
              f"{type(e).__name__} - {e!r}")
        return None


def send_email(html_content: str, subject: str, recipients: list[str],
               smtp_user: str, smtp_app_password: str,
               pdf_attachment: bytes | None = None, pdf_filename: str = "weekly.pdf") -> bool:
    """
    Gmail SMTP(587, STARTTLS)로 발송. 성공 True, 실패해도 예외 없이 False.
    pdf_attachment를 넘기면 HTML 본문 + PDF 첨부(mixed) 형태로 같이 보냄 -
    바깥은 mixed(첨부 지원), 그 안에 본문용 alternative를 한 겹 더 둠(기존
    HTML 전용 메일 클라이언트 호환은 그대로 유지하면서 첨부만 추가).
    """
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = ", ".join(recipients)

    body = MIMEMultipart("alternative")
    body.attach(MIMEText(html_content, "html", "utf-8"))
    msg.attach(body)

    if pdf_attachment:
        pdf_part = MIMEApplication(pdf_attachment, _subtype="pdf")
        pdf_part.add_header("Content-Disposition", "attachment", filename=pdf_filename)
        msg.attach(pdf_part)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.starttls()
            server.login(smtp_user, smtp_app_password)
            server.sendmail(smtp_user, recipients, msg.as_string())
    except (smtplib.SMTPException, OSError) as e:
        print(f"[deploy] 🔴 조치필요 [DP-01] - 이메일 발송 실패: {type(e).__name__} - {e!r}")
        return False

    attach_note = f", PDF 첨부 포함({len(pdf_attachment):,} bytes)" if pdf_attachment else " (PDF 첨부 없음)"
    print(f"[deploy] 이메일 발송 완료 -> {', '.join(recipients)}{attach_note}")
    return True


def send_weekly_email(week_label: str, domestic_summarized: list[dict], international_summarized: list[dict],
                       domestic_by_category: dict[str, list[dict]],
                       international_by_category: dict[str, list[dict]],
                       failed_sources: list[str],
                       category_comparison: dict[str, dict[str, dict]] | None = None) -> bool:
    """main.py 호출 진입점. 인증정보 없으면 안전하게 생략."""
    smtp_user = os.environ.get("SMTP_USER")
    smtp_app_password = os.environ.get("SMTP_APP_PASSWORD")
    recipients_raw = os.environ.get("EMAIL_RECIPIENTS")

    if not smtp_user or not smtp_app_password:
        print("[deploy] 🔴 조치필요 [DP-02] - SMTP_USER/SMTP_APP_PASSWORD 없음 - 이메일 발송 생략")
        return False
    if not recipients_raw:
        print("[deploy] 🔴 조치필요 [DP-03] - EMAIL_RECIPIENTS 없음 - 이메일 발송 생략")
        return False

    recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]
    if not recipients:
        print("[deploy] 🔴 조치필요 [DP-04] - EMAIL_RECIPIENTS가 비어있음(콤마만 있거나 공백) - 이메일 발송 생략")
        return False

    html_content = render_email_html(week_label, domestic_summarized, international_summarized,
                                      domestic_by_category, international_by_category, failed_sources,
                                      category_comparison)
    subject = f"[사료·축산뉴스] {_format_week_label_kr(week_label)} 주간 큐레이션"
    pdf_bytes = render_email_pdf(html_content)
    pdf_filename = f"feed_livestock_news_{week_label}.pdf"  # 파일명은 ASCII로(한글 파일명 인코딩 이슈 회피)
    return send_email(html_content, subject, recipients, smtp_user, smtp_app_password,
                       pdf_attachment=pdf_bytes, pdf_filename=pdf_filename)