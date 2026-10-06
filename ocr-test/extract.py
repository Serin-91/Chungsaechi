# 파싱된 공고문 텍스트에서 규칙(정규식)으로 필드 추출. LLM/토큰 안 씀.
# 모든 값은 {value, status, reason, evidence, page} 로 나옴:
#   status = 정상 | 확인필요 (애매·근사·후보 충돌) | 없음 (못 찾음 → 억지로 채우지 않음)
#   python extract.py 공고문.pdf   → 추출 결과 출력 (인자 없으면 자체 검사만)
import datetime
import json
import re
import sys

SEP = r"\s*[~∼～\-–]\s*"
FULL = r"(?:(\d{4})\s*[.년]|[‘’'](\d{2})\s*\.)\s*(\d{1,2})\s*[.월]\s*(\d{1,2})"  # 2026. 9. 14 / 2026년 3월 23 / ‘26. 4. 1
SHORT = r"(?:(\d{4})\s*[.년]\s*|[‘’'](\d{2})\s*\.\s*)?(\d{1,2})\s*[.월]\s*(\d{1,2})"  # 연도 생략 가능: 10. 16
PERIOD_LABEL = r"(?:신청|접수|모집)[^\n:：]{0,8}?기간\s*\)?\s*[:：]?"
ALWAYS = r"상시|연중|수시"
BUDGET = r"예산\s*(?:소진|범위)|선착순\s*마감|소진\s*시"
# 자격 줄 / 자격이 아닌 줄 (선정순위·우대 문장의 나이는 신청 자격이 아님)
ELIG = re.compile(r"지원\s*대상|신청\s*대상|모집\s*대상|참여\s*대상|신청\s*자격|자격\s*요건|자격\s*조건|대상자?\s*[:：]|연령|나이")
NOT_ELIG = re.compile(r"제외|불가|결격|선정\s*순위|우선\s*순위|순위|우선|우대|가점|가산|배점|연소자|동점")


def rec(value=None, status="없음", reason="", evidence="", pos=None, text=""):
    page = text.count("\f", 0, pos) + 1 if pos is not None else None
    return {"value": value, "status": status, "reason": reason, "evidence": evidence.strip()[:200], "page": page}


def _ymd(y, m, d):
    try:
        return datetime.date(int(y), int(m), int(d)).strftime("%Y%m%d")
    except ValueError:
        return None  # 2월 30일 같은 없는 날짜


def _year(g_full, g_short):
    return int(g_full) if g_full else 2000 + int(g_short) if g_short else None


def doc_year(text):
    m = re.search(r"20[2-3]\d", text)
    return int(m[0]) if m else None


def period_candidates(text):
    """신청기간 후보 전부. 각 후보: (start, end, kind, reason, evidence, pos)"""
    dy = doc_year(text)
    out = []
    for m in re.finditer(PERIOD_LABEL, text):
        tail = text[m.end():m.end() + 140]
        ev = (m.group(0) + tail.split("\n")[0])
        if re.match(r"\s*(?:" + ALWAYS + ")", tail):
            out.append(("", "", "상시", "", ev, m.start()))
            continue
        s = re.match(r"\s*" + FULL + r"[^~∼～\n]{0,20}?" + SEP, tail) or re.match(r"\s*" + SHORT + r"[^~∼～\n]{0,20}?" + SEP, tail)
        anno = re.match(r"\s*공고\s*일\s*(?:로?부터)?" + SEP, tail)
        if s:
            y = _year(s[1], s[2]) or dy
            if not y:
                continue
            start, rest = _ymd(y, s[3], s[4]), tail[s.end():]
            if not start:
                out.append(("", "", "오류", f"없는 시작일 {y}.{s[3]}.{s[4]}", ev, m.start()))
                continue
        elif anno:
            y, start, rest = dy, "", tail[anno.end():]
        else:
            continue
        if re.match(r"\s*(?:" + BUDGET + ")", rest):
            out.append((start, "", "예산소진", "", ev, m.start()))
            continue
        e = re.match(SHORT, rest)
        if not e:
            continue
        ey = _year(e[1], e[2])
        end = _ymd(ey or y, e[3], e[4])
        if not end:
            out.append((start, "", "오류", f"없는 종료일 {e[3]}.{e[4]}", ev, m.start()))
            continue
        reason = ""
        if start and end < start and not ey:  # 12. 1. ~ 1. 31. → 종료일은 다음 해
            end, reason = _ymd(y + 1, e[3], e[4]), "종료일 연도 생략 → 다음 해로 봄"
        if start and end < start:
            out.append((start, end, "오류", "종료일이 시작일보다 빠름", ev, m.start()))
            continue
        out.append((start, end, "공고일부터" if anno else "특정기간", reason or ("시작일은 공고일" if anno else ""), ev, m.start()))
    return out


