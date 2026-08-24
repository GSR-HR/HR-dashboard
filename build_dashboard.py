#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HR Monthly Session 대시보드 데이터 빌더
  노션 DB(속성 + 페이지 본문)  →  Gemini 분석  →  data.js

■ 사용법
  python build_dashboard.py            # docs/data.js 생성
  python build_dashboard.py --local    # 내 PC 폴더에도 함께 저장

■ 환경변수
  NOTION_TOKEN / NOTION_DATABASE_ID / GEMINI_API_KEY
  (선택) NOTION_CALENDAR_DATABASE_ID     임원 주요 일정 DB
  (선택) NOTION_PEOPLECYCLE_DATABASE_ID  HR People Cycle DB
  (선택) NOTION_MONTHLY_DATABASE_ID      BU/SU별 먼슬리 일정 DB
  (선택) GEMINI_MODEL, LOCAL_DIR
  (선택) GEMINI_MIN_INTERVAL            Gemini 호출 최소 간격(초). 기본 6.5 (무료 티어 10 RPM 대응)
"""

import os
import re
import sys
import json
import time
import base64
import random
import argparse
import datetime as dt
from io import BytesIO

import requests
from google import genai

# 이미지 축소용(선택) — 없으면 원본 그대로 사용
try:
    from PIL import Image
    HAS_PIL = True
except Exception:
    HAS_PIL = False

# ─────────────────────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────────────────────
REPO_OUT = os.path.join("docs", "data.js")
DEFAULT_LOCAL_DIR = r"C:\Users\Administrator\Desktop\플랫폼인사팀 업무파일\98. AI\AI Monthly session\데이터"

# 노션 속성 이름 후보 — 앞에 있는 것부터 찾아 자동으로 연결합니다.
# 이름을 바꿔도 아래 목록에 있으면 그대로 동작합니다.
PROP_CANDIDATES = {
    "name":     ["작업 이름", "이름", "제목", "Name"],
    "prio":     ["우선순위", "중요도", "Priority"],
    "bu":       ["BU", "사업부", "소속BU", "상태", "Status"],
    "progress": ["진행 상태", "진행상태", "진행", "상태", "Status"],
    "types":    ["작업 유형", "유형", "분류", "Tags"],
    "desc":     ["설명", "내용", "비고", "Description"],
    "publish":  ["대시보드 게시", "대시보드게시", "대시보드 노출", "대시보드", "게시", "노출", "Publish", "공개"],
    "order":    ["순서", "정렬", "정렬순서", "노출순서", "Order", "번호", "No"],
}
PROPS = {}          # 첫 조회 때 실제 속성 이름으로 채워집니다


def _opt_value(node):
    """select / status / multi_select 속성의 값을 문자열로."""
    if not node:
        return ""
    t = node.get("type")
    if t in ("select", "status"):
        return _select(node)
    if t == "multi_select":
        return ",".join(_multi(node))
    return ""


def resolve_props(pages: list):
    """실제 노션 속성 이름을 찾아 PROPS에 연결한다."""
    props = pages[0].get("properties", {})
    used = set()
    # bu → progress 순서로 먼저 잡아야 '상태'가 올바른 쪽에 배정됩니다
    for key in ["name", "prio", "bu", "progress", "types", "desc", "publish", "order"]:
        for cand in PROP_CANDIDATES[key]:
            if cand in props and cand not in used:
                PROPS[key] = cand
                used.add(cand)
                break

    # ── 이름으로 못 찾은 경우 값의 생김새로 추정 ──────────────
    sample = pages[:20]

    def scan(pred):
        """조건에 맞는 값을 가진 선택형 속성 이름을 찾는다."""
        for pname in props:
            if pname in used:
                continue
            for pg in sample:
                v = _opt_value(pg.get("properties", {}).get(pname))
                if v and pred(v):
                    return pname, v
        return None, None

    if not PROPS.get("bu"):
        name, v = scan(lambda v: re.search(r"(BU|SU|본부|부문|전사)", v, re.I))
        if name:
            PROPS["bu"] = name; used.add(name)
            print(f"    (BU 속성을 값에서 찾음: '{name}' 예: {v})")

    if not PROPS.get("progress"):
        done_or_wip = {"완료", "진행중", "진행 중", "할일", "할 일", "종료", "예정", "대기"}
        name, v = scan(lambda v: v.replace(" ", "") in {x.replace(" ", "") for x in done_or_wip})
        if name:
            PROPS["progress"] = name; used.add(name)
            print(f"    (진행 상태 속성을 값에서 찾음: '{name}' 예: {v})")

    print("  속성 연결:")
    for key in ["name", "bu", "progress", "prio", "types", "desc"]:
        print(f"    {key:9} → {PROPS.get(key) or '(없음)'}")

    if not PROPS.get("name"):
        sys.exit(
            "[X] 제목 속성을 찾지 못했습니다.\n"
            f"    노션의 속성 이름: {', '.join(props.keys())}\n"
            "    → PROP_CANDIDATES 에 실제 이름을 추가하세요."
        )

    for key, msg in (("bu", "모든 안건이 '전사공통'으로 표시됩니다"),
                     ("progress", "모든 안건이 '진행중'으로 분류됩니다")):
        if not PROPS.get(key):
            print(f"  ⚠ {key} 속성을 찾지 못했습니다 — {msg}.", file=sys.stderr)
            print(f"    노션에 있는 속성: {', '.join(props.keys())}", file=sys.stderr)
            print(f"    → 위 이름 중 맞는 것을 PROP_CANDIDATES['{key}'] 에 추가하세요.", file=sys.stderr)

# ── 임원 주요 일정 캘린더 DB (별도 데이터베이스) ──────────────
# 환경변수 NOTION_CALENDAR_DATABASE_ID 를 설정하면 캘린더가 표시됩니다.
CAL_CANDIDATES = {
    "title": ["일정명", "일정", "이름", "제목", "Name"],
    "date":  ["기간", "일자", "날짜", "Date"],
    "cat":   ["구분", "분류", "유형", "대상"],
}


def _pick(props: dict, names: list):
    for n in names:
        if n in props:
            return props[n]
    return None


# ── 임원 캘린더 '구분' 정렬 우선순위(위에서부터) ──────────────
# 같은 날짜에 여러 일정이 있으면 이 순서대로 위에서부터 배치됩니다.
# 여기 적힌 값은 노션 '구분' 셀렉트 값과 글자가 정확히 같아야 정렬됩니다.
# (목록에 없는 값은 자동으로 맨 뒤로 갑니다.)
CAL_CAT_ORDER = [
    "CEO",        # 대표님 주요일정
    "임원 공통",   # 주요임원 공통일정
    "플랫폼SU장",  # SU장 주요일정
    "편의점BU장",
    "수퍼BU장",
    "홈쇼핑BU장",
    "주요임원",
    "인사",
]


def _cat_rank(cat: str) -> int:
    try:
        return CAL_CAT_ORDER.index((cat or "").strip())
    except ValueError:
        return 99


# ── BU/SU별 먼슬리 일정 DB 속성 후보 ──────────────────────────
MONTHLY_CANDIDATES = {
    "title": ["일정명", "일정", "이름", "제목", "Name"],
    "bu":    ["BU/SU", "BU", "SU", "사업부", "구분"],
    "date":  ["일시", "기간", "일자", "날짜", "Date"],
    "place": ["장소", "위치", "비고", "Place"],
}


PRIO_ALLOWED = {"완료", "진행", "검토"}
# 노션에서 쓰는 다른 표기를 대시보드 값으로 변환
PRIO_ALIAS = {
    "완료됨": "완료", "완": "완료", "done": "완료", "종료": "완료",
    "진행중": "진행", "진행 중": "진행", "in progress": "진행",
    "검토중": "검토", "검토 중": "검토", "review": "검토",
}
# 진행 상태 값이 아래 중 하나면 '완료' 버킷으로 분류 (공백 무시하고 비교)
DONE_LABELS = {"완료", "종료", "완료됨", "done", "닫힘", "마감"}

NOTION_VERSION = "2022-06-28"
# 사용할 모델: 환경변수로 지정할 수 있고, 없으면 계정에서 쓸 수 있는 모델을 자동 선택
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "").strip()
MODEL_PREFERENCE = [
    "gemini-3.5-flash",       # 정식(GA), 요약 작업에 적합
    "gemini-3.6-flash",       # 최신 정식
    "gemini-3.5-flash-lite",  # 더 빠르고 저렴
    "gemini-3.1-flash-lite",
]
NO_CONTENT = "노션에 작성된 내용이 없습니다."
AI_FAILED = "AI 요약 생성에 실패했습니다."

# ── Gemini 호출 레이트리밋(무료 티어 10 RPM = 6초당 1회) ──────
GEMINI_MIN_INTERVAL = float(os.environ.get("GEMINI_MIN_INTERVAL", "6.5"))
GEMINI_MAX_RETRIES = 3
_last_gemini_call = [0.0]


# ─────────────────────────────────────────────────────────────
# 노션 읽기
# ─────────────────────────────────────────────────────────────
def normalize_db_id(raw: str) -> str:
    s = (raw or "").strip().strip('"').strip("'")
    ids = re.findall(r"[0-9a-fA-F]{32}", s.replace("-", ""))
    if not ids:
        sys.exit(
            "[X] NOTION_DATABASE_ID 형식이 올바르지 않습니다.\n"
            f"    현재 값(일부): {s[:12]}... (길이 {len(s)})\n"
            "    → 노션 DB 주소에서 '?v=' 앞의 32자리를 넣어야 합니다."
        )
    h = ids[0].lower()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _fail(r):
    try:
        err = r.json()
        code, msg = err.get("code", ""), err.get("message", "")
    except Exception:
        code, msg = "", r.text[:300]
    hint = {
        400: "DB ID 형식 또는 요청이 잘못되었습니다.",
        401: "NOTION_TOKEN 이 잘못되었거나 만료되었습니다.",
        403: "이 통합에 해당 DB 접근 권한이 없습니다.",
        404: "DB를 찾을 수 없습니다. 노션 DB의 ··· → 연결 에서 통합을 추가했는지 확인하세요.",
        429: "요청이 너무 잦습니다. 잠시 후 다시 실행하세요.",
    }.get(r.status_code, "")
    sys.exit(f"[X] 노션 API 오류 {r.status_code} ({code})\n    노션 메시지: {msg}\n    → {hint}")


def find_child_database(token: str, page_id: str):
    """페이지 안에 들어 있는 데이터베이스(인라인 DB)의 ID를 찾는다."""
    url = f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=100"
    try:
        r = requests.get(url, headers=_headers(token), timeout=30)
        if r.status_code != 200:
            return None
        for blk in r.json().get("results", []):
            if blk.get("type") == "child_database":
                return blk.get("id")
    except Exception:
        pass
    return None


def notion_query_all(token: str, db_id: str) -> list:
    db_id = normalize_db_id(db_id)
    print(f"  대상 DB: {db_id[:8]}…{db_id[-4:]}")
    url = f"https://api.notion.com/v1/databases/{db_id}/query"
    results, payload, page = [], {"page_size": 100}, 0
    retried = False
    while True:
        r = requests.post(url, headers=_headers(token), json=payload, timeout=30)
        if r.status_code != 200:
            # 페이지 주소를 넣은 경우 → 그 안의 데이터베이스를 자동으로 찾아 재시도
            if (not retried and r.status_code == 400
                    and "not a database" in (r.text or "")):
                child = find_child_database(token, db_id)
                if child:
                    retried = True
                    db_id = child
                    url = f"https://api.notion.com/v1/databases/{child}/query"
                    print(f"    (페이지 안의 데이터베이스를 찾았습니다 → {child[:8]}…)")
                    continue
                sys.exit(
                    "[X] 입력한 주소는 페이지이고, 그 안에서 데이터베이스를 찾지 못했습니다.\n"
                    "    → 캘린더 블록 우측 상단의 ··· → '링크 복사'로 얻은 주소를 넣거나,\n"
                    "      ··· → '전체 페이지로 열기' 후의 주소를 사용하세요."
                )
            _fail(r)
        data = r.json()
        batch = data.get("results", [])
        results.extend(batch)
        page += 1
        print(f"    - {page}페이지: {len(batch)}건 (누적 {len(results)}건)")
        if data.get("has_more"):
            payload["start_cursor"] = data["next_cursor"]
        else:
            break
    return results


# ── 페이지 본문(블록) 읽기 ────────────────────────────────────
TEXT_BLOCKS = {
    "paragraph", "heading_1", "heading_2", "heading_3",
    "bulleted_list_item", "numbered_list_item", "to_do",
    "toggle", "quote", "callout", "code",
}

def _rich_to_text(rich):
    return "".join(t.get("plain_text", "") for t in (rich or []))


def _image_url(body: dict) -> str:
    """image 블록에서 실제 이미지 주소를 꺼낸다(external / file)."""
    t = body.get("type")
    if t == "external":
        return (body.get("external") or {}).get("url", "")
    if t == "file":
        return (body.get("file") or {}).get("url", "")
    return ""


def fetch_table_rows(token: str, table_id: str) -> str:
    """table 블록의 행(table_row)들을 읽어 텍스트 표로 변환."""
    url = f"https://api.notion.com/v1/blocks/{table_id}/children?page_size=100"
    rows, cursor = [], None
    while True:
        u = url + (f"&start_cursor={cursor}" if cursor else "")
        try:
            r = requests.get(u, headers=_headers(token), timeout=30)
            if r.status_code != 200:
                break
            data = r.json()
        except Exception:
            break
        for blk in data.get("results", []):
            if blk.get("type") == "table_row":
                cells = blk["table_row"].get("cells", [])
                rows.append([_rich_to_text(c).strip().replace("\n", "⏎") for c in cells])
        if data.get("has_more"):
            cursor = data.get("next_cursor")
        else:
            break
    if not rows:
        return ""
    lines = ["[표]"]
    for idx, row in enumerate(rows):
        lines.append(" | ".join(row))
        if idx == 0 and len(rows) > 1:
            lines.append(" | ".join("---" for _ in row))
    return "\n".join(lines)


def _download_data_uri(url: str, max_side: int = 1200, max_bytes: int = 1_600_000):
    """이미지를 내려받아 base64 data URI로 변환(선택적으로 축소)."""
    try:
        r = requests.get(url, timeout=30)
        if r.status_code != 200 or not r.content:
            return None
        raw = r.content
        ctype = (r.headers.get("Content-Type", "") or "").split(";")[0].strip() or "image/png"
        if HAS_PIL:
            try:
                img = Image.open(BytesIO(raw))
                if img.mode in ("P", "RGBA", "LA"):
                    img = img.convert("RGB")
                img.thumbnail((max_side, max_side))
                buf = BytesIO()
                img.save(buf, format="JPEG", quality=82)
                raw = buf.getvalue()
                ctype = "image/jpeg"
            except Exception:
                pass  # 변환 실패 시 원본 사용
        if len(raw) > max_bytes:
            print(f"      · 이미지가 너무 커서 건너뜀({len(raw)//1024}KB)", file=sys.stderr)
            return None
        b64 = base64.b64encode(raw).decode("ascii")
        return f"data:{ctype};base64,{b64}"
    except Exception as e:
        print(f"      · 이미지 다운로드 실패: {str(e)[:60]}", file=sys.stderr)
        return None


def embed_images(imgs: list) -> list:
    """이미지 주소 목록 → data URI 목록(화면에 바로 표시 가능)."""
    out = []
    for im in imgs:
        uri = _download_data_uri(im.get("url", ""))
        if uri:
            out.append({"src": uri, "caption": im.get("caption", "")})
    return out


# '세부일정' 헤딩 하위 이미지는 우측(일정) 패널로, 그 외 본문 이미지는 좌측 본문으로 분리한다.
SCHEDULE_HEADING = "세부일정"

def _norm_heading(s: str) -> str:
    return re.sub(r"\s+", "", s or "")

def _heading_level(btype: str) -> int:
    return {"heading_1": 1, "heading_2": 2, "heading_3": 3}.get(btype, 0)


def fetch_page_content(token: str, page_id: str, ctx=None, depth: int = 0):
    """페이지 본문을 읽어 (평문, 세부일정이미지, 본문이미지) 반환.
       '세부일정' 헤딩 하위 이미지는 sched(우측), 그 외 이미지는 body(좌측)로 분리."""
    if ctx is None:
        ctx = {"in_schedule": False, "sched_level": 0, "sched": [], "body": []}
    if depth > 2:
        return "", ctx["sched"], ctx["body"]
    url = f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=100"
    lines, cursor = [], None
    while True:
        u = url + (f"&start_cursor={cursor}" if cursor else "")
        try:
            r = requests.get(u, headers=_headers(token), timeout=30)
            if r.status_code != 200:
                return "\n".join(lines), ctx["sched"], ctx["body"]
            data = r.json()
        except Exception:
            return "\n".join(lines), ctx["sched"], ctx["body"]

        for blk in data.get("results", []):
            btype = blk.get("type", "")

            if btype in TEXT_BLOCKS:
                body = blk.get(btype, {})
                txt = _rich_to_text(body.get("rich_text"))
                # '세부일정' 구간 on/off 판정
                lvl = _heading_level(btype)
                nt = _norm_heading(txt)
                if lvl:
                    # 헤딩(제목1/2/3): '세부일정' 포함 시 on, 같은 레벨 이하의 다음 헤딩에서 off
                    if SCHEDULE_HEADING in nt:
                        ctx["in_schedule"] = True
                        ctx["sched_level"] = lvl
                    elif ctx["in_schedule"] and lvl <= ctx["sched_level"]:
                        ctx["in_schedule"] = False
                elif nt == SCHEDULE_HEADING:
                    # 헤딩이 아니어도 '세부일정'만 단독으로 적힌 줄이면 구간 시작(다음 헤딩까지)
                    ctx["in_schedule"] = True
                    ctx["sched_level"] = 99
                if txt.strip():
                    prefix = "- " if btype in ("bulleted_list_item", "numbered_list_item", "to_do") else ""
                    body_txt = txt.rstrip().replace("\t", "  ")   # 뒤 공백만 제거, 앞 공백/탭(→공백2칸)은 보존
                    lines.append("  " * depth + prefix + body_txt)

            elif btype == "table":
                tbl = fetch_table_rows(token, blk["id"])
                if tbl:
                    lines.append(tbl)
                continue  # 표의 자식(행)은 위에서 처리했으므로 아래 재귀 생략

            elif btype == "image":
                body = blk.get("image", {})
                src = _image_url(body)
                cap = _rich_to_text(body.get("caption")).strip()
                if src:
                    if ctx["in_schedule"]:
                        ctx["sched"].append({"url": src, "caption": cap})
                        # 세부일정 이미지는 우측 패널로 가므로 본문 텍스트엔 남기지 않음
                    else:
                        ctx["body"].append({"url": src, "caption": cap})
                        lines.append("  " * depth + f"[본문이미지{': ' + cap if cap else ''}]")
                continue

            if blk.get("has_children") and btype != "table":
                sub, _s, _b = fetch_page_content(token, blk["id"], ctx, depth + 1)
                if sub:
                    lines.append(sub)

        if data.get("has_more"):
            cursor = data.get("next_cursor")
        else:
            break
    return "\n".join(lines), ctx["sched"], ctx["body"]


# ── 속성 추출 ────────────────────────────────────────────────
def _title(p):  return _rich_to_text(p.get("title")).strip() if p else ""
def _rich(p):   return _rich_to_text(p.get("rich_text")).strip() if p else ""
def _multi(p):  return [t.get("name", "") for t in (p.get("multi_select") or [])] if p else []
def _checkbox(p): return bool(p.get("checkbox")) if p else False
def _number(p):   return p.get("number") if p else None

def _select(p):
    if not p:
        return ""
    node = p.get("select") or p.get("status")
    return (node or {}).get("name", "") if node else ""


def map_page(page: dict):
    props = page.get("properties", {})
    P = lambda k: props.get(PROPS.get(k) or "")

    name = _title(P("name"))
    if not name:
        return None

    # '대시보드 게시' 체크박스가 있는 DB면 체크된 안건만 노출(속성이 없으면 전부 노출 = 기존과 동일)
    pub = P("publish")
    if pub is not None and not _checkbox(pub):
        return None

    prio_raw = _select(P("prio")).strip()
    if not prio_raw:
        print(f"    · '우선순위'가 비어 있어 '진행'으로 기본 처리: {name}", file=sys.stderr)
    prio = PRIO_ALIAS.get(prio_raw.lower(), PRIO_ALIAS.get(prio_raw, prio_raw))
    if prio not in PRIO_ALLOWED:
        prio = "진행"

    prog = _select(P("progress")).replace(" ", "").lower()
    status = "완료" if prog in {d.replace(" ", "").lower() for d in DONE_LABELS} else "진행중"

    return {
        "id":     page.get("id", ""),
        "name":   name,
        "bu":     _select(P("bu")) or "전사공통",
        "prio":   prio,
        "status": status,
        "types":  _multi(P("types")),
        "descProp": _rich(P("desc")),
        "url":    page.get("url", "#"),
        "_order":   _number(P("order")),
        "_created": page.get("created_time", ""),
    }


def fetch_calendar(token: str):
    """임원 주요 일정 DB를 읽어 캘린더용 일정 목록을 반환."""
    db = os.environ.get("NOTION_CALENDAR_DATABASE_ID", "").strip()
    if not db:
        print("  (캘린더 DB가 설정되지 않아 건너뜁니다)")
        return []

    pages = notion_query_all(token, db)
    events = []
    for pg in pages:
        props = pg.get("properties", {})
        title = _title(_pick(props, CAL_CANDIDATES["title"]))
        if not title:
            continue
        d = (_pick(props, CAL_CANDIDATES["date"]) or {}).get("date") or {}
        start = (d.get("start") or "")[:10]
        if not start:
            continue
        end = (d.get("end") or start)[:10]
        events.append({
            "title": title,
            "start": start,
            "end": end,
            "cat": _select(_pick(props, CAL_CANDIDATES["cat"])) or "주요임원",
        })
    # 같은 날짜 안에서는 '구분' 우선순위(대표님 > 주요임원 공통 > SU장 …) 순으로 정렬
    events.sort(key=lambda e: (e["start"], _cat_rank(e["cat"]), e["end"]))
    print(f"  일정 {len(events)}건")
    return events


def fetch_monthly(token: str):
    """BU/SU별 먼슬리 일정 DB를 읽어 목록을 반환(시작·종료 시각 포함)."""
    db = os.environ.get("NOTION_MONTHLY_DATABASE_ID", "").strip()
    if not db:
        print("  (먼슬리 일정 DB가 설정되지 않아 건너뜁니다)")
        return []

    pages = notion_query_all(token, db)
    events = []
    for pg in pages:
        props = pg.get("properties", {})
        title = _title(_pick(props, MONTHLY_CANDIDATES["title"]))
        if not title:
            continue
        d = (_pick(props, MONTHLY_CANDIDATES["date"]) or {}).get("date") or {}
        start = d.get("start") or ""          # 예: '2026-09-11T11:00:00.000+09:00'
        if not start:
            continue
        end = d.get("end") or start
        events.append({
            "title": title,
            "bu":    _select(_pick(props, MONTHLY_CANDIDATES["bu"])) or "전사공통",
            "start": start,                   # 시간까지 그대로 유지(화면에서 11:00~12:00 표시용)
            "end":   end,
            "place": _rich(_pick(props, MONTHLY_CANDIDATES["place"])) or "",
        })
    events.sort(key=lambda e: e["start"])
    print(f"  먼슬리 일정 {len(events)}건")
    return events


# ── HR People Cycle DB 속성 후보 ──────────────────────────────
PC_CANDIDATES = {
    "title": ["일정명", "일정", "이름", "제목", "Name"],
    "date":  ["기간", "일자", "날짜", "Date"],
    "dae":   ["대분류", "구분", "카테고리", "영역"],
    "so":    ["소분류", "세부", "항목"],
    "type":  ["유형", "형태", "종류", "Type"],
    "detail":["상세내용", "상세", "내용", "설명", "비고"],
}


def _month_of(date_str: str) -> int:
    """'2026-04-15' → 4. 실패 시 0."""
    try:
        return int(date_str[5:7])
    except Exception:
        return 0


def fetch_people_cycle(token: str):
    """People Cycle DB를 읽어 연간 타임라인용 목록을 반환."""
    db = os.environ.get("NOTION_PEOPLECYCLE_DATABASE_ID", "").strip()
    if not db:
        print("  (People Cycle DB가 설정되지 않아 건너뜁니다)")
        return {"year": str(dt.datetime.now().year), "events": []}

    pages = notion_query_all(token, db)
    events, year = [], None
    for pg in pages:
        props = pg.get("properties", {})
        title = _title(_pick(props, PC_CANDIDATES["title"]))
        if not title:
            continue
        d = (_pick(props, PC_CANDIDATES["date"]) or {}).get("date") or {}
        start = (d.get("start") or "")[:10]
        if not start:
            continue
        end = (d.get("end") or start)[:10]
        if year is None and len(start) >= 4:
            year = start[:4]
        events.append({
            "name":   title,
            "daebun": _select(_pick(props, PC_CANDIDATES["dae"])) or "",
            "sobun":  _select(_pick(props, PC_CANDIDATES["so"])) or "",
            "startM": _month_of(start),
            "endM":   _month_of(end) or _month_of(start),
            "type":   _select(_pick(props, PC_CANDIDATES["type"])) or "기간형",
            "detail": _rich(_pick(props, PC_CANDIDATES["detail"])) or "",
        })
    events = [e for e in events if e["startM"]]
    print(f"  People Cycle 일정 {len(events)}건")
    return {"year": year or str(dt.datetime.now().year), "events": events}


# ─────────────────────────────────────────────────────────────
# Gemini 분석
# ─────────────────────────────────────────────────────────────
ITEM_PROMPT = """너는 대기업 인사팀 임원 보고 대시보드의 '한 줄 요약'을 쓰는 분석가다.
아래는 사내 HR 안건 하나의 제목과 실제 작성 내용이다.
(작성 내용에는 표가 '[표] 행 | 행' 형태로, 이미지가 '[본문이미지]' 형태로 표시될 수 있다.)

