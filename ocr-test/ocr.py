# PDF → 텍스트. 페이지에 텍스트 레이어가 있으면 PyMuPDF, 없으면(스캔/이미지) PaddleOCR.
# 사용법:
#   python ocr.py            → 한국어 샘플 PDF를 만들어 자체 테스트
#   python ocr.py 공고문.pdf  → 실제 PDF 파싱 결과 출력
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pymupdf

DPI = 200
OCR_TIMEOUT = 300  # 페이지당 초. 넘으면 그 문서의 OCR은 실패 처리 (한 문서가 전체를 막지 않게)
_ocr = None


class ParseError(Exception):
    """열 수 없는 PDF (깨짐 / 암호). 문서 단위로 건너뛰라는 신호."""


def get_ocr():
    global _ocr
    if _ocr is None:  # 모델 로딩이 느려서 처음 필요할 때 한 번만
        from paddleocr import PaddleOCR
        _ocr = PaddleOCR(lang="korean", use_doc_orientation_classify=False, use_doc_unwarping=False,
                         use_textline_orientation=False,
                         enable_mkldnn=True,  # 명시 안 하면 꺼짐 → 6배 느림. paddlepaddle==3.2.2 고정: 3.3은 Windows에서 이게 깨짐
                         text_det_limit_type="max", text_det_limit_side_len=1280)  # 큰 이미지 세그폴트 회피
    return _ocr


# HWP→PDF 공고문의 글머리 기호는 Wingdings 글꼴의 영문자로 들어옴 (m=❍, q=❑ ...). 의미가 같은 표준 기호로 바꿈
WINGDINGS = {"l": "●", "m": "○", "n": "■", "o": "□", "p": "□", "q": "□", "r": "□", "s": "◆", "t": "◆", "u": "◆", "v": "◆",
             "w": "◆", "§": "▪", "¨": "□", "Ÿ": "•", "Ø": "➢", "ü": "✓", "û": "✗", "è": "→", "ç": "←", "é": "↑", "ê": "↓",
             "\x80": "⓪", "\x81": "①", "‚": "②", "ƒ": "③", "„": "④", "…": "⑤", "†": "⑥", "‡": "⑦", "ˆ": "⑧", "‰": "⑨", "Š": "⑩"}


def _char(ch, wing):
    if 0xF000 <= ord(ch) <= 0xF0FF:  # 기호 글꼴의 사용자 정의 영역 ( = Wingdings 'm')
        ch, wing = chr(ord(ch) - 0xF000), True
    if wing:
        return WINGDINGS.get(ch, "")  # 모르는 기호는 버림 (엉뚱한 영문자로 남는 것보다 나음)
    return "" if ch < " " else ch    # 제어문자 제거