def period_info(text):
    """{start, end, kind} 각각 rec. kind = 특정기간 | 공고일부터 | 예산소진 | 상시"""
    cands = period_candidates(text)
    good = [c for c in cands if c[2] != "오류"]
    if not good:
        bad = cands[0] if cands else None
        r = rec(status="확인필요" if bad else "없음", reason=bad[3] if bad else "", evidence=bad[4] if bad else "",
                pos=bad[5] if bad else None, text=text)
        return {"start": r, "end": dict(r), "kind": dict(r)}
    s, e, kind, reason, ev, pos = good[0]
    distinct = {(c[0], c[1], c[2]) for c in good}
    status = "정상"
    if len(distinct) > 1:  # 모집기간/신청기간이 서로 다르게 적혀 있음 → 첫 번째를 쓰되 확인 표시
        status, reason = "확인필요", (reason + " / " if reason else "") + f"기간 후보 {len(distinct)}개가 서로 다름"
    elif reason and "다음 해" in reason:
        status = "확인필요"
    mk = lambda v: rec(v or None, status if v else "없음", reason, ev, pos, text)
    return {"start": mk(s), "end": mk(e), "kind": rec(kind, status, reason, ev, pos, text)}


def find_period(text):
    """(시작, 종료, 근거) — 예전 호출부 호환용"""
    p = period_info(text)
    return p["start"]["value"] or "", p["end"]["value"] or "", p["start"]["evidence"]


AGE_RANGE = r"(?:만\s*)?(\d{2})\s*세?\s*(?:이상)?\s*(?:[~∼～\-–]|부터)?\s*(?:만\s*)?(\d{2})\s*세\s*(?:이하|미만|까지)?"
AGE_MAX = r"(?:만\s*)?(\d{2})\s*세\s*(?:\(([^)]{0,30})\)\s*)?(이하|미만)"
AGE_MIN = r"(?:만\s*)?(\d{2})\s*세\s*이상"
BIRTH = r"(\d{4})\s*[.년]\s*(?:\d{1,2}\s*[.월]\s*\d{1,2}\s*[.일]?\s*)?" + SEP + r"(\d{4})\s*[.년]\s*(?:\d{1,2}\s*[.월]\s*\d{1,2}\s*[.일]?\s*)?[^\n]{0,6}?(?:출생|생)"


def age_info(text):
    """{min, max} rec. 자격 줄(지원대상·신청자격·연령)을 먼저 보고, 선정순위·우대 줄은 무시."""
    lines = text.replace("\f", "\n").split("\n")
    offs, o = [], 0
    for l in lines:
        offs.append(o)
        o += len(l) + 1
    elig = [i for i, l in enumerate(lines) if ELIG.search(l)]
    order = elig + list(range(len(lines)))  # 자격 줄 우선, 그다음 전체
    elig, seen = set(elig), set()
    for i in order:
        if i in seen:
            continue
        seen.add(i)
        one_sided_ok = i in elig  # 자격 줄이 아니면 'N세 미만' 같은 한쪽 표현은 안 믿음 (제외대상 목록인 경우가 많음)
        l = lines[i]
        if NOT_ELIG.search(l):
            continue
        pos = offs[i]
        b = re.search(BIRTH, l)
        if b:  # 출생연도 범위 → 기준 연도로 나이 환산 (근사)
            ref = doc_year(text) or datetime.date.today().year
            lo, hi = sorted((int(b[1]), int(b[2])))
            reason = f"출생연도 {lo}~{hi} → {ref}년 기준 나이로 근사"
            return {"min": rec(ref - hi, "확인필요", reason, l, pos, text), "max": rec(ref - lo, "확인필요", reason, l, pos, text)}
        m = re.search(AGE_RANGE, l)
        if m and int(m[1]) < int(m[2]) and (re.search(r"[~∼～\-–]|부터|이상", m[0])):
            return {"min": rec(int(m[1]), "정상", "", l, pos, text), "max": rec(int(m[2]), "정상", "", l, pos, text)}
        if not one_sided_ok:
            continue
        m = re.search(AGE_MAX, l)
        if m:
            mx = int(m[1]) - (1 if m[3] == "미만" else 0)
            st, why = ("확인필요", f"예외 조건: {m[2]}") if m[2] else ("정상", "")
            mn = re.search(AGE_MIN, l)
            return {"min": rec(int(mn[1]), "정상", "", l, pos, text) if mn else rec(),
                    "max": rec(mx, st, why, l, pos, text)}
        m = re.search(AGE_MIN, l)
        if m:
            return {"min": rec(int(m[1]), "정상", "", l, pos, text), "max": rec()}
    return {"min": rec(), "max": rec()}


