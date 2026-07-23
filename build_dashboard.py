#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HR Monthly Session 대시보드 데이터 빌더
  노션 DB  →  (설명을 Gemini로 3줄 요약)  →  data.js

■ 사용법
  (1) 로컬 PC에서 실행 — 결과를 내 PC 폴더에도 저장
        python build_dashboard.py
        python build_dashboard.py --local        (LOCAL_DIR 에도 함께 저장)

  (2) GitHub Actions에서 실행 — docs/data.js 만 생성 (자동)

■ 필요 환경변수
  NOTION_TOKEN         노션 내부 통합 토큰  (ntn_... 또는 secret_...)
  NOTION_DATABASE_ID   대상 데이터베이스 ID (32자리)
  GEMINI_API_KEY       Google AI Studio API 키
  (선택) GEMINI_MODEL  기본 'gemini-2.5-flash'
  (선택) LOCAL_DIR     추가 저장 폴더 (미지정 시 아래 DEFAULT_LOCAL_DIR)
"""

import os
import sys
import json
import time
import argparse
import datetime as dt

import requests
from google import genai  # pip install google-genai

# ─────────────────────────────────────────────────────────────
# 0. 설정
# ─────────────────────────────────────────────────────────────

# 깃허브 Pages 용 (리포지토리 안, 항상 생성)
REPO_OUT = os.path.join("docs", "data.js")

# 내 PC 백업 저장 폴더 (--local 옵션 또는 LOCAL_DIR 환경변수로 사용)
DEFAULT_LOCAL_DIR = r"C:\Users\Administrator\Desktop\플랫폼인사팀 업무파일\98. AI\AI Monthly session\데이터"

# 노션 '속성 이름'이 다르면 여기만 바꾸면 됩니다
PROPS = {
    "name":     "작업 이름",     # title
    "desc":     "설명",          # rich_text
    "prio":     "우선순위",      # select : 높음 / 중간 / 낮음
    "bu":       "상태",          # status(또는 select) : 값이 BU명
    "progress": "진행 상태",     # select : 진행중 / 완료
    "types":    "작업 유형",     # multi_select
}

PRIO_ALLOWED = {"높음", "중간", "낮음"}
DONE_LABELS = {"완료", "종료", "done", "Done", "완료됨"}  # 이 중 하나면 '완료' 버킷

NOTION_VERSION = "2022-06-28"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")


# ─────────────────────────────────────────────────────────────
# 1. 노션에서 안건 읽기
# ─────────────────────────────────────────────────────────────
def notion_query_all(token: str, db_id: str) -> list:
    url = f"https://api.notion.com/v1/databases/{db_id}/query"
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    results, payload = [], {"page_size": 100}
    while True:
        r = requests.post(url, headers=headers, json=payload, timeout=30)
        if r.status_code == 401:
            sys.exit("[X] 401 인증 실패 — NOTION_TOKEN 을 확인하세요.")
        if r.status_code == 404:
            sys.exit("[X] 404 — DB ID가 틀렸거나, 데이터베이스에 통합(Connection)을 연결하지 않았습니다.")
        r.raise_for_status()
        data = r.json()
        results.extend(data.get("results", []))
        if data.get("has_more"):
            payload["start_cursor"] = data["next_cursor"]
        else:
            break
    return results


# ── 속성 추출 헬퍼 ────────────────────────────────────────────
def _plain(rich):
    return "".join(t.get("plain_text", "") for t in (rich or [])).strip()

def _title(prop):
    return _plain(prop.get("title")) if prop else ""

def _rich(prop):
    return _plain(prop.get("rich_text")) if prop else ""

def _select(prop):
    if not prop:
        return ""
    node = prop.get("select") or prop.get("status")   # select / status 모두 지원
    return (node or {}).get("name", "") if node else ""

def _multi(prop):
    return [t.get("name", "") for t in (prop.get("multi_select") or [])] if prop else []


def map_page(page: dict):
    props = page.get("properties", {})
    def P(key):
        return props.get(PROPS[key])

    name = _title(P("name"))
    if not name:
        return None  # 제목 없는 행은 건너뜀

    prio = _select(P("prio")) or "중간"
    if prio not in PRIO_ALLOWED:
        prio = "중간"

    status = "완료" if _select(P("progress")) in DONE_LABELS else "진행중"

    return {
        "name":   name,
        "bu":     _select(P("bu")) or "전사공통",
        "prio":   prio,
        "status": status,
        "types":  _multi(P("types")),
        "desc":   _rich(P("desc")),
        "url":    page.get("url", "#"),
        # ai(3줄)는 Gemini 단계에서 채움
    }


# ─────────────────────────────────────────────────────────────
# 2. Gemini 3줄 요약
# ─────────────────────────────────────────────────────────────
SUMMARY_PROMPT = """다음은 사내 HR 안건의 '설명'입니다.
이 내용을 임원이 빠르게 파악할 수 있도록 한국어 3줄로 요약하세요.

