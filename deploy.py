"""
deploy.py - 6단계 배포 레이어.

Gmail SMTP로 이번 주 큐레이션 결과를 HTML 이메일로 발송한다. main.py의
[6] 배포 단계에서 이 모듈의 send_weekly_email()을 호출한다.

** Gmail SMTP를 선택한 이유 **
SendGrid/Mailgun 같은 무료 트랜잭션 이메일 API는 도메인 인증(SPF/DKIM)
없이 기본 발신 주소로 보내면 스팸함으로 분류될 확률이 높음 - 이제 막 만든
발신자라 평판이 전혀 없는 상태로 시작하기 때문. Gmail SMTP는 실제 Gmail
계정에서 Gmail 서버를 통해 보내는 거라 SPF/DKIM이 자동으로 유효하고
발신처 신뢰도도 이미 높아 이 문제가 훨씬 덜함 - 특히 지금처럼 수신자가
소수일 때는 도메인 설정 같은 번거로운 과정 없이 바로 쓸 수 있는 쪽이 낫다.

** 인증정보 관리 **
GitHub Secrets(민감정보라 Variables 아님)에서 읽는다:
  SMTP_USER          발신용 Gmail 주소
  SMTP_APP_PASSWORD  Gmail 앱 비밀번호(일반 로그인 비밀번호 아님)
  EMAIL_RECIPIENTS   수신자 이메일, 콤마로 구분

** 콘텐츠 구성 **
storage.py가 이미 만들어둔 domestic_summarized/international_summarized/
domestic_by_category/international_by_category(scored.json과 동일한
데이터)를 그대로 받아서 summary.md와 같은 구조(요약 유무와 무관하게 원문
링크는 항상 같이 노출)로 HTML을 렌더링한다. 이메일 클라이언트는 외부
스타일시트를 지원 안 하는 경우가 많아 인라인 스타일만 사용.

** 안전 실패 원칙 **
storage.py와 같은 방향 - 이메일 발송이 실패해도(SMTP 인증 오류, 네트워크
문제 등) 예외를 그대로 던지지 않고 로그만 남기고 조용히 실패한다. 이 시점엔
이미 수집/스코어링/요약/저장이 다 끝난 뒤라, 배포 하나 실패했다고 전체
실행을 죽이면 안 된다는 판단.
"""

import html
import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

# --- 이메일 디자인 토큰 (2026-07-30, 담당자 요청으로 시각적 개편) ---
#
# 참고용으로 받은 업계 뉴스레터 시안(GBT_DOC)을 보고, "본문을 그대로
# 재현하는 건 저작권 문제로 안 되지만, 레이아웃/색상/타이포그래피 같은
# 포맷은 참고해도 된다"는 원칙으로 반영함. 로고 이미지는 없어서(요청도
# 없었음) 색상 배너 + 타이틀 텍스트로 "정식 발행물" 느낌만 냄.
#
# 국내/해외를 색으로 구분 - 파랑/보라 조합. 초록은 아래 증감 표에서 이미
# "증가"를 뜻하는 색으로 쓰고 있어서 축 구분색으로 재사용하면 헷갈릴 수
# 있어 피함.
ACCENT_DOMESTIC = "#1a73e8"   # 국내 - 파랑
ACCENT_DOMESTIC_TINT = "#e8f0fe"  # 파랑의 옅은 배경톤 (태그/배지 배경용)
ACCENT_INTL = "#7c3aed"       # 해외 - 보라
ACCENT_INTL_TINT = "#f3ebfd"  # 보라의 옅은 배경톤

HEADER_BG = "#0f2f5c"  # 헤더 배너 배경(짙은 남색) - 본문 색상들과 안 부딪히게 별도 계열


def _escape(value) -> str:
    return html.escape(str(value))