def find_age(text):
    a = age_info(text)
    return a["min"]["value"], a["max"]["value"], a["min"]["evidence"] or a["max"]["evidence"]


def count_info(text):
    m = (re.search(r"(?:지원|모집|선정)\s*(?:인원|규모|대상|가구)\s*[:：]?\s*(?:총\s*|약\s*)?([\d,]+)\s*(?:명|가구|개사|호|세대)", text)
         or re.search(r"총\s*([\d,]+)\s*(?:명|가구|개사|세대)\s*(?:내외\s*)?(?:지원|선정|모집)", text))
    if not m:
        return rec()
    approx = re.search(r"약|내외|정도|이내", text[m.start():m.end() + 6])
    return rec(int(m[1].replace(",", "")), "확인필요" if approx else "정상", "대략적인 인원" if approx else "", m.group(0), m.start(), text)


def find_count(text):
    c = count_info(text)
    return c["value"], c["evidence"]


UNIT = {"억": 100_000_000, "천만": 10_000_000, "백만": 1_000_000, "만": 10_000, "천": 1_000, "": 1}
AMOUNT = r"(?:(월|연|1인당|인당|1회|회당|최대|총)\s*)*(?:최대\s*)?([\d,]+(?:\.\d+)?)\s*(억|천만|백만|만|천)?\s*원"


def to_won(num, unit):
    return int(round(float(num.replace(",", "")) * UNIT[unit or ""]))


def amount_info(text):
    """지원금액(원 단위로 통일) + 주기. 지원내용·지원금액 줄에서만 찾음."""
    for m in re.finditer(r"[^\n]*(?:지원\s*내용|지원\s*금액|지원\s*액|지원\s*규모|지원금)[^\n]*", text):
        line = m.group(0)
        if NOT_ELIG.search(line) and "지원" not in line[:10]:
            continue
        a = re.search(AMOUNT, line)
        if not a:
            continue
        won = to_won(a[2], a[3])
        per = next((p for p in ("월", "연", "1회", "회당", "1인당", "인당", "총") if re.search(p + r"\s*(?:최대\s*)?[\d,]", line[max(0, a.start() - 8):a.end()])), "")
        status, reason = "정상", ""
        if len({x.group(0) for x in re.finditer(AMOUNT, line)}) > 1:
            status, reason = "확인필요", "한 줄에 금액이 여러 개"
        return {"amount": rec(won, status, reason, line, m.start(), text), "per": rec(per or None, status if per else "없음", reason, line, m.start(), text)}
    return {"amount": rec(), "per": rec()}


def doc_text(pages):
    # 묶음은 한 줄로, 표는 행 단위로. 페이지 사이는 \f (근거의 쪽 번호 계산용)
    return "\f".join("\n".join(r for b in p["blocks"] for r in (b["rows"] if "table" in b else [" ".join(b["rows"])])) for p in pages)


def extract_text(t):
    p, a, am = period_info(t), age_info(t), amount_info(t)
    return {"aplyStart": p["start"], "aplyEnd": p["end"], "aplyKind": p["kind"], "minAge": a["min"], "maxAge": a["max"],
            "sprtSclCnt": count_info(t), "amount": am["amount"], "amountPer": am["per"]}


def extract(pdf):
    from ocr import parse
    return extract_text(doc_text(parse(pdf)))