def _line_text(l):
    """글자 좌표로 줄 텍스트를 다시 만듦: 기호 변환 + 사라진 띄어쓰기 복원(양쪽 정렬 문단)."""
    chars = [(_char(c["c"], "wingding" in s["font"].lower()), c["bbox"], s["size"]) for s in l["spans"] for c in s["chars"]]
    chars = [c for c in chars if c[0]]
    gaps = sorted(b[1][0] - a[1][2] for a, b in zip(chars, chars[1:]) if a[0] != " " and b[0] != " ")
    base = gaps[len(gaps) // 2] if gaps else 0  # 이 줄의 평소 글자 간격 (자간이 넓은 줄도 있어서 줄마다 따로)
    out = []
    for i, (ch, bb, size) in enumerate(chars):
        if i and ch != " " and out[-1] != " " and bb[0] - chars[i - 1][1][2] > base + 0.25 * size:
            out.append(" ")
        out.append(ch)
    return "".join(out).strip()


def _garbage(text):
    """깨진 텍스트층(스캔본에 붙은 엉터리 글자)인지: 한글/영숫자/일반 기호 비율이 낮으면 True."""
    t = text.replace(" ", "")
    if len(t) < 20:
        return False
    ok = sum(1 for ch in t if "가" <= ch <= "힣" or "ㄱ" <= ch <= "ㆎ" or ch.isascii() or ch in "·※○●□■◆▪•➢✓→←↑↓①②③④⑤⑥⑦⑧⑨⑩「」『』〈〉《》【】～∼’‘“”…")
    return ok / len(t) < 0.6


def _ocr_pages(pngs):
    """OCR을 별도 프로세스에서 실행: 세그폴트·무한대기가 나도 이 프로세스는 안 죽음. 실패하면 (None, 사유)."""
    with tempfile.TemporaryDirectory() as d:
        for i, png in enumerate(pngs):
            Path(d, f"{i:04d}.png").write_bytes(png)
        try:
            r = subprocess.run([sys.executable, __file__, "--ocr-worker", d], capture_output=True,
                               timeout=OCR_TIMEOUT * len(pngs), env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        except subprocess.TimeoutExpired:
            return None, f"OCR 시간 초과({OCR_TIMEOUT * len(pngs)}초)"
        out = Path(d, "result.json")
        if r.returncode != 0 or not out.exists():
            return None, f"OCR 프로세스 비정상 종료(code {r.returncode})"
        return json.loads(out.read_text(encoding="utf-8")), ""


def _ocr_worker(d):
    res = []
    for png in sorted(Path(d).glob("*.png")):
        lines = []
        for r in get_ocr().predict(str(png)):
            for t, s, p in zip(r["rec_texts"], r["rec_scores"], r["rec_polys"]):
                lines.append({"text": t, "score": float(s), "box": p.tolist()})
        res.append(lines)
    Path(d, "result.json").write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")


def _table_cells(t):
    """표 셀. 병합으로 가려진 칸(None)은 가로 병합이면 왼쪽, 세로 병합이면 위 칸 값으로 채움."""
    raw = t.extract()
    xs = sorted({round(c[0]) for c in t.cells if c})  # 열 왼쪽 경계
    cells = []
    for r, row in enumerate(raw):
        out = []
        for c, v in enumerate(row):
            if v is None:
                left = next((t.rows[r].cells[k] for k in range(c - 1, -1, -1) if t.rows[r].cells[k]), None)
                col_x = xs[c] if c < len(xs) else None
                if left and col_x is not None and left[2] > col_x + 1:
                    v = out[-1] if out else ""
                else:
                    v = cells[-1][c] if cells and c < len(cells[-1]) else ""
            out.append((v or "").replace("\n", " ").strip())
        cells.append(out)
    return cells


def _real_table(t):
    """선 몇 개로 된 틀(로고 칸, 도식)을 표로 오인한 경우 거름: 빈 칸(병합 아님)이 너무 많으면 표가 아님."""
    vals = [v for row in t.extract() for v in row if v is not None]
    empty = sum(1 for v in vals if not v.strip()) / max(len(vals), 1)
    return t.row_count > 1 and t.col_count > 1 and not (empty >= 0.7 or (t.row_count <= 2 and empty >= 0.5))


def parse(pdf):
    """pdf: 경로 또는 bytes. 페이지별 {method, png, w, h, lines, blocks, error}
    - 열 수 없으면 ParseError. 페이지 하나가 실패하면 그 페이지만 error를 채우고 계속."""
    try:
        doc = pymupdf.open(pdf) if isinstance(pdf, str) else pymupdf.open(stream=pdf, filetype="pdf")
    except Exception as e:
        raise ParseError(f"열기 실패: {e}") from e
    if doc.needs_pass and not doc.authenticate(""):
        raise ParseError("암호가 걸린 PDF")
    if doc.page_count == 0:
        raise ParseError("페이지 없음")
    zoom = DPI / 72
    pages = []
    for page in doc:
        p = {"method": "pymupdf", "png": b"", "w": 0, "h": 0, "lines": [], "blocks": [], "error": ""}
        pages.append(p)
        try:
            pix = page.get_pixmap(dpi=DPI)
            p.update(png=pix.tobytes("png"), w=pix.w, h=pix.h)
            for b in page.get_text("rawdict")["blocks"]:
                for l in b.get("lines", []):
                    text = _line_text(l)
                    if text:
                        x0, y0, x1, y1 = (v * zoom for v in l["bbox"])
                        p["lines"].append({"text": text, "score": 1.0, "box": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]})
            if p["lines"] and _garbage(" ".join(l["text"] for l in p["lines"])):
                p["lines"], p["method"] = [], "ocr"
                p["error"] = "텍스트층이 깨져 있어 OCR로 대체"
            elif not p["lines"]:
                p["method"] = "ocr"
            if p["method"] == "pymupdf":
                _text_blocks(page, p, zoom)
        except Exception as e:
            p["error"] = f"페이지 처리 실패: {e}"
    todo = [p for p in pages if p["method"] == "ocr" and p["png"]]
    if todo:
        res, err = _ocr_pages([p["png"] for p in todo])
        for i, p in enumerate(todo):
            if res is None:
                p["error"] = (p["error"] + " / " if p["error"] else "") + err
            else:
                p["lines"] = res[i]
                p["blocks"] = group(p["lines"])
    _join_split_tables(pages)
    return pages


def _text_blocks(page, p, zoom):
    # 표는 셀 구조로 따로 뽑고, 표 안의 줄은 일반 묶음에서 뺌 (이미지 페이지는 표 인식 불가)
    lines = p["lines"]
    tables = [t for t in page.find_tables().tables if _real_table(t)]
    trects = [[v * zoom for v in t.bbox] for t in tables]
    inside = lambda l, r: r[0] <= (l["box"][0][0] + l["box"][2][0]) / 2 <= r[2] and r[1] <= (l["box"][0][1] + l["box"][2][1]) / 2 <= r[3]
    keep = [i for i, l in enumerate(lines) if not any(inside(l, r) for r in trects)]
    blocks = [dict(b, members=[keep[m] for m in b["members"]]) for b in group([lines[i] for i in keep])]
    for t, r in zip(tables, trects):
        cells = _table_cells(t)
        rows = [" | ".join(row) for row in cells]
        blocks.append({"text": " ".join(rows), "rows": rows, "table": cells, "score": 1.0, "rect": r,
                       "members": [i for i, l in enumerate(lines) if inside(l, r)]})
    p["blocks"] = [blocks[i] for row in reading_rows(range(len(blocks)), [b["rect"] for b in blocks]) for i in row]


def _join_split_tables(pages):
    """쪽을 넘어간 표: 다음 쪽 맨 위 표가 앞 쪽 마지막 표와 열 수가 같으면 이어진 표로 보고 머리글을 붙여줌."""
    short = lambda blk: len(blk["text"]) < 15  # 쪽 번호·머리말 같은 짧은 글은 무시
    for prev, cur in zip(pages, pages[1:]):
        pa = [x for x in prev["blocks"] if not short(x)]
        cb = [x for x in cur["blocks"] if not short(x)]
        if not (pa and cb and cur["h"]):
            continue
        a, b = pa[-1], cb[0]
        if "table" in a and "table" in b and len(a["table"][0]) == len(b["table"][0]) and b["rect"][1] < cur["h"] * 0.2 \
                and b["table"][0] != a["table"][0]:
            b["table"] = [a["table"][0]] + b["table"]
            b["rows"] = [" | ".join(r) for r in b["table"]]
            b["text"] = " ".join(b["rows"])
            b["continued"] = True  # 앞 쪽에서 이어짐 (첫 행은 앞 쪽 머리글)


# 묶기 기준 (글자 높이 대비 비율). 공고문 레이아웃에 따라 조정하는 손잡이.
ROW_GAP = 2.0    # 같은 줄: 옆 박스와의 가로 간격이 글자 높이의 몇 배까지면 이어진 것으로 볼지
STACK_GAP = 0.6  # 위아래: 세로 간격이 글자 높이의 몇 배까지면 같은 덩어리로 볼지
OVERLAP = 0.5    # 위아래/같은 줄 판정에 필요한 겹침 비율 (작은 쪽 기준)


def group(lines):
    """좌표로 줄을 덩어리로 묶음. [{text, rows:[str], score, rect:[x0,y0,x1,y1], members:[줄 index]}]"""
    rects = [(min(x for x, _ in l["box"]), min(y for _, y in l["box"]),
              max(x for x, _ in l["box"]), max(y for _, y in l["box"])) for l in lines]
    parent = list(range(len(lines)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def overlap(a0, a1, b0, b1):
        return min(a1, b1) - max(a0, b0)  # 음수면 그만큼 떨어져 있음

    for i, a in enumerate(rects):
        for j in range(i + 1, len(rects)):
            b = rects[j]
            h = min(a[3] - a[1], b[3] - b[1])
            w = min(a[2] - a[0], b[2] - b[0])
            vo, ho = overlap(a[1], a[3], b[1], b[3]), overlap(a[0], a[2], b[0], b[2])
            same_row = vo >= OVERLAP * h and -ho <= ROW_GAP * h
            stacked = ho >= OVERLAP * w and -vo <= STACK_GAP * h
            if same_row or stacked:
                parent[root(i)] = root(j)  # ponytail: O(n²) 비교, 페이지당 수백 줄까지는 충분

    blocks = {}
    for i in range(len(lines)):
        blocks.setdefault(root(i), []).append(i)
    out = []
    for members in blocks.values():
        texts = [" ".join(lines[i]["text"] for i in row) for row in reading_rows(members, rects)]
        out.append({"text": " ".join(texts), "rows": texts, "score": min(lines[i]["score"] for i in members),
                    "rect": [min(rects[i][0] for i in members), min(rects[i][1] for i in members),
                             max(rects[i][2] for i in members), max(rects[i][3] for i in members)],
                    "members": members})
    return [out[i] for row in reading_rows(range(len(out)), [b["rect"] for b in out]) for i in row]


def reading_rows(ids, rects):
    """읽는 순서: 세로로 겹치는 것끼리 한 줄(위→아래), 줄 안에서는 왼→오른."""
    rows = []
    for i in sorted(ids, key=lambda i: rects[i][1]):
        r = rects[i]
        row = next((row for row in rows
                    if min(r[3], row["y"][1]) - max(r[1], row["y"][0]) >= OVERLAP * min(r[3] - r[1], row["y"][1] - row["y"][0])), None)
        if row:
            row["ids"].append(i)
        else:
            rows.append({"y": (r[1], r[3]), "ids": [i]})
    return [sorted(row["ids"], key=lambda i: rects[i][0]) for row in rows]


def check_group():
    box = lambda x0, y0, x1, y1: [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
    L = lambda t, *r: {"text": t, "score": 1.0, "box": box(*r)}
    # 카드 두 개가 나란히: 윗줄 라벨 2개, 아랫줄 라벨 2개 → 위아래 짝끼리 묶여야 함
    got = [b["text"] for b in group([L("기업", 0, 0, 40, 20), L("공기업", 200, 0, 260, 20),
                                     L("채용관", 0, 25, 60, 45), L("컨설팅관", 200, 25, 280, 45)])]
    assert got == ["기업 채용관", "공기업 컨설팅관"], got
    # 한 줄이 두 박스로 쪼개짐 → 합쳐져야 함
    assert [b["text"] for b in group([L("남구와 지역대학이", 0, 0, 200, 30), L("함께 여는", 215, 0, 300, 30)])] == ["남구와 지역대학이 함께 여는"]


def make_sample():
    # 텍스트 레이어 없는 '스캔본' 흉내: 글자를 이미지로 구워서 PDF에 넣음
    src = pymupdf.open()
    page = src.new_page()
    page.insert_text((72, 100), "청년 월세 지원 사업\n신청기간: 2026.10.01 ~ 2026.12.31\n지원대상: 만 19세 ~ 34세 무주택 청년",
                     fontsize=18, fontfile="C:/Windows/Fonts/malgun.ttf", fontname="malgun")
    scan = pymupdf.open()
    scan.new_page(width=page.rect.width, height=page.rect.height).insert_image(page.rect, pixmap=page.get_pixmap(dpi=DPI))
    return scan.tobytes()


def check_parse():
    # 기호 글꼴 변환
    assert _char("m", True) == "○" and _char("", False) == "○" and _char("‚", True) == "②" and _char("\x07", False) == ""
    # 띄어쓰기 복원: 자간보다 확실히 넓은 간격에만 공백
    C = lambda ch, x, w=10: {"c": ch, "bbox": (x, 0, x + w, 10)}
    line = {"spans": [{"font": "Batang", "size": 10, "chars": [C("가", 0), C("나", 11), C("다", 30), C("라", 41)]}]}
    assert _line_text(line) == "가나 다라", _line_text(line)
    wide = {"spans": [{"font": "Batang", "size": 10, "chars": [C("가", 0), C("나", 15), C("다", 30)]}]}  # 자간이 고르게 넓음
    assert _line_text(wide) == "가나다"
    # 깨진 텍스트층
    assert _garbage("ÀÌ¹ÌÁö ÆÄÀÏ ¼³¸í ÀÔ´Ï´Ù ÀÌ¹ÌÁö ÆÄÀÏ") and not _garbage("2026년 청년 월세 지원사업 신청자 모집 공고 (제2026-1145호)")
    # 깨진 PDF / 암호 PDF는 ParseError
    for bad in (b"not a pdf", _encrypted_pdf()):
        try:
            parse(bad)
            raise AssertionError("ParseError가 나야 함")
        except ParseError:
            pass


def _encrypted_pdf():
    d = pymupdf.open()
    d.new_page()
    return d.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="u")


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--ocr-worker":
        _ocr_worker(sys.argv[2])
    elif len(sys.argv) > 1:
        for i, p in enumerate(parse(sys.argv[1]), 1):
            print(f"[p{i}] {p['method']}")
            for b in p["blocks"]:
                if "table" in b:
                    print("  [표]\n" + "\n".join("     " + r for r in b["rows"]))
                else:
                    print(f"  {b['score']:.2f}  " + " / ".join(b["rows"]))
    else:
        check_group()
        check_parse()
        got = [l["text"].replace(" ", "") for p in parse(make_sample()) for l in p["lines"]]
        print(got)
        for want in ["청년월세지원사업", "2026.10.01", "34세"]:
            assert any(want in g for g in got), f"못 읽음: {want}"
        print("OK: 한국어 OCR 통과")
