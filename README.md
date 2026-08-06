# 사료·축산업 뉴스 큐레이션 시스템

국내(네이버)·해외(GDELT)·업계 전문지(WATT) 뉴스를 매주 자동으로 수집해, 같은 사건을 묶고, 관련 없는 기사를 걸러내고, LLM으로 요약한 뒤 이메일(HTML + PDF 첨부)로 발송하는 파이프라인입니다.

자동 실행: 매주 월요일 00:01(KST). 상세 알고리즘 설명은 [`알고리즘_요약본.md`](./알고리즘_요약본.md) 참고.

## 파이프라인 개요

```
[1] 수집(WATT/네이버/GDELT)
        │  ── GitHub Actions job1 → job2 사이 artifact로 전달 ──
        ▼
[2] 정규화(중복 제거·키워드 태깅)
[3] 임베딩 모델 로드 → [4] 이슈 그룹핑(BGE-M3 + LLM 보조)
[5] 관련성 필터 → [6] 카테고리 재분류   (그룹 대표 기사 1건만 LLM 판단)
[7] 스코어링(Top N + 4차 사후 재검토) → [8] 카테고리 집계
[9] LLM 요약 → [10] 카테고리별 요약
[11] 저장(data/) → [12] 배포(이메일 + PDF)
```

## 실행 구조

`run-pipline.yml`이 GitHub Actions job 2개로 나눠서 돕니다(GitHub 호스팅 러너는 job 1개당 최대 6시간이라, 수집이 오래 걸려도 처리+배포가 영향받지 않도록 분리).

| job | 담당 | 진입점 |
|---|---|---|
| `collect` | [1] 수집만 | `python -u main.py collect` |
| `process` | [2]~[12] 전부 | `python -u main.py process` |

로컬에서 전체를 한 번에 돌려보고 싶으면 인자 없이 실행합니다: `python -u main.py`

## 설정 (GitHub Secrets / Variables)

리포 **Settings → Secrets and variables → Actions**에 등록합니다. Secrets는 값이 로그에 자동 마스킹되는 민감정보용, Variables는 평문 설정값용입니다.

### Secrets

| 이름 | 용도 |
|---|---|
| `NAVER_CLIENT_ID` / `NAVER_CLIENT_SECRET` | 네이버 뉴스 검색 API |
| `OPENROUTER_API_KEY` | LLM 호출(그룹핑 보조·관련성 필터·요약) |
| `SMTP_USER` / `SMTP_APP_PASSWORD` | Gmail 발송 계정(앱 비밀번호 필요) |
| `EMAIL_RECIPIENTS` | 수신자 이메일(콤마 구분, 개인정보라 Variable 아닌 Secret) |

### Variables (전부 선택, 비우면 코드 기본값 사용)

| 이름 | 기본값 | 용도 |
|---|---|---|
| `KEYWORD_SHEET_CSV_URL` | (없음 → 하드코딩 키워드) | 구글 시트 키워드 목록 CSV 게시 URL |
| `LLM_PROVIDER` | `openrouter` | LLM 프로바이더 |
| `OPENROUTER_MODEL` / `_MODEL_2` / `_MODEL_3` | `openrouter/free` | **판단형**(그룹핑·관련성 필터·재분류) 모델 재시도 체인 |
| `OPENROUTER_MODEL_SUMMARY` / `_SUMMARY_2` / `_SUMMARY_3` | `openai/gpt-oss-20b:free` | **생성형**(요약) 모델 재시도 체인 - 판단형과 별개 |
| `TOP_N` | `3` | 국내/해외 주간 Top N 개수 |
| `CATEGORY_TOP_N` | `1` | 카테고리별 Top N 개수 |
| `SIMILARITY_DEBUG_CSV` | 꺼짐 | 이슈 그룹핑 임계값 튜닝용 유사도 CSV 저장 스위치(`1`로 켬) |

## 파일 구성

| 파일 | 역할 |
|---|---|
| `main.py` | 오케스트레이션(12단계 실행 순서, 시간 예산 관리) |
| `naver_collector.py` / `gdelt_collector.py` / `WATT_collector.py` | 소스별 수집 |
| `keyword_source.py` / `keyword_tagger.py` | 키워드 목록 로드 / 카테고리 키워드 매칭 |
| `issue_grouper.py` | 이슈 그룹핑(1~4차) |
| `relevance_filter.py` | 관련성 필터 · 카테고리 재분류 |
| `scorer.py` | 점수 계산 · Top N 랭킹 |
| `category_aggregator.py` | 카테고리 전체 집계 · 주간 추이 |
| `llm_summarizer.py` | 이슈별 LLM 요약 생성 |
| `storage.py` | `data/YYYY-WW/`에 결과 저장 |
| `deploy.py` | 이메일(HTML+PDF) 렌더링·발송 |
| `run-pipline.yml` | GitHub Actions 워크플로 |

## 로그 읽는 법

모든 로그는 `[모듈명] 등급 [코드] - 내용` 형식입니다.

- 🟡 **주의**: 문제가 있었지만 안전하게 넘어감(파이프라인 계속 진행)
- 🔴 **조치필요**: 사람이 확인해야 하는 실패. 실행이 끝나면 이메일 맨 아래에도 발생한 코드만 작게 표시됩니다(PDF에는 안 나옴)

코드 접두사: `MN`(main) · `GD`(gdelt) · `WT`(WATT) · `NV`(naver) · `KS`(keyword_source) · `IG`(issue_grouper) · `RF`(relevance_filter) · `LS`(llm_summarizer) · `ST`(storage) · `CA`(category_aggregator) · `DP`(deploy)