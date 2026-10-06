# samples/ 의 공고 PDF에서 규칙으로 컬럼을 뽑아 온통청년 API 값과 비교. LLM/토큰 안 씀.
#   python evaluate.py            → 전체 문서, 컬럼별 정확도 (규칙 vs '항상 최빈값' 기준선)
#   python evaluate.py 2          → 묶음 2 (21~40번 문서)만
#   python evaluate.py 2 -v 컬럼  → 그 묶음에서 그 컬럼이 틀린 문서 목록
import json
import re
import sys
from collections import Counter
from pathlib import Path

from extract import find_age, find_count, find_period, doc_text

HERE = Path(__file__).parent
S = HERE / "samples"
CODES = json.loads((HERE / "codes.json").read_text(encoding="utf-8"))
BATCH = 20
ZIP = CODES["ZIP"]["codes"]  # {'30110': {'ctpv': '대전광역시', 'sgg': '대전광역시 동구'}}


def text_of(pdf):
    # 파싱 결과 캐시 (이미지 PDF는 OCR이 느려서 한 번만)
    cache = S / "cache" / (pdf.stem + ".txt")
    if not cache.exists():
        from ocr import parse
        cache.parent.mkdir(exist_ok=True)
        cache.write_text(doc_text(parse(str(pdf))), encoding="utf-8")
    return cache.read_text(encoding="utf-8")


def has(t, pat):
    return re.search(pat, t) is not None


# --- 코드 컬럼 규칙 (키워드). 해당 없으면 '제한없음/무관/기타' ---
MULTI = {
    "jobCd": [("0013003", r"미취업|구직자|실업"), ("0013001", r"재직\s*중|재직자|근로자(?!의 날)"),
              ("0013006", r"예비\s*창업|창업자|창업\s*\d+년"), ("0013002", r"자영업|소상공인"),
              ("0013004", r"프리랜서"), ("0013008", r"영농|농업인|어업인|임업인"), ("0013005", r"일용"), ("0013007", r"단기\s*근로")],
    "schoolCd": [("0049005", r"대학\s*재학|재학생|대학생"), ("0049006", r"졸업\s*예정"), ("0049007", r"대학\s*졸업|졸업자"),
                 ("0049004", r"고교\s*졸업|고등학교\s*졸업"), ("0049002", r"고등학생|고교\s*재학"), ("0049008", r"석사|박사|대학원")],
    "sbizCd": [("0014001", r"중소기업"), ("0014002", r"여성"), ("0014003", r"기초생활\s*수급|수급자"), ("0014004", r"한부모"),
               ("0014005", r"장애인"), ("0014006", r"농업인"), ("0014007", r"군인|장병|병사"), ("0014008", r"지역\s*인재")],
    "plcyMajorCd": [("0011006", r"예체능|예술\s*전공|체육\s*전공"), ("0011005", r"공학\s*계열|이공계"), ("0011001", r"인문\s*계열")],
}
NONE = {"jobCd": "0013010", "schoolCd": "0049010", "sbizCd": "0014010", "plcyMajorCd": "0011009"}


ELIG = re.compile(r"지원\s*대상|신청\s*대상|모집\s*대상|참여\s*대상|신청\s*자격|자격\s*요건|자격\s*조건|대상자?\s*[:：]")
EXCL = re.compile(r"제외|불가|결격|제한\s*대상|해당하는\s*자는|우대|가점|가산")


def elig_text(t):
    # 자격 줄만: '지원대상/신청자격' 이 들어간 줄. 제외·우대 문장은 뺌 (본문 전체에서 찾으면 설명문 단어가 걸림)
    return "\n".join(l for l in t.splitlines() if ELIG.search(l) and not EXCL.search(l[:80]))


