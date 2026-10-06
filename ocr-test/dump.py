import json, ocr
ls = ocr.parse("poster.pdf")[0]["lines"]
json.dump(ls, open("poster_lines.json", "w", encoding="utf-8"), ensure_ascii=False)
for l in ls:
    xs=[p[0] for p in l["box"]]; ys=[p[1] for p in l["box"]]
    print(f"x{min(xs):5.0f}-{max(xs):5.0f} y{min(ys):5.0f}-{max(ys):5.0f} {l['score']:.2f} {l['text']}")
