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
  (선택) NOTION_CALENDAR_DATABASE_ID  임원 주요 일정 DB
  (선택) GEMINI_MODEL, LOCAL_DIR
"""

import os
import re
import sys
import json
import time
import argparse
import datetime as dt

import requests
from google import genai

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
}
PROPS = {}          # 첫 조회 때 실제 속성 이름으로 채워집니다


def resolve_props(page: dict):
    """실제 노션 속성 이름을 찾아 PROPS에 연결한다."""
    props = page.get("properties", {})
    used = set()
    # bu → progress 순서로 먼저 잡아야 '상태'가 올바른 쪽에 배정됩니다
    for key in ["name", "prio", "bu", "progress", "types", "desc"]:
        for cand in PROP_CANDIDATES[key]:
            if cand in props and cand not in used:
                PROPS[key] = cand
                used.add(cand)
                break
    print("  속성 연결:")
    for key in ["name", "bu", "progress", "prio", "types", "desc"]:
        print(f"    {key:9} → {PROPS.get(key) or '(없음)'}")
    missing = [k for k in ("name",) if not PROPS.get(k)]
    if missing:
        sys.exit(
            "[X] 제목 속성을 찾지 못했습니다.\n"
            f"    노션의 속성 이름: {', '.join(props.keys())}\n"
            "    → PROP_CANDIDATES 에 실제 이름을 추가하세요."
        )

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

PRIO_ALLOWED = {"높음", "중간", "낮음"}
# 노션에서 쓰는 다른 표기를 대시보드 값으로 변환
PRIO_ALIAS = {
    "보통": "중간", "중": "중간", "normal": "중간", "medium": "중간",
    "상": "높음", "high": "높음", "긴급": "높음",
    "하": "낮음", "low": "낮음",
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


def notion_query_all(token: str, db_id: str) -> list:
    db_id = normalize_db_id(db_id)
    print(f"  대상 DB: {db_id[:8]}…{db_id[-4:]}")
    url = f"https://api.notion.com/v1/databases/{db_id}/query"
    results, payload, page = [], {"page_size": 100}, 0
    while True:
        r = requests.post(url, headers=_headers(token), json=payload, timeout=30)
        if r.status_code != 200:
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


def fetch_page_text(token: str, page_id: str, depth: int = 0) -> str:
    """페이지 본문 블록을 재귀적으로 읽어 평문으로 반환."""
    if depth > 2:
        return ""
    url = f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=100"
    lines, cursor = [], None
    while True:
        u = url + (f"&start_cursor={cursor}" if cursor else "")
        try:
            r = requests.get(u, headers=_headers(token), timeout=30)
            if r.status_code != 200:
                return "\n".join(lines)
            data = r.json()
        except Exception:
            return "\n".join(lines)

        for blk in data.get("results", []):
            btype = blk.get("type", "")
            if btype in TEXT_BLOCKS:
                body = blk.get(btype, {})
                txt = _rich_to_text(body.get("rich_text"))
                if txt.strip():
                    prefix = "- " if btype in ("bulleted_list_item", "numbered_list_item", "to_do") else ""
                    lines.append(prefix + txt.strip())
            if blk.get("has_children"):
                sub = fetch_page_text(token, blk["id"], depth + 1)
                if sub:
                    lines.append(sub)

        if data.get("has_more"):
            cursor = data.get("next_cursor")
        else:
            break
    return "\n".join(lines)


# ── 속성 추출 ────────────────────────────────────────────────
def _title(p):  return _rich_to_text(p.get("title")).strip() if p else ""
def _rich(p):   return _rich_to_text(p.get("rich_text")).strip() if p else ""
def _multi(p):  return [t.get("name", "") for t in (p.get("multi_select") or [])] if p else []

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

    prio_raw = _select(P("prio")).strip()
    prio = PRIO_ALIAS.get(prio_raw.lower(), PRIO_ALIAS.get(prio_raw, prio_raw))
    if prio not in PRIO_ALLOWED:
        prio = "중간"

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
    events.sort(key=lambda e: (e["start"], e["end"]))
    print(f"  일정 {len(events)}건")
    return events


# ─────────────────────────────────────────────────────────────
# Gemini 분석
# ─────────────────────────────────────────────────────────────
ITEM_PROMPT = """너는 대기업 인사팀의 임원 보고 자료를 만드는 분석가다.
아래는 사내 HR 안건 하나의 제목과 실제 작성 내용이다.