def rule_codes(t):
    out = {}
    el = elig_text(t)
    for col, rules in MULTI.items():
        hit = [c for c, p in rules if has(el, p)]
        out[col] = ",".join(sorted(hit)) or NONE[col]
    # 혼인: '미혼'만 명시 → 미혼, 신혼부부/기혼 대상 → 기혼(둘 다 있으면 제한없음)
    mi, gi = has(el, r"미혼"), has(el, r"신혼부부|기혼|혼인\s*\d+년")
    out["mrgSttsCd"] = "0055002" if mi and not gi else "0055001" if gi and not mi else "0055003"
    # 소득: 연소득/총급여 금액 → 연소득, 중위소득 등 → 기타, 없으면 무관
    out["earnCndSeCd"] = ("0043002" if has(t, r"연\s*소득|연간\s*소득|총\s*급여|연\s*매출") else
                          "0043003" if has(t, r"중위\s*소득|소득\s*인정액|건강보험료|소득\s*\d+분위") else "0043001")
    s, e, ev = find_period(t)
    # '마감'(0057003)은 API에서 날짜와 무관하게 쓰여서(종료일 지나도 특정기간으로 남은 경우 다수) 규칙으로 안 뽑음
    out["aplyPrdSeCd"] = ("0057001" if s else
                          "0057002" if has(t, r"(?:신청|접수|모집)\s*기간\s*[:：]?\s*(?:상시|연중|수시)") else "0057001")
    out["bizPrdSeCd"] = "0056001" if has(t, r"사업\s*기간\s*[:：]?\s*\d{4}") else "0056002"
    out["plcyPvsnMthdCd"] = ("0042006" if has(t, r"이자\s*(?:비용\s*)?지원|이자\s*차액") else "0042003" if has(t, r"융자|대출(?!\s*이자)") else "0042010" if has(t, r"바우처|이용권") else
                             "0042009" if has(t, r"세액\s*공제|비과세|감면") else "0042002" if has(t, r"교육\s*과정|프로그램|아카데미|멘토링|캠프") else
                             "0042006" if has(t, r"지원금|수당|보조금|월세|임차료|이자\s*지원") else "0042013")
    out["zipCd"] = ",".join(sorted(rule_zip(t)))
    return out


CTPV_ALIAS = {  # 코드표 시도명 → 공고문에 실제로 쓰이는 표기
    "서울특별시": r"서울", "부산광역시": r"부산", "대구광역시": r"대구", "인천광역시": r"인천", "대전광역시": r"대전",
    "울산광역시": r"울산", "세종특별자치시": r"세종", "경기도": r"경기", "강원특별자치도": r"강원", "충청북도": r"충북|충청북도",
    "충청남도": r"충남|충청남도", "전북특별자치도": r"전북|전라북도", "전남광주통합특별시": r"전남|전라남도|광주",
    "경상북도": r"경북|경상북도", "경상남도": r"경남|경상남도", "제주특별자치도": r"제주"}
assert set(CTPV_ALIAS) == set(CODES["ZIP"]["ctpv"].values()), "코드표 시도명이 바뀜"


def rule_zip(t):
    # 공고문 앞부분의 기관명/거주요건에서 시군구 → 없으면 시도 전체 → 없으면 전국
    head = t[:3000]
    sgg = {c for c, z in ZIP.items() if z["sgg"].split()[-1] in head and len(z["sgg"].split()[-1]) >= 2}
    # 시도: 앞부분(기관명·제목)에서 가장 많이 나온 시도 하나. 공고문은 '광주광역시', '전남' 등으로 적음
    cnt = Counter({c: len(re.findall(p, head[:1500])) for c, p in CTPV_ALIAS.items()})
    ctpvs = {cnt.most_common(1)[0][0]} if cnt and cnt.most_common(1)[0][1] else set()
    if ctpvs:  # 시도를 알면 그 시도 안의 시군구만 인정 (동명 '고성군', '중구' 등 정리)
        sgg = {c for c in sgg if ZIP[c]["ctpv"] in ctpvs} or {c for c, z in ZIP.items() if z["ctpv"] in ctpvs}
    elif len(sgg) > 3:  # 시도 단서 없이 여러 지역명 → 오탐 가능성이 높아 전국 처리
        sgg = set()
    return sgg or set(ZIP)


