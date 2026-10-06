# 온통청년에서 공고 PDF를 모음 → samples/ (PDF, policies.json, manifest*.jsonl)
#   python collect.py 100        → PDF 공고 100개 모일 때까지
#   python collect.py 100 0 4    → 병렬: 작업자 4개 중 0번 (정책을 번갈아 나눠 맡음, 기록은 manifest_0.jsonl)
# 기록(정책마다 한 줄): 첨부 목록, 건너뛴 이유, PDF가 없는 이유, 실패 사유, 수집일. 실패한 정책은 다음 실행 때 다시 시도.
import datetime, json, os, re, sys, time, urllib.request, http.cookiejar
from pathlib import Path

B = "https://www.youthcenter.go.kr"
OUT = Path(__file__).parent / "samples"
ENV = Path(__file__).parent / ".env"
KEY = (dict(l.split("=", 1) for l in ENV.read_text().split() if "=" in l) if ENV.exists() else {}).get("YOUTH_API_KEY")
SKIP = re.compile(r"신청서|위임장|서식|동의서|확약서|양식|계획서|확인서|별지|체크리스트|리플렛|리플릿|브로슈어|브로셔|포스터|카드뉴스|홍보물|안내책자|책자|매뉴얼|가이드북|\d권\)|명단|결과|합격|FAQ|Q&A|질의")
MAX_BYTES = 15_000_000

op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
op.addheaders = [("Referer", B + "/youthPolicy/ythPlcyTotalSearch"), ("User-Agent", "Mozilla/5.0")]


def get(url, raw=False, tries=3):
    for i in range(tries):
        try:
            data = op.open(url if url.startswith("http") else B + url, timeout=60).read()
            return data if raw else json.loads(data)
        except Exception:  # 일시 오류는 잠깐 쉬고 재시도, 3번 실패하면 호출한 쪽(정책 하나)만 실패
            if i == tries - 1:
                raise
            time.sleep(3)


def load_policies():
    path = OUT / "policies.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    if not KEY:
        sys.exit(f"{ENV} 에 YOUTH_API_KEY=... 가 필요함")
    policies, page = [], 1
    while True:
        r = get(f"{B}/go/ythip/getPlcy?apiKeyNm={KEY}&pageNum={page}&pageSize=500&rtnType=json")["result"]
        policies += r["youthPolicyList"]
        if len(policies) >= r["pagging"]["totCount"] or not r["youthPolicyList"]:
            break
        page += 1
    path.write_text(json.dumps(policies, ensure_ascii=False), encoding="utf-8")
    return policies


def check_policy(p, names, today, fp):
    rec = {"plcyNo": p["plcyNo"], "checked": True, "collected_at": today, "attach": [], "skipped": [], "linked": []}
    got = 0
    try:
        d = json.dumps(get(f"/wrk/yrm/plcyInfo/plcy/{p['plcyNo']}?user=true&isMaskingYn=Y"))
        time.sleep(0.3)
        m = re.search(r'"atchFileMngSn": "?(\d+)', d)
        files = get(f"/sur/com/atchFile/atchFileDet?atchFileMngSn={m.group(1)}")["result"]["atchFileDetList"] if m and m.group(1) != "0" else []
        for f in files:
            nm, uri, ext, size = f["exsFileNm"], f"{f['atchFileMngSn']}/{f['atchFileSn']}", f["atchFileExtnNm"].lower(), int(f["atchFileSz"])
            rec["attach"].append({"name": nm, "ext": ext, "kb": size // 1024})
            why = "PDF 아님" if ext != "pdf" else "서식·홍보물 등 제외 대상" if SKIP.search(nm) else "15MB 초과" if size > MAX_BYTES else ""
            if why:
                rec["skipped"].append({"name": nm, "why": why})
                continue
            if nm in names:  # 다른 정책에서 이미 받은 같은 문서 → 다시 받지 않고 연결만
                rec["linked"].append(names[nm])
                continue
            get(f"/sur/com/atchFile/atchFileDetInfoCheck/{uri}")
            data = get(f"/sur/com/atchFile/atchFileDetInfo/{uri}", raw=True)
            if not data.startswith(b"%PDF"):
                rec["skipped"].append({"name": nm, "why": f"PDF가 아닌 응답: {data[:60]!r}"})
                continue
            path = OUT / f"{p['plcyNo']}_{f['atchFileSn']}.pdf"
            path.write_bytes(data)
            names[nm] = path.name
            fp.write(json.dumps({"plcyNo": p["plcyNo"], "file": nm, "path": path.name, "kb": len(data) // 1024,
                                 "collected_at": today}, ensure_ascii=False) + "\n")
            got += 1
            time.sleep(1)
        exts = {a["ext"] for a in rec["attach"]}
        rec["no_pdf_reason"] = ("" if "pdf" in exts else "첨부 없음" if not exts else
                                "HWP·HWPX만" if exts <= {"hwp", "hwpx"} else "PDF 없음(" + ",".join(sorted(exts)) + ")")
    except Exception as e:  # 이 정책만 실패로 남기고 계속
        rec["error"] = f"{type(e).__name__}: {e}"
        print(f"  실패 {p['plcyNo']}: {rec['error']}", flush=True)
    fp.write(json.dumps(rec, ensure_ascii=False) + "\n")
    fp.flush()
    return got


def main(target, worker=0, workers=1):
    OUT.mkdir(exist_ok=True)
    policies = load_policies()
    man = OUT / (f"manifest_{worker}.jsonl" if workers > 1 else "manifest.jsonl")
    done = [json.loads(l) for f in OUT.glob("manifest*.jsonl") for l in f.read_text(encoding="utf-8").splitlines()]
    seen = {d["plcyNo"] for d in done if d.get("checked") and not d.get("error")}  # 실패했던 정책은 다시 시도
    names = {d["file"]: d["path"] for d in done if d.get("path")}
    n_pdf = lambda: len(list(OUT.glob("*.pdf")))  # 작업자 전체 합계
    pid = OUT / f"collect_{worker}.pid"
    pid.write_text(str(os.getpid()))  # 중지 명령이 안 먹을 때 이 PID로 직접 종료
    today = datetime.date.today().isoformat()
    try:
        get("/", raw=True)  # 세션 쿠키
        print(f"정책 {len(policies)}건, 이미 받은 PDF {n_pdf()}개, 작업자 {worker}/{workers}", flush=True)
        with man.open("a", encoding="utf-8") as fp:
            for i, p in enumerate(policies):
                if n_pdf() >= target:
                    break
                if i % workers == worker and p["plcyNo"] not in seen and check_policy(p, names, today, fp):
                    print(f"[{n_pdf()}] {i}/{len(policies)} {p['plcyNm'][:40]}", flush=True)
    finally:
        pid.unlink(missing_ok=True)
    print("끝: PDF", n_pdf(), flush=True)


if __name__ == "__main__":
    main(*(int(a) for a in sys.argv[1:4])) if len(sys.argv) > 1 else main(100)
