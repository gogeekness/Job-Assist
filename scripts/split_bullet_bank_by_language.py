#!/usr/bin/env python3
"""
One-off migration: split the single richard_cv_master_bullet_bank_g_update.csv
(English content + bolted-on "German Variant N" columns) into two
same-shaped, single-language files:

    richard_cv_master_bullet_bank_en.csv
    richard_cv_master_bullet_bank_de.csv

Both files share the same header/column layout and the same IDs -- an ID
present in both files is the same achievement, English text in one, German
in the other (cv_bank.py merges them back into one bullet at load time).
The old file is left in place, untouched, as a reference/backup.
"""
import csv
from pathlib import Path

SRC = Path(__file__).parent.parent / "richard_cv_master_bullet_bank_g_update.csv"
OUT_EN = SRC.parent / "bullet_bank_en.csv"
OUT_DE = SRC.parent / "bullet_bank_de.csv"

# shared (non-language) columns kept in both files, by header name
SHARED = ["Employer", "ID", "Job Title", "Period", "Category", "Imp.",
          "Anchor\n(Always Include)", "Core Skill Tags", "Source CV Family"]
NEW_HEADER = SHARED + ["Base Bullet", "Variation 1 — Linux Admin",
                        "Variation 2 — DevOps", "Variation 3 — Ops / SRE",
                        "Verb Risk", "JD Keyword Notes"]


def main():
    with open(SRC, encoding="utf-8") as f:
        rows = list(csv.reader(f))
    banner, header = rows[0], rows[1]
    data = [r for r in rows[2:] if any(r)]

    def col(name):
        return next((i for i, h in enumerate(header) if h == name), None)

    shared_idx = [col(h) for h in SHARED]
    en_idx = {"base": col("Base Bullet"), "v1": col("Variation 1 — Linux Admin"),
              "v2": col("Variation 2 — DevOps"), "v3": col("Variation 3 — Ops / SRE"),
              "verb_risk": col("Verb Risk"), "jd": col("JD Keyword Notes")}
    de_idx = {"v1": col("German Variant 1 (DE)"), "v2": col("German Variant 2 (DE)")}
    # the header has "German Variant 2 (DE)" twice and "German Variant 3 (DE)" once --
    # take them positionally in header order, skipping the first two we already used
    de_cols = [i for i, h in enumerate(header) if h.startswith("German Variant")]
    de_idx["v3"] = de_cols[3] if len(de_cols) > 3 else None  # "German Variant 3 (DE)"

    def get(row, idx):
        return row[idx] if idx is not None and idx < len(row) else ""

    en_rows, de_rows = [], []
    for row in data:
        shared_vals = [get(row, i) for i in shared_idx]
        en_rows.append(shared_vals + [
            get(row, en_idx["base"]), get(row, en_idx["v1"]),
            get(row, en_idx["v2"]), get(row, en_idx["v3"]),
            get(row, en_idx["verb_risk"]), get(row, en_idx["jd"]),
        ])
        de_rows.append(shared_vals + [
            "",  # no German "Base" existed -- left blank for the user to fill in
            get(row, de_idx["v1"]), get(row, de_idx["v2"]), get(row, de_idx["v3"]),
            get(row, en_idx["verb_risk"]), get(row, en_idx["jd"]),  # shared, not language-specific
        ])

    en_banner = list(banner[:len(NEW_HEADER)]) + [""] * max(0, len(NEW_HEADER) - len(banner))
    en_banner = en_banner[:len(NEW_HEADER)]
    en_banner[0] = (banner[0] if banner else "") + " (English)"
    de_banner = list(en_banner)
    de_banner[0] = (banner[0] if banner else "") + " (German)"

    with open(OUT_EN, "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows([en_banner, NEW_HEADER] + en_rows)
    with open(OUT_DE, "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows([de_banner, NEW_HEADER] + de_rows)

    print(f"Wrote {len(en_rows)} rows -> {OUT_EN.name}")
    print(f"Wrote {len(de_rows)} rows -> {OUT_DE.name}")
    de_missing_base = sum(1 for r in de_rows if not r[len(SHARED)])
    de_missing_all = sum(1 for r in de_rows if not any(r[len(SHARED):]))
    print(f"German rows needing a Base Bullet: {de_missing_base} (all of them -- none existed before)")
    print(f"German rows with NO content at all yet (blank across the board): {de_missing_all}")


if __name__ == "__main__":
    main()