def api_fields(p):
    a_start, _, a_end = (p.get("aplyYmd") or "").partition(" ~ ")
    return {"aplyStart": a_start.strip(), "aplyEnd": a_end.strip(),
            "minAge": int(p["sprtTrgtMinAge"] or 0) or None, "maxAge": int(p["sprtTrgtMaxAge"] or 0) or None,
            "sprtSclCnt": int(p["sprtSclCnt"] or 0) or None,
            **{k: ",".join(sorted(x.strip() for x in str(p.get(k) or "").split(",") if x.strip()))
               for k in list(MULTI) + ["mrgSttsCd", "earnCndSeCd", "aplyPrdSeCd", "bizPrdSeCd", "plcyPvsnMthdCd", "zipCd"]}}


def rule_fields(t):
    s, e, _ = find_period(t)
    amin, amax, _ = find_age(t)
    cnt, _ = find_count(t)
    return {"aplyStart": s, "aplyEnd": e, "minAge": amin or None, "maxAge": amax, "sprtSclCnt": cnt, **rule_codes(t)}


def score(got, want, col):
    if col == "zipCd":  # 집합 비교: 완전 일치만 정답 (부분 일치율은 따로)
        g, w = set(got.split(",")), set(want.split(","))
        return g == w, len(g & w) / len(g | w) if g | w else 1
    return str(got) == str(want), None


def main():
    pols = {p["plcyNo"]: p for p in json.loads((S / "policies.json").read_text(encoding="utf-8"))}
    man = [json.loads(l) for f in S.glob("manifest*.jsonl") for l in f.read_text(encoding="utf-8").splitlines()]
    docs = sorted((m for m in man if m.get("path") and (S / m["path"]).exists()), key=lambda m: (S / m["path"]).stat().st_mtime)  # 받은 순서 = 묶음 순서
    args = [a for a in sys.argv[1:]]
    verbose = args[args.index("-v") + 1] if "-v" in args else None
    batch = next((int(a) for a in args if a.isdigit()), None)  # 묶음 번호: 1 → 1~20번 문서
    if batch:
        docs = docs[(batch - 1) * BATCH: batch * BATCH]
        print(f"묶음 {batch}: {(batch - 1) * BATCH + 1}~{(batch - 1) * BATCH + len(docs)}번 문서")
    rows = []
    from ocr import ParseError
    for m in docs:
        try:
            t = text_of(S / m["path"])
        except ParseError as ex:  # 깨진·암호 PDF는 건너뛰고 표시
            print(f"  건너뜀 {m['path']}: {ex}")
            continue
        rows.append((m, rule_fields(t), api_fields(pols[m["plcyNo"]])))
    cols = list(rows[0][2])
    print(f"문서 {len(rows)}개 (빈 API 값은 그 컬럼 평가에서 제외)\n")
    print(f"{'컬럼':15} {'평가 n':>6} {'규칙':>7} {'기준선':>7}  기준선 값")
    for col in cols:
        pairs = [(g[col], w[col], m) for m, g, w in rows if w[col] not in ("", None)]
        if not pairs:
            continue
        ok = [score(g, w, col)[0] for g, w, _ in pairs]
        base_val, base_n = Counter(w for _, w, _ in pairs).most_common(1)[0]
        extra = ""
        if col == "zipCd":
            extra = f"  (부분일치 평균 {sum(score(g, w, col)[1] for g, w, _ in pairs) / len(pairs):.0%})"
        bv = base_val if len(str(base_val)) < 20 else f"{str(base_val)[:16]}…({len(str(base_val).split(','))}개)"
        print(f"{col:15} {len(pairs):6} {sum(ok) / len(pairs):7.0%} {base_n / len(pairs):7.0%}  {bv}{extra}")
        if col == verbose:
            for (g, w, m), o in zip(pairs, ok):
                if not o:
                    print(f"    ✗ {m['path']}  규칙={str(g)[:40]}  API={str(w)[:40]}  {m['file'][:40]}")


if __name__ == "__main__":
    main()