[안건명]
{name}

[작성 내용]
{body}

이 내용만 근거로 아래 JSON을 만들어라. 반드시 지킬 것:
- 작성 내용에 없는 사실, 수치, 일정, 담당자를 절대 지어내지 말 것.
- 내용이 부족하면 있는 만큼만 쓰고, 억지로 채우지 말 것.
- 일정이나 담당자를 언급하지 말 것.
- 임원이 판단에 참고할 핵심(무엇을 왜 하는지, 현재 어디까지 왔는지, 무엇이 쟁점인지) 위주로 쓸 것.

JSON 형식 (다른 말 없이 JSON만 출력):
{{
  "one_line": "표에 넣을 한 줄 핵심 요약. 35자 내외.",
  "three_lines": ["첫 줄", "둘째 줄", "셋째 줄"],
  "polished": "작성 내용 전체를 빠짐없이 담되 문장을 정돈한 글. 내용 추가·삭제 없이 톤앤매너만 다듬을 것. 원문이 항목식이면 항목식 유지."
}}

three_lines 규칙: 각 줄은 한 문장, 30자 내외. 안건의 목적·현황·쟁점 순서를 권장하되 내용에 맞게 조정.
"""

BRIEF_PROMPT = """너는 대기업 인사팀의 임원 보고 브리핑을 작성하는 분석가다.
아래는 이번 월간 세션의 '{bucket}' 안건 목록이다.

총 {count}건 (우선순위: 높음 {high}건, 중간 {mid}건, 낮음 {low}건)

[안건 목록]
{listing}

임원에게 보고할 브리핑을 3줄로 작성하라. 반드시 지킬 것:
- 목록에 없는 사실을 지어내지 말 것.
- 일정이나 담당자를 언급하지 말 것.
- 1줄: 전체 규모와 우선순위 구성 요약.
- 2줄: 우선순위가 높거나 비중이 큰 주요 안건이 무엇인지.
- 3줄: 전체적으로 주목할 흐름이나 쟁점.

