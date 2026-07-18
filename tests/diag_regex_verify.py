"""Verify the PROBATE_NAME_RE / DECEDENT_NAME_RE / PR_ADDRESS_RE fixes
against the real raw text captured live from mopublicnotices.com on
2026-07-17 (see logs/diag_probate_text3c_output.log for the full captures).

Not part of the test suite — run manually:
    .venv/Scripts/python.exe tests/diag_regex_verify.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from notice_parser import (
    NoticeData,
    DECEDENT_NAME_RE,
    PROBATE_NAME_RE,
    PR_ADDRESS_RE,
    _parse_name,
    _parse_pr_address,
)

SAMPLES = {
    "Walker (ID=1236715/1236711)": """The Pulse

IN THE 16TH JUDICIAL CIRCUIT,
JACKSON COUNTY, MISSOURI
PROBATE DIVISION

Case Number: 26P9-PR00893

In the Estate of
JOSEPH LEE WALKER,

Deceased.

NOTICE OF LETTERS OF ADMINISTRATION GRANTED
(Independent Administration)

        To  all  persons  interested  in  the  estate  of  JOSEPH  LEE  WALKER,
Decedent:
        On 08-JULY-2026, the following individual was appointed the personal
representative  of  the  estate  of  JOSEPH  LEE  WALKER,  decedent,  by  the
Probate Division of the Circuit Court of JACKSON COUNTY, Missouri.
        The personal representative's business address is:

BARBI L. WALKER,

1006 W. COX SCHOOL ROAD,
ODESSA, MO 64076
""",
    "Stewart (ID=1236707, cross-state PR address)": """The Pulse

IN THE 16TH JUDICIAL CIRCUIT,

JACKSON COUNTY, MISSOURI

PROBATE DIVISION

Case Number: 26P9-PR00600

In the Estate of
CATHERINE L. STEWART,

Deceased.

NOTICE OF LETTERS OF ADMINISTRATION GRANTED
(Supervised Administration)

        To All Persons Interested in the Estate of  CATHERINE L. STEW-
ART, Decedent:
        On 07-08-2026, the following individual was appointed the personal rep-

resentative  of  the  Estate  of  CATHERINE  L.  STEWART,  decedent,  by  the

Probate Division of the Circuit Court of Jackson County, Missouri.

        The personal representative's business address is:

ASSURED TRUST COMPANY,

10975 GRANDVIEW DRIVE, STE 502,
OVERLAND PARK, KS 66210.
""",
    "Shipley (ID=1236719, unknown heirs, no PR at all)": """The Pulse

IN THE 16TH JUDICIAL CIRCUIT,
JACKSON COUNTY, MISSOURI
PROBATE DIVISION

In the Estate of
ROGER DENNIS SHIPLEY,
Decedent.

Case Number: 26P9-PR00846

Notice of Hearing-Determination of Heirship

        To:
        All unknown heirs of the decedent and all persons known or believed to
claim any interest in the property outlined below as an heir or through an heir
of the decedent.
""",
}


def main():
    for label, text in SAMPLES.items():
        print(f"\n{'=' * 70}")
        print(f"=== {label} ===")
        print(f"{'=' * 70}")

        notice = NoticeData(notice_type="probate", county="Jackson", state="MO")
        notice.raw_text = text
        _parse_name(notice)
        _parse_pr_address(notice)

        print(f"decedent_name : {notice.decedent_name!r}")
        print(f"owner_name    : {notice.owner_name!r}")
        print(f"owner_street  : {notice.owner_street!r}")
        print(f"owner_city    : {notice.owner_city!r}")
        print(f"owner_state   : {notice.owner_state!r}")
        print(f"owner_zip     : {notice.owner_zip!r}")

        # Raw regex matches for extra visibility
        m = PROBATE_NAME_RE.search(text)
        print(f"[PROBATE_NAME_RE raw match] {(m.group(1) if m else None)!r}")
        m = DECEDENT_NAME_RE.search(text)
        print(f"[DECEDENT_NAME_RE raw match] {(m.group(1) if m else None)!r}")
        m = PR_ADDRESS_RE.search(text)
        print(f"[PR_ADDRESS_RE raw match] street={(m.group(1) if m else None)!r} "
              f"city={(m.group(2) if m else None)!r} state={(m.group(3) if m else None)!r} "
              f"zip={(m.group(4) if m else None)!r}")


main()