[안건명]
{name}

[작성 내용]
{body}

임원이 표에서 이 안건을 한눈에 파악하도록 '한 줄 요약'을 작성하라.

작성 원칙:
- 이 안건이 '무엇을 하는지'가 드러나게, 핵심 행위 중심으로 쓸 것. (예: "경력사원 채용 추진", "급여체계 개편 검토", "조직역량 진단 결과 공유")
- 45자 이내, 개조식(명사형 종결). "~함/~합니다" 같은 서술 어미나 마침표를 쓰지 말 것.
- 작성 내용에 실제로 있는 사실만 사용. 없는 수치·일정·인원·부서·담당자를 지어내지 말 것.
- 핵심 수치가 있으면 하나만 담아 구체성을 더할 것. (예: "경력 32명 채용 진행")
- 제목을 그대로 되풀이하지 말고, 본문이 더해주는 정보를 담을 것.
- 표만 있거나 내용이 빈약하면 제목을 다듬는 선에서 간결하게.
- 진행상태(완료/진행/검토)는 쓰지 말 것. 그건 표에 별도 표시된다.

다른 말 없이 JSON만 출력:
{{
  "one_line": "여기에 한 줄 요약"
}}
"""


def gemini_generate(client, model, prompt):
    """레이트리밋(무료 티어) 준수 + 429/일시오류 재시도."""
    for attempt in range(GEMINI_MAX_RETRIES):
        wait = GEMINI_MIN_INTERVAL - (time.time() - _last_gemini_call[0])
        if wait > 0:
            time.sleep(wait)
        try:
            resp = client.models.generate_content(model=model, contents=prompt)
            _last_gemini_call[0] = time.time()
            return resp
        except Exception as e:
            _last_gemini_call[0] = time.time()
            msg = str(e).lower()
            transient = any(k in msg for k in (
                "429", "resource_exhausted", "rate", "quota", "503",
                "unavailable", "overloaded", "500", "deadline", "timeout"))
            if attempt < GEMINI_MAX_RETRIES - 1 and transient:
                delay = min(60, (2 ** attempt) * 5) + random.uniform(0, 2)
                m = re.search(r"retry.{0,20}?(\d+(?:\.\d+)?)\s*s", msg)
                if m:
                    delay = max(delay, float(m.group(1)) + 1)
                print(f"    · 일시 오류로 {delay:.0f}초 후 재시도 ({attempt + 1}/{GEMINI_MAX_RETRIES})…",
                      file=sys.stderr)
                time.sleep(delay)
                continue
            raise
    return None


def pick_model(client) -> str:
    """계정에서 실제로 사용 가능한 모델을 골라 반환한다."""
    available = []
    try:
        for m in client.models.list():
            name = (getattr(m, "name", "") or "").replace("models/", "")
            acts = (getattr(m, "supported_actions", None)
                    or getattr(m, "supported_generation_methods", None) or [])
            if name and (not acts or "generateContent" in acts):
                available.append(name)
    except Exception as e:
        print(f"  ! 모델 목록 조회 실패({e}) — 기본 후보로 진행합니다.", file=sys.stderr)

    candidates, seen = [], set()
    for c in ([GEMINI_MODEL] if GEMINI_MODEL else []) + MODEL_PREFERENCE:
        if c and c not in seen:
            seen.add(c); candidates.append(c)
    for cand in candidates:
        if not available or cand in available:
            if _smoke_test(client, cand):
                print(f"  사용 모델: {cand}")
                return cand

    # 후보가 모두 실패하면 목록에 있는 flash 계열을 순서대로 시도
    rest = [n for n in available if "flash" in n and "lite" not in n] + available
    for name in [n for n in rest if not (n in seen or seen.add(n))]:
        if _smoke_test(client, name):
            print(f"  사용 모델(자동 선택): {name}")
            return name

    sys.exit(
        "[X] 사용 가능한 Gemini 모델을 찾지 못했습니다.\n"
        f"    계정에서 조회된 모델: {', '.join(available[:15]) if available else '(조회 실패)'}\n"
        "    → GEMINI_MODEL 환경변수로 모델명을 직접 지정하거나 API 키를 확인하세요."
    )


def _smoke_test(client, model: str) -> bool:
    """모델이 실제로 응답하는지 1회 확인."""
    try:
        r = gemini_generate(client, model, "ok 이라고만 답해줘.")
        return bool((r.text or "").strip()) if r else False
    except Exception as e:
        short = str(e)[:90].replace("\n", " ")
        print(f"    - {model}: 사용 불가 ({short}…)")
        return False


def _gen_json(client, model, prompt, fallback):
    """Gemini 호출 후 JSON 파싱. 실패 시 fallback 반환."""
    try:
        resp = gemini_generate(client, model, prompt)
        txt = (resp.text or "").strip() if resp else ""
        txt = re.sub(r"^```(?:json)?|```$", "", txt, flags=re.MULTILINE).strip()
        m = re.search(r"[\[{].*[\]}]", txt, re.DOTALL)
        return json.loads(m.group(0)) if m else fallback
    except Exception as e:
        print(f"    ! Gemini 처리 실패: {e}", file=sys.stderr)
        return fallback


def analyze_item(client, model, it: dict) -> dict:
    """설명 원문(desc)은 노션 본문 그대로 보존(날짜·명단·순서·표/이미지 표시 유지),
       표에 들어갈 '한 줄 요약'만 제미나이로 생성. 실패 시 본문 첫 줄로 폴백."""
    body = "\n".join(x for x in [it.get("descProp", ""), it.get("bodyText", "")] if x).strip()
    if not body:
        return {"oneLine": NO_CONTENT, "desc": NO_CONTENT, "aiOk": True}
    # 폴백 한 줄: 본문 첫 의미 있는 줄
    fb = ""
    for ln in body.split("\n"):
        t = ln.strip().lstrip("-").strip()
        if t and not t.startswith("["):
            fb = t
            break
    fb = (fb or body.replace("\n", " ").strip())[:40]
    data = _gen_json(client, model, ITEM_PROMPT.format(name=it["name"], body=body[:6000]), fallback={})
    if not data:                          # 재시도까지 실패 → 원문 보존, 첫 줄로 대체
        return {"oneLine": fb, "desc": body, "aiOk": False}
    one = str(data.get("one_line") or "").strip() or fb
    return {"oneLine": one, "desc": body, "aiOk": True}


BATCH_PROMPT = """너는 대기업 인사팀 임원 보고 대시보드의 '한 줄 요약'을 쓰는 분석가다.
아래에 여러 개의 HR 안건이 번호와 함께 주어진다. 각 안건마다 '한 줄 요약'을 하나씩 만들어라.
(각 안건의 [내용]에는 표가 '[표] 행 | 행' 형태로, 이미지가 '[본문이미지]' 형태로 표시될 수 있다.)

