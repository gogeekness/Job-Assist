#!/usr/bin/env python3
"""
One-off migration: renumber the bullet-bank CSV's IDs from the old
"E###" scheme to Company-Group-Unique (e.g. PeSU-001-001), and insert
one new "-000-001" summation bullet per position. Run once; safe to
re-run only if you restore the pre-migration backup first, since it
assumes the old E### IDs are still present.
"""
import csv
from pathlib import Path

CSV_PATH = Path(__file__).parent.parent / "richard_cv_master_bullet_bank_g_update.csv"

COMPANY_CODE = {
    "Peer Network PSU": "PeSU",
    "Excelgens / Native Teams": "Exms",
    "Herbst Datentechnik GmbH": "HebH",
    "Operational Services GmbH / Modus": "Opus",
    "YOC": "YOOC",
    "Mindtree onsite at Microsoft": "Mift",
    "CompuCom onsite at Amazon": "Coon",
    "Abacus Service Corporation on Intel": "Abel",
    "CompuCom onsite at Intel": "Coel",
}

# old ID -> (group, unique) per employer, in the order decided with the user.
# The Peer duplicate "E001" incident-response row shares group 005 with E005.
ID_PLAN = {
    "Peer Network PSU": [("E001", "001", "001"), ("E002", "002", "001"), ("E003", "003", "001"),
                          ("E004", "004", "001"), ("E005", "005", "001"), ("E001-DUP", "005", "002")],
    "Excelgens / Native Teams": [("E006", "001", "001"), ("E007", "002", "001")],
    "Herbst Datentechnik GmbH": [("E008", "001", "001"), ("E009", "002", "001"), ("E010", "003", "001"),
                                  ("E011", "004", "001"), ("E012", "005", "001"), ("E013", "006", "001")],
    "Operational Services GmbH / Modus": [("E014", "001", "001"), ("E015", "002", "001"), ("E016", "003", "001"),
                                           ("E017", "004", "001"), ("E018", "005", "001"), ("E019", "006", "001"),
                                           ("E020", "007", "001")],
    "YOC": [("E021", "001", "001"), ("E022", "002", "001")],
    "Mindtree onsite at Microsoft": [("E023", "001", "001"), ("E024", "002", "001"), ("E025", "003", "001")],
    "CompuCom onsite at Amazon": [("E026", "001", "001"), ("E027", "002", "001"), ("E028", "003", "001")],
    "Abacus Service Corporation on Intel": [("E029", "001", "001"), ("E030", "002", "001"), ("E031", "003", "001")],
    "CompuCom onsite at Intel": [("E032", "001", "001"), ("E033", "002", "001"), ("E034", "003", "001"),
                                  ("E035", "004", "001")],
}

SUMMATIONS = {
    "Peer Network PSU": "Served as the primary technical point of contact for the company's infrastructure and operations.",
    "Excelgens / Native Teams": "Led incident/escalation response and built cloud test clusters (Azure, AWS, Terraform, Docker Compose, Ansible).",
    "Herbst Datentechnik GmbH": "Administered 500+ production Linux systems, building automation, monitoring, CI/CD, and secure repository infrastructure.",
    "Operational Services GmbH / Modus": "Administered Linux systems and built Ansible-driven automation, CI/CD, and monitoring-data tooling across 50+ servers.",
    "YOC": "Troubleshot production services and automated deployments across on-prem and cloud systems with Ansible.",
    "Mindtree onsite at Microsoft": "Delivered Tier 3 Linux escalation support and Azure operations, coordinating with vendors including Red Hat, SUSE, and Canonical.",
    "CompuCom onsite at Amazon": "Managed NetBackup storage clusters and backup automation supporting global EC2 infrastructure, and led an LDAP/regionalization migration across 500+ servers.",
    "Abacus Service Corporation on Intel": "Administered Intel's Green500-ranked Atlantis HPC cluster, handling hardware operations and stakeholder requests.",
    "CompuCom onsite at Intel": "Supported thousands of Linux/Windows systems and resolved network/hardware issues, including a data-center decommission project and security training for 2,000+ employees.",
}


def main():
    with open(CSV_PATH, encoding="utf-8") as f:
        rows = list(csv.reader(f))
    banner, header = rows[0], rows[1]

    id_col = next(i for i, h in enumerate(header) if h.strip().lower() == "id")
    emp_col = next(i for i, h in enumerate(header) if h.strip().lower() == "employer")
    cat_col = next(i for i, h in enumerate(header) if h.strip().lower() == "category")
    imp_col = next(i for i, h in enumerate(header) if h.strip().lower() == "imp.")
    anchor_col = next(i for i, h in enumerate(header) if "anchor" in h.lower())
    base_col = next(i for i, h in enumerate(header) if h.strip().lower() == "base bullet")
    title_col = next(i for i, h in enumerate(header) if h.strip().lower() == "job title")
    period_col = next(i for i, h in enumerate(header) if h.strip().lower() == "period")
    group_col = next((i for i, h in enumerate(header) if h.strip().lower() == "bullet group"), None)

    # drop the now-superseded free-text "Bullet Group" column entirely
    if group_col is not None:
        del header[group_col]
        del banner[group_col]

    data = [r for r in rows[2:] if any(r)]

    # find and tag the Peer duplicate "E001" (incident-response) row -- it's
    # the *second* row whose ID column reads E001
    seen_e001 = False
    old_id_of = []
    for row in data:
        rid = row[id_col] if id_col < len(row) else ""
        if rid == "E001":
            if seen_e001:
                old_id_of.append("E001-DUP")
            else:
                old_id_of.append("E001")
                seen_e001 = True
        else:
            old_id_of.append(rid)

    new_rows = []
    for row, old_id in zip(data, old_id_of):
        row = row[:]
        if group_col is not None and len(row) > group_col:
            del row[group_col]
        emp = row[emp_col] if emp_col < len(row) else ""
        plan = {oid: (g, u) for oid, g, u in ID_PLAN.get(emp, [])}
        if old_id in plan:
            group, unique = plan[old_id]
            code = COMPANY_CODE.get(emp)
            if code and id_col < len(row):
                row[id_col] = f"{code}-{group}-{unique}"
        new_rows.append(row)

    # insert one new "-000-001" summation row per position, right before
    # that position's first real bullet (keeps the CSV grouped visually)
    final_rows = []
    inserted = set()
    for row in new_rows:
        emp = row[emp_col] if emp_col < len(row) else ""
        if emp in SUMMATIONS and emp not in inserted:
            code = COMPANY_CODE[emp]
            title = row[title_col] if title_col < len(row) else ""
            period = row[period_col] if period_col < len(row) else ""
            summ_row = [""] * len(header)
            summ_row[emp_col] = emp
            summ_row[id_col] = f"{code}-000-001"
            summ_row[title_col] = title
            summ_row[period_col] = period
            summ_row[cat_col] = "summation"
            summ_row[imp_col] = "5"
            summ_row[anchor_col] = "★"
            summ_row[base_col] = SUMMATIONS[emp]
            final_rows.append(summ_row)
            inserted.add(emp)
        final_rows.append(row)

    with open(CSV_PATH, "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows([banner, header] + final_rows)

    print(f"Migrated {len(new_rows)} bullets, inserted {len(inserted)} summation rows, "
          f"dropped 'Bullet Group' column: {group_col is not None}")


if __name__ == "__main__":
    main()
