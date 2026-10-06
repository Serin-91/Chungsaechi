# 온통청년 API 값 + 공고 PDF 추출값 → samples/merged.jsonl (에이전트가 쓰는 최종 데이터)
#   python merge.py   → 전체 정책 병합 + 상태 집계 출력
#
# 필드마다 {value, status, source, reason, evidence, doc, page, api_value, pdf_value}
#   status  정상     : 그대로 써도 됨
#           확인필요 : 값은 있지만 애매함(근사·후보 여러 개·지난 공고 의심 등) → 단정하지 말고 원문 확인 권장
#           충돌     : API와 PDF가 다름 → API 값을 쓰되 둘 다 보여주고 원문 확인 권장
#           없음     : 어디에도 없음 → 추측하지 말고 "공고문 확인" + 원문 링크
#   source  api | pdf
# 에이전트 규칙: 확인필요·충돌·없음은 단정 금지. collected_at 이후 바뀌었을 수 있음을 안내. 마감 여부는 aply_state 사용.
import datetime
import json
import re
from collections import Counter

from evaluate import CODES, S, text_of
from extract import doc_year, extract_text
from ocr import ParseError

TODAY = datetime.date.today().strftime("%Y%m%d")
VERSION = re.compile(r"수정|정정|변경|연장|재공고|최종")
NUM_FIELDS = ["aplyStart", "aplyEnd", "minAge", "maxAge", "sprtSclCnt"]
CODE_FIELDS = {f: g for g, v in CODES.items() if g != "ZIP" for f in v["fields"]}


def api_values(p):
    """API 값 + 이상값 사유. {field: (value, reason)}"""
    out = {}
    s, _, e = (p.get("aplyYmd") or "").partition(" ~ ")
    s, e = s.strip(), e.strip()
    valid = lambda d: bool(re.fullmatch(r"\d{8}", d)) and _ok_date(d)
    out["aplyStart"] = (s, "" if not s or valid(s) else "날짜 형식 오류")
    out["aplyEnd"] = (e, "" if not e or valid(e) else "날짜 형식 오류")
    if s and e and valid(s) and valid(e) and e < s:
        out["aplyEnd"] = (e, "종료일이 시작일보다 빠름")
    mn, mx = int(p.get("sprtTrgtMinAge") or 0), int(p.get("sprtTrgtMaxAge") or 0)
    no_limit = p.get("sprtTrgtAgeLmtYn") == "Y"
    age_note = "연령제한 '없음'인데 나이 값이 있음" if no_limit and (mn or mx) else ""
    out["minAge"] = (mn or None, "하한 비정상(14세 미만)" if 0 < mn < 14 else age_note)
    out["maxAge"] = (mx or None, "상한 비정상" if mx and (mx < 14 or mx > 99) else age_note)
    if mn and mx and mn > mx:
        out["minAge"] = out["maxAge"] = (out["minAge"][0], "최소 나이 > 최대 나이")
    out["sprtSclCnt"] = (int(p.get("sprtSclCnt") or 0) or None, "")
    return out


def _ok_date(d):
    try:
        datetime.date(int(d[:4]), int(d[4:6]), int(d[6:]))
        return True
    except ValueError:
        return False


def code_checks(p):
    bad = []
    for f, g in CODE_FIELDS.items():
        for c in filter(None, (x.strip() for x in str(p.get(f) or "").split(","))):
            if c not in CODES[g]["codes"]:
                bad.append(f"{f}={c} 코드표에 없음")
    for z in filter(None, (x.strip() for x in str(p.get("zipCd") or "").split(","))):
        if z not in CODES["ZIP"]["codes"]:
            bad.append(f"zipCd={z} 코드표에 없음")
            break
    return bad


def policy_year(p):
    return int((p.get("aplyYmd") or "")[:4] or p.get("frstRegDt", "")[:4] or 0)


def doc_infos(p, docs):
    """정책의 PDF들: 열기 실패/지난 공고 의심/수정본 여부. 수정본·최신 연도·정상 문서가 앞으로."""
    out = []
    for d in docs:
        info = {"path": d["path"], "file": d["file"], "collected_at": d.get("collected_at", ""), "error": "", "fields": {}}
        try:
            t = text_of(S / d["path"])
        except ParseError as e:
            info["error"] = str(e)
            out.append(info)
            continue
        y = doc_year(t[:1500])
        py = policy_year(p)
        info["year"] = y
        info["stale"] = bool(y and py and y < py)  # 문서 앞부분 연도가 정책 연도보다 이전 → 지난 공고 첨부 의심
        info["version"] = bool(VERSION.search(d["file"]))
        info["fields"] = extract_text(t)
        out.append(info)
    return sorted(out, key=lambda i: (bool(i["error"]), i.get("stale", False), not i.get("version", False)))