규칙:
- 정확히 3줄. 각 줄은 한 문장, 25자 내외.
- 1줄=핵심 현황, 2줄=주요 이슈나 결과, 3줄은 반드시 "일정: ..." 형식으로 다음 할 일.
- 불릿(-,•)이나 번호를 붙이지 말 것. 줄바꿈으로만 구분.

[설명]
{desc}
"""

def summarize(client, desc: str) -> list:
    desc = (desc or "").strip()
    if not desc:
        return ["설명이 아직 작성되지 않았습니다.", "", "일정: 담당자 확인 필요."]
    try:
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=SUMMARY_PROMPT.format(desc=desc),
        )
        lines = [ln.strip(" -•\t") for ln in (resp.text or "").splitlines() if ln.strip()][:3]
        while len(lines) < 3:
            lines.append("")
        return lines
    except Exception as e:
        print(f"  ! Gemini 요약 실패, 원문 앞부분 사용: {e}", file=sys.stderr)
        head = desc.replace("\n", " ")
        return [head[:40], "", "일정: 상세 검토 필요."]


# ─────────────────────────────────────────────────────────────
# 3. data.js 쓰기
# ─────────────────────────────────────────────────────────────
def write_data_js(path: str, data: dict):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    body = ("// 이 파일은 build_dashboard.py 가 자동 생성합니다. 수동 편집 금지.\n"
            "window.DASHBOARD_DATA = "
            + json.dumps(data, ensure_ascii=False, indent=2)
            + ";\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    print(f"  저장 완료 → {path}")


def build(save_local: bool):
    token = os.environ["NOTION_TOKEN"]
    db_id = os.environ["NOTION_DATABASE_ID"]
    gkey  = os.environ["GEMINI_API_KEY"]

    print("· 노션 조회 중…")
    pages = notion_query_all(token, db_id)
    items = [m for m in (map_page(p) for p in pages) if m]
    print(f"  안건 {len(items)}건")

    print("· Gemini 요약 생성 중…")
    client = genai.Client(api_key=gkey)
    for i, it in enumerate(items, 1):
        it["ai"] = summarize(client, it["desc"])
        print(f"  [{i}/{len(items)}] {it['name']}")
        time.sleep(0.4)  # 레이트리밋 여유

    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=9)))  # KST
    data = {
        "generatedAt": now.strftime("%Y-%m-%d %H:%M KST"),
        "session": f"{now.year}년 {now.month}월",
        "items": items,
    }

    print("· 파일 쓰는 중…")
    write_data_js(REPO_OUT, data)          # 깃허브용 (항상)

    if save_local:                          # 내 PC 폴더 (선택)
        local_dir = os.environ.get("LOCAL_DIR", DEFAULT_LOCAL_DIR)
        try:
            write_data_js(os.path.join(local_dir, "data.js"), data)
            # 사람이 열어볼 수 있게 원본 JSON도 함께 백업
            os.makedirs(local_dir, exist_ok=True)
            snap = os.path.join(local_dir, f"data_{now.strftime('%Y%m%d')}.json")
            with open(snap, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print(f"  백업 완료 → {snap}")
        except OSError as e:
            print(f"  ! 로컬 저장 실패(경로 확인 필요): {e}", file=sys.stderr)

    print(f"· 전체 완료 ({len(items)}건, {data['generatedAt']})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", action="store_true",
                    help="내 PC 폴더(LOCAL_DIR)에도 data.js와 JSON 백업을 저장")
    args = ap.parse_args()
    build(save_local=args.local)