def check():
    v = lambda d: d["value"]
    P = lambda s: (find_period(s)[0], find_period(s)[1])
    assert P("○ 신청기간 : 2026. 10. 1.(목) ~ 10. 16.(금) 18:00") == ("20261001", "20261016")
    assert P("○(신청기간) ‘26. 4. 1.(수) ∼5. 29.(금)") == ("20260401", "20260529")
    assert P("□ 신청기간: 2026년 3월 23일(월) 10:00 ~ 4월 17일(금)") == ("20260323", "20260417")
    assert P("2026년 공고\n□ 모집기간 : 6. 18. (목) ~ 6. 24. (수)") == ("20260618", "20260624")
    # 해 넘김 / 없는 날짜 / 역순 / 예산소진 / 상시 / 공고일부터
    p = period_info("신청기간 : 2026. 12. 1. ~ 1. 31.")
    assert (v(p["start"]), v(p["end"]), p["end"]["status"]) == ("20261201", "20270131", "확인필요")
    assert period_info("신청기간 : 2026. 2. 30. ~ 3. 5.")["start"]["status"] == "확인필요"
    assert period_info("신청기간 : 2026. 10. 16. ~ 2026. 10. 1.")["start"]["reason"] == "종료일이 시작일보다 빠름"
    p = period_info("o 신청기간 : 2026. 4. 27.(월) ~ 예산소진시까지")
    assert (v(p["start"]), v(p["end"]), v(p["kind"])) == ("20260427", None, "예산소진")
    assert v(period_info("□ 신청기간 : 연중 상시 접수")["kind"]) == "상시"
    p = period_info("2026년\nㅇ (공고 및 모집기간) 공고일로부터 ~ ‘26. 7. 20.(월)")
    assert (v(p["start"]), v(p["end"]), v(p["kind"])) == (None, "20260720", "공고일부터")
    p = period_info("신청기간 : 2026. 3. 2. ~ 3. 20.\n접수기간 : 2026. 4. 1. ~ 4. 10.")
    assert p["start"]["status"] == "확인필요" and v(p["start"]) == "20260302"
    # 나이
    A = lambda s: find_age(s)[:2]
    assert A("부모님과 별도 거주하는 18세~45세 무주택") == (18, 45)
    assert A("만 19세 ~ 만 39세 이하") == (19, 39)
    assert A("지원대상 : 만 19세 이상 39세 이하(1986년생 포함)") == (19, 39)
    a = age_info("2026년 사업\n- 신청 가능 연령 : 1986. 1. 1. ～2008. 12. 31. 출생자")
    assert (v(a["min"]), v(a["max"]), a["min"]["status"]) == (18, 40, "확인필요")
    a = age_info("지원대상 : 만 34세(군필자 최대 39세) 이하")
    assert v(a["max"]) == 34 and a["max"]["status"] == "확인필요" and "군필자" in a["max"]["reason"]
    assert A("지원대상 : 예술인 누구나\n(2차) 34세 이하 대상자 중 연소자순 선정") == (None, None)  # 선정순위 문장은 자격 아님
    assert A("지원대상 : 만 39세 미만 청년") == (None, 38)
    assert A("지원 제외 대상\n- 만 19세 미만 예술인") == (None, None)  # 제목 아래 목록의 한쪽 표현은 무시
    # 인원 / 금액 단위
    assert find_count("○ 지원인원 : 10명")[0] == 10 and find_count("□ 사업개요: 연 1회 시행, 총 18,333명 지원")[0] == 18333
    assert count_info("지원인원 : 약 300명")["status"] == "확인필요"
    assert to_won("20", "만") == 200_000 and to_won("1,000,000", "") == 1_000_000 and to_won("3", "천만") == 30_000_000 and to_won("1.5", "억") == 150_000_000
    am = amount_info("○ 지원내용 : 월 최대 20만원 지원(최대 1년, 생애 1회)")
    assert v(am["amount"]) == 200_000 and v(am["per"]) == "월"
    # 쪽 번호
    assert period_info("1쪽\f2쪽\n신청기간 : 2026. 1. 2. ~ 1. 9.")["start"]["page"] == 2


if __name__ == "__main__":
    check()
    print("check OK")
    if len(sys.argv) > 1:
        for k, r in extract(sys.argv[1]).items():
            print(f"{k:11} {str(r['value']):10} {r['status']:5} p{r['page'] or '-'} {r['reason'][:30]:30} {r['evidence'][:60]}")