다른 말 없이 JSON 배열만 출력: ["첫 줄", "둘째 줄", "셋째 줄"]
"""


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
        r = client.models.generate_content(model=model, contents="ok 이라고만 답해줘.")
        return bool((r.text or "").strip())
    except Exception as e:
        msg = str(e)
        short = msg[:90].replace("\n", " ")
        print(f"    - {model}: 사용 불가 ({short}…)")
        return False


def _gen_json(client, model, prompt, fallback):
    """Gemini 호출 후 JSON 파싱. 실패 시 fallback 반환."""
    try:
        resp = client.models.generate_content(model=model, contents=prompt)
        txt = (resp.text or "").strip()
        txt = re.sub(r"^```(?:json)?|```$", "", txt, flags=re.MULTILINE).strip()
        m = re.search(r"[\[{].*[\]}]", txt, re.DOTALL)
        return json.loads(m.group(0)) if m else fallback
    except Exception as e:
        print(f"    ! Gemini 처리 실패: {e}", file=sys.stderr)
        return fallback


def analyze_item(client, model, it: dict) -> dict:
    body = "\n".join(x for x in [it.get("descProp", ""), it.get("bodyText", "")] if x).strip()
    if not body:
        return {"oneLine": NO_CONTENT, "ai": [NO_CONTENT, "", ""], "desc": NO_CONTENT}

    prompt = ITEM_PROMPT.format(name=it["name"], body=body[:6000])
    data = _gen_json(client, model, prompt, fallback={})
    if not data:                      # 일시적 오류일 수 있으므로 1회 재시도
        time.sleep(1.5)
        data = _gen_json(client, model, prompt, fallback={})
    if not data:                      # 그래도 실패하면 원문을 자르지 않고 실패로 표시
        return {"oneLine": AI_FAILED, "ai": [AI_FAILED, "", ""], "desc": body, "aiOk": False}

    three = [str(x).strip() for x in (data.get("three_lines") or []) if str(x).strip()][:3]
    while len(three) < 3:
        three.append("")
    return {
        "oneLine": str(data.get("one_line") or "").strip() or body.replace("\n", " ")[:35],
        "ai": three,
        "desc": str(data.get("polished") or "").strip() or body,
        "aiOk": True,
    }


def make_brief(client, model, items: list, bucket: str) -> list:
    if not items:
        return [f"{bucket} 안건이 없습니다.", "", ""]
    cnt = {p: sum(1 for i in items if i["prio"] == p) for p in ("높음", "중간", "낮음")}
    listing = "\n".join(
        f"- [{i['bu']} / 우선순위 {i['prio']}] {i['name']}: {i.get('oneLine','')}"
        for i in items
    )
    fb = [f"{bucket} 안건은 총 {len(items)}건입니다.",
          f"우선순위 구성은 높음 {cnt['높음']}건, 중간 {cnt['중간']}건, 낮음 {cnt['낮음']}건입니다.", ""]
    out = _gen_json(
        client, model,
        BRIEF_PROMPT.format(bucket=bucket, count=len(items),
                            high=cnt["높음"], mid=cnt["중간"], low=cnt["낮음"],
                            listing=listing[:6000]),
        fallback=fb,
    )
    if isinstance(out, dict):
        out = list(out.values())
    lines = [str(x).strip() for x in (out or []) if str(x).strip()][:3]
    return lines or fb


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
        resolve_props(pages[0])
    items = [m for m in (map_page(p) for p in pages) if m]
    skipped = len(pages) - len(items)
    if skipped:
        print(f"  (제목이 비어 건너뛴 행: {skipped}건)")
    print(f"  처리 대상 안건: {len(items)}건")

    print("· 노션 페이지 본문 읽는 중…")
    for i, it in enumerate(items, 1):
        it["bodyText"] = fetch_page_text(token, it["id"])
        chars = len(it["bodyText"]) + len(it["descProp"])
        print(f"  [{i}/{len(items)}] {it['name']} — 본문 {chars}자")
        time.sleep(0.15)

    print("· Gemini 준비 중…")
    client = genai.Client(api_key=gkey)
    model = pick_model(client)

    print("· Gemini 분석 중…")
    fails = 0
    for i, it in enumerate(items, 1):
        it.update(analyze_item(client, model, it))
        if it.get("aiOk") is False:
            fails += 1
        print(f"  [{i}/{len(items)}] {it['name']}")
        time.sleep(0.4)

    if fails:
        print(f"  ⚠ {fails}건은 AI 요약에 실패했습니다. 위 오류 메시지를 확인하세요.", file=sys.stderr)
    else:
        print("  모든 안건 AI 분석 성공")

    print("· 임원 주요 일정 조회 중…")
    cal_events = fetch_calendar(token)

    print("· 브리핑 생성 중…")
    now_items = [i for i in items if i["status"] == "진행중"]
    done_items = [i for i in items if i["status"] == "완료"]
    briefs = {
        "진행": make_brief(client, model, now_items, "이번달 진행"),
        "완료": make_brief(client, model, done_items, "지난달 완료"),
    }
    print(f"  진행 {len(now_items)}건 / 완료 {len(done_items)}건")

    # 대시보드에 필요한 필드만 남기기
    clean = [{
        "name": it["name"], "bu": it["bu"], "prio": it["prio"], "status": it["status"],
        "types": it["types"], "oneLine": it["oneLine"], "ai": it["ai"],
        "desc": it["desc"], "url": it["url"],
    } for it in items]

    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=9)))
    data = {
        "generatedAt": now.strftime("%Y-%m-%d %H:%M KST"),
        "session": f"{now.year}년 {now.month}월",
        "briefs": briefs,
        "items": clean,
        "calendar": {"month": now.strftime("%Y-%m"), "events": cal_events},
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