각 요약이 반드시 지킬 원칙(안건마다 독립적으로 적용):
- 그 안건의 [내용]에 실제로 있는 사실만 사용한다. 다른 안건 내용을 섞지 말고, 없는 수치·일정·인원·부서·담당자를 지어내지 않는다.
- 그 안건이 '무엇을 하는지'가 드러나게 핵심 행위 중심으로 쓴다. (예: "경력사원 채용 추진", "급여체계 개편 검토", "조직역량 진단 결과 공유")
- 45자 이내, 개조식(명사형 종결). "~함/~합니다" 같은 서술 어미나 마침표를 쓰지 않는다.
- 핵심 수치가 있으면 하나만 담아 구체성을 더한다. (예: "경력 32명 채용 진행")
- 제목을 그대로 되풀이하지 말고, [내용]이 더해주는 정보를 담는다.
- [내용]이 표·이미지뿐이거나 빈약하면 제목을 다듬는 선에서 간결하게 쓴다.
- 진행상태(완료/진행/검토)는 쓰지 않는다. 진행상태는 표에 별도 표시된다.

[안건들]
{listing}

출력은 다른 말·설명·코드펜스 없이 아래 형식의 JSON 객체 하나만.
키는 각 안건의 번호(문자열), 값은 그 안건의 한 줄 요약.
{{"1": "요약", "2": "요약"}}
반드시 위 [안건들]의 모든 번호를 빠짐없이 포함하고 번호를 정확히 일치시킬 것."""


def _first_line(body: str) -> str:
    for ln in body.split("\n"):
        t = ln.strip().lstrip("-").strip()
        if t and not t.startswith("["):
            return t[:40]
    return body.replace("\n", " ").strip()[:40]


def summarize_batch(client, model, items: list, chunk: int = 10) -> None:
    """모든 안건의 '한 줄 요약'을 배치 호출로 생성.
       - 개별 프롬프트와 동일한 품질 원칙을 배치 프롬프트에 그대로 적용.
       - 설명 원문(desc)은 노션 본문 그대로 보존(날짜·명단·순서·표/이미지 표시 유지).
       - 요약 누락·실패분은 본문 첫 줄로 폴백."""
    bodies = []
    for it in items:
        b = "\n".join(x for x in [it.get("descProp", ""), it.get("bodyText", "")] if x).strip()
        bodies.append(b)

    ok = 0
    total = len(items)
    for start in range(0, total, chunk):
        idxs = list(range(start, min(start + chunk, total)))
        listing = "\n".join(
            "[%d] 제목: %s\n내용: %s\n---" % (
                k + 1, items[i]["name"], (bodies[i] or "(내용 없음)")[:800])
            for k, i in enumerate(idxs))
        data = _gen_json(client, model, BATCH_PROMPT.format(listing=listing), fallback={})
        for k, i in enumerate(idxs):
            body = bodies[i]
            one = ""
            if isinstance(data, dict):
                one = str(data.get(str(k + 1), "") or "").strip()
            items[i]["desc"] = body if body else NO_CONTENT
            items[i]["oneLine"] = (one or _first_line(body)) if body else NO_CONTENT
            items[i]["aiOk"] = bool(one)
            if one:
                ok += 1
        print("  · 배치 %d–%d 요약 완료" % (start + 1, start + len(idxs)))
    print("  한 줄 요약 %d/%d건 AI 생성" % (ok, total)
          + ("" if ok == total else " (%d건 본문 첫 줄로 대체)" % (total - ok)))


HEADLINE_PROMPT = """너는 대기업 인사팀 임원 보고 '종합 브리핑'의 핵심 요약 한 줄을 쓰는 분석가다.
아래는 이번 월간 세션의 전체 안건 목록이다.