def _format_week_label_kr(week_label: str) -> str:
    """
    "2026-31"(ISO 연도-주차, main.py/storage.py가 저장 디렉토리명으로 쓰는
    형식) 같은 라벨을 이메일에서 보여줄 "2026년 7월 4주차" 형태로 바꾼다
    (2026-07-30, 담당자 요청 - "N주차"가 사람이 보기에 훨씬 직관적).

    ISO 주차는 달력 월과 딱 맞아떨어지지 않아서(한 ISO 주가 두 달에 걸칠
    수 있음), 그 주의 월요일이 속한 달/일을 기준으로 "몇 번째 주"인지
    계산한다 - "월 1~7일=1주차, 8~14일=2주차..." 식으로 단순하게 나눠서
    사람들이 보통 쓰는 감각과 맞춘다. 형식이 예상과 다르면(수동 실행 등
    으로 다른 문자열이 들어온 경우) 파싱을 포기하고 원본 그대로 반환 -
    이메일 발송 전체를 막을 정도의 실패는 아니라고 판단.
    """
    try:
        monday = datetime.strptime(f"{week_label}-1", "%G-%V-%u")
    except ValueError:
        return week_label
    week_of_month = ((monday.day - 1) // 7) + 1
    return f"{monday.year}년 {monday.month}월 {week_of_month}주차"


def _format_issue_html(item: dict, rank: int | None = None, accent: str = ACCENT_DOMESTIC) -> str:
    """
    이슈 하나 분량의 HTML 블록. summary.md의 _format_issue_section과 같은 정보를 담는다.

    rank: 이 이슈가 속한 목록(전체 Top N 또는 카테고리별 Top N) 안에서 몇 번째인지.
    None이면(순위 개념이 없는 호출부) 배지를 안 붙인다 - 호출부(_format_section_html/
    _format_category_html)가 enumerate로 1부터 매겨서 넘겨준다. 지금까지 이메일에
    순위 숫자가 아예 안 붙어있었던 걸 담당자가 지적해서 추가함(2026-07-28).

    accent: 국내/해외 축 색상(ACCENT_DOMESTIC/ACCENT_INTL) - 순위 배지, 반대 축
    링크, 원문 링크 색에 일괄 적용해서 "지금 국내를 보는 중인지 해외를 보는
    중인지"가 스크롤하다가도 색만으로 구분되게 함(2026-07-30).

    ** 순위 배지를 원형 배지로 바꾼 이유(2026-07-30) **: 기존엔 그냥
    "1." 파란 텍스트라 눈에 잘 안 띄었음. border-radius:50%는 이메일
    클라이언트마다 지원이 갈리는데(구형 Outlook 데스크톱은 무시하고
    사각형으로 표시 - 큰 문제는 아니고 그냥 순위 숫자가 사각 배지로
    보일 뿐), 발신 계정이 Gmail이고 주 수신 환경도 Gmail(웹/앱)이라
    실사용 환경에서는 문제없이 원형으로 보임.

    ** 점수/언급 건수 라인 제거(2026-07-30) **: 담당자 판단으로, 수신자가
    실제로 참고할 정보가 아니라고 보고 뺌 - 데이터 자체는 item에 여전히
    남아있어서(summary.md 등 다른 출력물에는 영향 없음) 필요해지면 다시
    붙이는 것도 어렵지 않음.

    ** 대표 제목을 LLM이 새로 쓴 헤드라인으로 교체(2026-07-30) **:
    llm_summarizer.summarize_issue가 요약과 함께 생성해주는
    item["generated_title"]이 있으면 그걸 대표 제목으로 쓰고, titles[0]
    (수집 원본 제목)은 화면에 아예 안 보여준다 - 해외 기사도 이제
    "번역"이 아니라 처음부터 한국어 헤드라인으로 나와서 영어 원문이
    노출될 일이 없다(담당자 요청 - "영어는 원문 남기지 말자"). 다만
    생성이 실패/생략된 경우(예: LLM 호출 자체가 안 됨)에는 화면에 뭔가는
    떠야 하므로 titles[0]로 fallback한다 - 이 경우 국내는 원본 한국어
    제목이라 문제없지만, 해외는 드물게 영어 원문이 보일 수 있다는 걸
    감안할 것(요약 자체가 실패한 상황이라 애초에 "요약 생략" 문구도 같이
    뜨니, 아예 아무 제목도 없는 것보다는 낫다고 판단).
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

    # 카드형 박스 - 기존엔 밑줄(border-bottom)만 있었는데, 옅은 테두리 +
    # 둥근 모서리로 바꿔서 기사 하나하나가 독립된 카드처럼 보이게 함
    # (참고한 뉴스레터 시안의 "여백/구획이 확실한" 느낌을 레이아웃만 차용).
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
    # 섹션 제목 왼쪽에 축 색상 컬러바를 둬서(border-left) 국내/해외를 스크롤
    # 중에도 색으로 바로 구분할 수 있게 함(2026-07-30).
    title_html = (f'<h3 style="font-size:16px; color:#222; margin:20px 0 10px 0; '
                  f'padding-left:10px; border-left:4px solid {accent};">{_escape(title)}</h3>')
    if not items:
        return f'{title_html}<p style="color:#999; font-size:13px;">(이번 주 이슈 없음)</p>'
    # items는 scorer.score_and_rank()가 이미 점수순으로 정렬해둔 상태 - 그 순서
    # 그대로 1부터 번호만 매기면 됨(2026-07-28, 순위 번호 표시 추가).
    body = "".join(_format_issue_html(item, rank=i, accent=accent) for i, item in enumerate(items, start=1))
    return f'{title_html}{body}'


def _format_category_html(label: str, by_category: dict[str, list[dict]],
                           accent: str = ACCENT_DOMESTIC, accent_tint: str = ACCENT_DOMESTIC_TINT) -> str:
    if not by_category:
        return ""
    blocks = []
    for category, items in by_category.items():
        # 대괄호 텍스트("[질병명]") 대신 알약(pill) 모양 태그로 - 참고한
        # 뉴스레터 시안의 태그 느낌을 레이아웃만 차용(2026-07-30).
        # 폰트 크기 12px -> 15px로 확대(2026-07-30, 담당자 요청 - 카테고리명이
        # 더 잘 보이게).
        tag_html = (f'<span style="display:inline-block; padding:4px 14px; border-radius:14px; '
                    f'background:{accent_tint}; color:{accent}; font-size:15px; font-weight:bold; '
                    f'margin:14px 0 8px 0;">{_escape(category)}</span>')
        blocks.append(f'<div>{tag_html}</div>')
        # 카테고리별로 별도의 Top N이므로, 전체 순위가 아니라 그 카테고리 안에서
        # 1부터 다시 매김(2026-07-28, 순위 번호 표시 추가).
        blocks.append("".join(_format_issue_html(item, rank=i, accent=accent) for i, item in enumerate(items, start=1)))
    title_html = (f'<h3 style="font-size:16px; color:#222; margin:24px 0 8px 0; '
                  f'padding-left:10px; border-left:4px solid {accent};">{_escape(label)}</h3>')
    return f'{title_html}{"".join(blocks)}'


def _format_category_comparison_axis_html(axis_data: dict[str, dict] | None, accent: str) -> str:
    """
    카테고리별 지난주 대비 증감 표 - 축(국내/해외) 하나 분량만 만든다.
    2026-07-30 2단 레이아웃 개편으로 국내/해외를 나란히 배치하게 되면서,
    기존에 한 축씩 이어붙이던 _format_category_comparison_html을 축
    단위로 쪼갬(render_email_html이 _two_column_table로 좌우 배치).
    """
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
    """
    두 블록(국내/해외)을 좌우로 나란히 배치하는 테이블 래퍼(2026-07-30,
    데스크톱 와이드 2단 레이아웃 개편). flexbox/grid 대신 <table>을 쓰는
    이유: 구형 Outlook 데스크톱 등 일부 메일 클라이언트가 flexbox/grid를
    지원 안 해서 레이아웃이 깨질 수 있는데, <table> 기반은 이메일
    클라이언트 전반에서 안정적으로 지원됨 - 담당자가 컴퓨터로만 본다고
    확인해서 폭을 넓히는 김에 좌우 2단으로 감.
    """
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
    scored.json과 동일한 데이터를 받아 이메일 본문(HTML 문자열)을 만든다.
    summary.md(storage.py)와 콘텐츠 구성은 같고 렌더링 형식만 HTML로 다르다.

    category_comparison(지난주 대비 증감)이 있으면 제목 바로 아래에 추가 -
    "이번 주 큰 흐름"을 이슈 목록보다 먼저 보여주는 구성(summary.md와
    동일한 배치 원칙).

    ** 전체 레이아웃 개편(2026-07-30) **: 담당자가 참고용으로 공유한 업계
    뉴스레터 시안(GBT_DOC)을 보고 "본문 재현은 저작권 때문에 안 되지만,
    레이아웃/색상 같은 포맷은 참고해도 된다"는 원칙으로 반영. 로고 없이
    색상 배너 + 타이틀 텍스트로 "정식 발행물" 느낌만 내고, 바깥은 옅은
    회색 배경 위에 흰색 콘텐츠 카드를 얹는 구조로 바꿈(이메일 자체가
    하나의 카드처럼 보이게). 콘텐츠는 100% 그대로, 표현 방식만 바뀜.

    ** 2단 데스크톱 와이드 레이아웃으로 재개편(같은 날 후속) **: 컴퓨터로만
    보신다는 확인을 받아서, 폭을 640px -> 1000px로 넓히고 "주간 Top N"과
    "카테고리별 Top N"을 국내/해외가 위아래로 쌓이는 대신 좌우로 나란히
    보이게 함(_two_column_table). 기존엔 국내 전체를 다 본 다음 한참
    스크롤해야 해외가 나왔는데, 이제 같은 화면에서 국내/해외를 바로 비교
    가능 - "지금 너무 세로로만 길다"는 지적 반영. 지난주 대비 증감 표도
    같은 방식으로 좌우 배치.
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
        # 바깥 배경(옅은 회색) - 흰색 콘텐츠 카드가 그 위에 얹힌 것처럼 보이게 함
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

    # 푸터 - 기존엔 아예 없었음. "자동 발송" 안내 정도는 정식 뉴스레터에
    # 보통 있는 요소라 추가(2026-07-30). 발송 시각 등은 아직 안 넣음 -
    # 필요해지면 추가 가능.
    #
    # AI 면책 문구도 같은 날 추가 - 요약/헤드라인이 전부 LLM 생성물이라
    # 틀릴 수 있다는 걸 매번 명시해서, 수신자가 요약만 보고 그대로
    # 믿기보다 원문 링크로 확인하는 습관을 갖게 하려는 목적(담당자 요청).
    parts.append(
        '<p style="margin-top:28px; padding-top:16px; border-top:1px solid #eee; '
        'font-size:11px; color:#aaa; text-align:center;">'
        '이 메일은 사료·축산업 뉴스 큐레이션 시스템이 매주 자동으로 발송합니다.<br>'
        'AI가 자동으로 생성한 요약·헤드라인이 포함되어 있어 실제 내용과 다를 수 있습니다. '
        '정확한 내용은 원문 링크를 확인해주세요.</p>'
    )

    parts.append('</div>')  # 콘텐츠 padding div 닫기
    parts.append('</div>')  # 흰색 카드 div 닫기
    parts.append('</div>')  # 바깥 배경 div 닫기
    return "".join(parts)


def send_email(html_content: str, subject: str, recipients: list[str],
               smtp_user: str, smtp_app_password: str) -> bool:
    """
    Gmail SMTP(587, STARTTLS)로 HTML 이메일을 보낸다. 성공하면 True, 실패하면
    (예외를 던지지 않고) False를 반환한다 - 호출부가 이 결과로 로그만 남기고
    계속 진행할 수 있게.
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.starttls()
            server.login(smtp_user, smtp_app_password)
            server.sendmail(smtp_user, recipients, msg.as_string())
    except (smtplib.SMTPException, OSError) as e:
        print(f"[deploy] 🔴 조치필요 [DP-01] - 이메일 발송 실패: {type(e).__name__} - {e!r}")
        return False

    print(f"[deploy] 이메일 발송 완료 -> {', '.join(recipients)}")
    return True


def send_weekly_email(week_label: str, domestic_summarized: list[dict], international_summarized: list[dict],
                       domestic_by_category: dict[str, list[dict]],
                       international_by_category: dict[str, list[dict]],
                       failed_sources: list[str],
                       category_comparison: dict[str, dict[str, dict]] | None = None) -> bool:
    """
    main.py에서 부르는 단일 진입점. 환경변수(GitHub Secrets)에서 인증정보를
    읽고, 없으면 요약 모듈들과 같은 패턴으로 안전하게 생략한다.
    """
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
    subject = f"[사료·축산뉴스] {week_label} 주간 큐레이션"
    return send_email(html_content, subject, recipients, smtp_user, smtp_app_password)