def merge_field(name, api, docs):
    a_val, a_bad = api.get(name, (None, ""))
    cand = [(d, d["fields"][name]) for d in docs if not d["error"] and d["fields"].get(name, {}).get("value") not in (None, "")]
    pdf = cand[0] if cand else None
    rec = {"value": None, "status": "없음", "source": None, "reason": "", "evidence": "", "doc": None, "page": None,
           "api_value": a_val or None, "pdf_value": pdf[1]["value"] if pdf else None}
    if pdf:
        d, r = pdf
        rec.update(evidence=r["evidence"], doc=d["path"], page=r["page"])
        reasons = [r["reason"]] if r["reason"] else []
        if d.get("stale"):
            reasons.append(f"지난 공고 의심({d['year']}년 문서)")
        if len({str(x[1]["value"]) for x in cand}) > 1:
            reasons.append("첨부 문서끼리 값이 다름")
        pdf_status = "확인필요" if reasons or r["status"] != "정상" else "정상"
    if a_val not in (None, "") and not a_bad:
        rec.update(value=a_val, source="api", status="정상")
        if pdf and str(pdf[1]["value"]) != str(a_val) and pdf_status == "정상":
            rec.update(status="충돌", reason=f"PDF는 {pdf[1]['value']}")
    elif pdf:
        why = [f"API {a_bad}"] if a_bad else []
        rec.update(value=pdf[1]["value"], source="pdf", status=pdf_status, reason=" / ".join(why + reasons))
    elif a_val not in (None, ""):  # API 값이 이상한데 PDF에도 없음 → 값은 주되 확인필요
        rec.update(value=a_val, source="api", status="확인필요", reason=f"API {a_bad}")
    return rec


def aply_state(f):
    """신청 상태: 종료일·시작일을 오늘과 비교 (API의 마감 표시는 갱신이 안 된 경우가 많음)"""
    kind = (f.get("aplyKind") or {}).get("value")
    s, e = f["aplyStart"]["value"], f["aplyEnd"]["value"]
    if kind == "상시":
        return "상시"
    if e and str(e) < TODAY:
        return "마감"
    if s and str(s) > TODAY:
        return "예정"
    if kind == "예산소진":
        return "진행중(예산 소진 시 마감)"
    return "진행중" if s or e else "알수없음"


def main():
    pols = json.loads((S / "policies.json").read_text(encoding="utf-8"))
    man = [json.loads(l) for f in S.glob("manifest*.jsonl") for l in f.read_text(encoding="utf-8").splitlines()]
    by_path = {m["path"]: m for m in man if m.get("path")}
    docs_of, checked = {}, {}
    for m in man:
        if m.get("path"):
            docs_of.setdefault(m["plcyNo"], []).append(m)
        if m.get("checked"):
            checked[m["plcyNo"]] = m
            for path in m.get("linked", []):  # 다른 정책에서 받은 같은 문서
                if path in by_path:
                    docs_of.setdefault(m["plcyNo"], []).append(by_path[path])
    stat, out = Counter(), []
    for p in pols:
        api = api_values(p)
        docs = doc_infos(p, docs_of.get(p["plcyNo"], []))
        fields = {n: merge_field(n, api, docs) for n in NUM_FIELDS + ["aplyKind", "amount", "amountPer"]}
        chk = checked.get(p["plcyNo"], {})
        rec = {"plcyNo": p["plcyNo"], "plcyNm": p["plcyNm"].strip(), "aply_state": aply_state(fields), "fields": fields,
               "codes": {f: p.get(f) for f in list(CODE_FIELDS) + ["zipCd"]}, "code_issues": code_checks(p),
               "docs": [{k: d.get(k) for k in ("path", "file", "year", "stale", "version", "error")} for d in docs],
               "pdf_status": ("수집 실패: " + chk["error"]) if chk.get("error") else (chk.get("no_pdf_reason") or "") if chk else "미확인",
               "collected_at": chk.get("collected_at") or (docs[0]["collected_at"] if docs else "")}
        out.append(rec)
        if docs:
            for n, f in fields.items():
                stat[(n, f["status"], f["source"])] += 1
            stat[("_doc", "지난공고의심", None)] += sum(1 for d in docs if d.get("stale"))
            stat[("_doc", "열기실패", None)] += sum(1 for d in docs if d["error"])
        stat[("_code", "코드이상", None)] += bool(rec["code_issues"])
        stat[("_state", rec["aply_state"], None)] += 1
    (S / "merged.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in out), encoding="utf-8")
    print(f"정책 {len(out)}건 → samples/merged.jsonl  (PDF 있는 정책 {sum(1 for r in out if r['docs'])}건)")
    for k, n in sorted(stat.items(), key=lambda x: (x[0][0], str(x[0][1]), str(x[0][2]))):
        print(f"  {k[0]:11} {str(k[1]):14} {str(k[2] or ''):4} {n}")


if __name__ == "__main__":
    main()