총 {count}건 (완료 {done} · 진행 {prog} · 검토 {review})

[안건 목록]
{listing}

이 목록만 근거로, 이번 세션 전체를 관통하는 '핵심 요약'을 딱 한 문장(80자 내외)으로 작성하라.
- 목록에 없는 사실·숫자·안건명을 지어내지 말 것. 불확실하면 일반적 표현으로.
- 개별 안건 나열이 아니라, 이번 세션이 무엇에 집중했는지 큰 흐름을 한 문장으로.
- 임원 보고체, 군더더기 없이.
다른 말·기호·따옴표 없이 한 문장만 출력."""


def make_brief(client, model, items: list, bucket: str = "이번 세션"):
    """통합 대시보드 종합 브리핑.
       반환: {"count": 건수 문장, "headline": 핵심요약 한 줄, "items": [BU 접두 안건 줄]}
       - 건수/안건 줄은 코드로 구성(정확), 핵심요약 한 문장만 제미나이(1회)."""
    if not items:
        return {"count": "%s 안건이 없습니다." % bucket, "headline": "", "items": []}
    n = len(items)
    cnt = {p: sum(1 for i in items if i["prio"] == p) for p in ("완료", "진행", "검토")}
    count_line = ("이번 세션 안건은 총 %d건이며, 완료 %d건 · 진행 %d건 · 검토 %d건입니다."
                  % (n, cnt["완료"], cnt["진행"], cnt["검토"]))
    item_lines = ["[%s] 「%s」 — %s" % (i.get("bu", "전사공통"), i["name"],
                                        i.get("oneLine", "") or "핵심 안건") for i in items]
    listing = "\n".join("- [%s] %s: %s" % (i.get("bu", ""), i["name"], i.get("oneLine", "")) for i in items)
    fb_head = ("이번 세션은 총 %d건의 인사 안건을 완료 %d · 진행 %d · 검토 %d건으로 점검하며, "
               "전사 공통 과제와 각 사업부 현안을 함께 다루고 있습니다."
               % (n, cnt["완료"], cnt["진행"], cnt["검토"]))
    try:
        resp = gemini_generate(client, model, HEADLINE_PROMPT.format(
            count=n, done=cnt["완료"], prog=cnt["진행"], review=cnt["검토"], listing=listing[:6000]))
        txt = (resp.text or "").strip() if resp else ""
        txt = re.sub(r"^```.*?\n|```$", "", txt, flags=re.MULTILINE).strip().strip('"').strip()
        headline = txt or fb_head
    except Exception as e:
        print("    ! 핵심요약 생성 실패(폴백): %s" % str(e)[:50], file=sys.stderr)
        headline = fb_head
    return {"count": count_line, "headline": headline, "items": item_lines}


# ─────────────────────────────────────────────────────────────
# 출력
# ─────────────────────────────────────────────────────────────
def write_data_js(path: str, data: dict):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    body = ("// 이 파일은 build_dashboard.py 가 자동 생성합니다. 수동 편집 금지.\n"
            "window.DASHBOARD_DATA = "
            + json.dumps(data, ensure_ascii=False, indent=2) + ";\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    print(f"  저장 완료 → {path}")


def build(save_local: bool):
    token = os.environ["NOTION_TOKEN"]
    db_id = os.environ["NOTION_DATABASE_ID"]
    gkey = os.environ["GEMINI_API_KEY"]

    print("· 노션 조회 중…")
    pages = notion_query_all(token, db_id)
    print(f"  노션이 돌려준 행: {len(pages)}건")

    if pages:
        resolve_props(pages)
    items = [m for m in (map_page(p) for p in pages) if m]
    # 노션 게시 순서 재현: '순서' 숫자 속성이 있으면 그 순, 없으면 생성시각 순
    if any(it.get("_order") is not None for it in items):
        items.sort(key=lambda it: (it.get("_order") if it.get("_order") is not None else float("inf"), it.get("_created", "")))
    else:
        items.sort(key=lambda it: it.get("_created", ""))
    skipped = len(pages) - len(items)
    if skipped:
        print(f"  (제목이 비어 건너뛴 행: {skipped}건)")
    print(f"  처리 대상 안건: {len(items)}건")

    print("· 노션 페이지 본문(표·이미지 포함) 읽는 중…")
    if not HAS_PIL:
        print("  (참고: Pillow 미설치 — 이미지 축소 없이 원본 크기로 담습니다)", file=sys.stderr)
    for i, it in enumerate(items, 1):
        text, sched_imgs, body_imgs = fetch_page_content(token, it["id"])
        it["bodyText"] = text
        it["images"] = embed_images(sched_imgs)       # 세부일정 → 우측 패널
        it["bodyImages"] = embed_images(body_imgs)    # 본문 → 좌측 설명
        chars = len(it["bodyText"]) + len(it["descProp"])
        print(f"  [{i}/{len(items)}] {it['name']} — 본문 {chars}자, 일정사진 {len(it['images'])}장, 본문사진 {len(it['bodyImages'])}장")
        time.sleep(0.15)

    print("· Gemini 준비 중…")
    client = genai.Client(api_key=gkey)
    model = pick_model(client)

    print("· Gemini 한 줄 요약 생성 중… (안건을 묶어 배치 호출 · 설명 원문은 노션 그대로)")
    summarize_batch(client, model, items)

    print("· 임원 주요 일정 조회 중…")
    cal_events = fetch_calendar(token)

    print("· HR People Cycle 조회 중…")
    people_cycle = fetch_people_cycle(token)

    print("· BU/SU별 먼슬리 일정 조회 중…")
    monthly = fetch_monthly(token)

    print("· 브리핑 생성 중…")
    # 통합 HR 대시보드는 전 안건을 한 화면에 보므로, 브리핑도 전체 기준 하나로 생성
    briefs = {
        "전체": make_brief(client, model, items, "이번 세션"),
    }
    print(f"  전체 {len(items)}건 기준 브리핑 생성")

    # 대시보드에 필요한 필드만 남기기
    clean = [{
        "name": it["name"], "bu": it["bu"], "prio": it["prio"], "status": it["status"],
        "types": it["types"], "oneLine": it["oneLine"],
        "desc": it["desc"], "url": it["url"], "images": it.get("images", []),
        "bodyImages": it.get("bodyImages", []),
    } for it in items]

    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=9)))
    data = {
        "generatedAt": now.strftime("%Y-%m-%d %H:%M KST"),
        "session": f"{now.year}년 {now.month}월",
        "briefs": briefs,
        "items": clean,
        "calendar": {"month": now.strftime("%Y-%m"), "events": cal_events},
        "peopleCycle": people_cycle,
        "monthly": monthly,
    }

    print("· 파일 쓰는 중…")
    write_data_js(REPO_OUT, data)

    if save_local:
        local_dir = os.environ.get("LOCAL_DIR", DEFAULT_LOCAL_DIR)
        try:
            write_data_js(os.path.join(local_dir, "data.js"), data)
            snap = os.path.join(local_dir, f"data_{now.strftime('%Y%m%d')}.json")
            with open(snap, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print(f"  백업 완료 → {snap}")
        except OSError as e:
            print(f"  ! 로컬 저장 실패(경로 확인 필요): {e}", file=sys.stderr)

    print(f"· 전체 완료 ({len(clean)}건, {data['generatedAt']})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", action="store_true", help="내 PC 폴더에도 저장")
    build(save_local=ap.parse_args().local)
