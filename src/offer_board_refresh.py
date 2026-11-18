"""Collect DataSift message-board posts for the Offer-Ready Board properties.

Prints a JSON list of ArtifactData `set` writes for the board's "updates" collection
(https://claude.ai/artifact/1XwSLXEsfR4wtbAEkg9hri). Doc ids are "ds-<message uuid>", so
re-running is idempotent. Read-only against DataSift. The write itself is done by a Claude
session (ArtifactData batch); the page can't call DataSift.

    python src/offer_board_refresh.py [--since 2026-11-01] [--out writes.json]
"""
import argparse
import json
import sys

import pm_text_sender as p

PROPS = {
    "37ef830c-8d23-4c6d-a637-5cf288a555ed": "3946 College Ave",
    "01026d5a-2d1d-4621-8fd4-867fe8ef3b2e": "4821 Brooklyn Ave",
    "91792b18-37a0-4ded-8f71-3acf6269fbbb": "3924 Jackson Ave",
    "d94a93d5-5b2d-4a13-86dd-19ddcbe2cd8a": "4012 Montgall Ave",
    "9588e003-1994-4839-b2f4-9c1861709132": "8212 E 74th Ct",
    "39b103c1-3a15-482a-b1b2-3f0aa798e2e3": "1316 Arrowhead Rdg",
    "a253d8bc-4854-4771-96a0-68567005f1ce": "6901 E 97th St",
    "68f164af-9762-42d0-98d2-5ab316a27565": "2008 NE 13th St",
    "4ac7be3a-e257-4783-8723-5524a97ff249": "3505 SE 45th St",
    "3d6454d2-2ca6-4889-8360-7e47b96ef027": "11725 Troost Ave",
    "7e937c39-a3ad-4417-a9ca-1366a58078e6": "228 S 103rd E Ave",
    "906c330a-fa71-4d42-8552-100cfc3ed2a3": "4924 Agnes Ave",
    "732076a1-01e5-4a4f-afd7-0f5561cd5bb2": "7139 Walrond Ave",
    "37ef830c-8d23-4c6d-a637-5cf288a555ed": "3946 College Ave",
    "57750141-ce71-4bb3-910c-5c98d7793040": "211 High St SE",
    # Offer tracker rows (board "offers" collection, seeded 2026-11-15)
    "17122d99-ab75-4f1b-b2b1-bac67eed5ae6": "1825 Michell Dr",
    "138e4c8e-ca2b-45d6-a5ca-e999b5313ded": "1116 N Saint Clair",
    "43ea74f7-3e2a-47d1-a215-8937fb40ac41": "9904 NW Shaggy Bark Ln",
    "0f8059bf-fb05-40a3-a23f-ae87f5783a19": "4106 Norledge Ave",
    "80d09ea7-e7ff-4cf2-94d7-f4bc0acd752a": "4123 Garfield Ave",
    "37bae544-a7ab-4417-b113-606cec444d21": "1233 Cathy Ln",
    "77b57eb5-2593-4dca-8cf8-b53a03246f3c": "105 Sherlock Ct",
    "d0385d36-f209-4b26-b5dc-215182ad2eed": "9204 Calistoga Ave NE",
    "408637ad-53a2-4c28-8d12-5b21b31281cd": "1008 E Zion Ct",
    "c82673de-1037-4ff3-a22b-947cef359097": "9149 E 46th Pl",
    "5cceb9ec-457b-4a0b-b499-fbb29f961f7d": "309 SW 15th St",
    "2765a304-ff51-4d25-a7d4-c4f075a134fd": "4207 E 35th St",
    "b82c8b30-d524-477d-af19-c99c47a6c902": "8105 NW 28th Ter",
    "6972b6ea-31f1-4ad4-961c-13e674e6621e": "1601 Rancho",
    "5d030b3f-3624-469f-96d7-98abd47b3e5d": "5305 Coble",
    "b09af4a8-a8be-43d1-8025-6af7b42ae30e": "3109 NW 35th Pl",
    "06f1ebe8-c7af-4eea-87bb-29b543bde8e7": "4909 Redbud",
    "50186bfe-6df7-4c39-897a-e62e30228657": "3771 E 82nd",
    "a77bd0ba-a3ae-4332-8ac0-7c27904cb9ad": "9200 E 66th Ter",
    "522b9c30-13f2-45e3-9eef-470bd33455c0": "1720 S Lake",
    "20b6d403-5868-409b-92d9-228632e91e22": "18001 Chestnut Oak Dr",
    "3205a756-fb90-40b6-99b0-1a20017b7fa5": "3803 NE 33rd",
    "90686bf7-db67-4344-a360-75c08c585684": "2840 SW Murray",
    "37e703a9-80d9-41c4-858f-811d1fb5841a": "400 Swallow",
    "99b86f8d-f95c-4edc-bd43-d1fa6e6b5d1f": "4840 Blue Ridge Blvd",
    "4b5cd75f-d261-404f-a315-f0983e66e0da": "5432 Ralston",
}
# The 11/7 analysis posts are already on the board as seeded notes; SMS-responder drafts are noise.
SKIP_PREFIX = ("DEAL ANALYSIS UPDATE", "[auto-reply")


def messages(api, uuid):
    out, path = [], f"/api/internal/property/{uuid}/message/?limit=100"
    for _ in range(20):
        r = api.call(path)
        out += r.get("results") or []
        nxt = r.get("next")
        if not nxt:
            return out
        path = nxt[nxt.index("/api/"):]
    raise RuntimeError(f"message list for {uuid} did not finish paging")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-11-01", help="ISO date; older messages are skipped")
    ap.add_argument("--out", help="write JSON here instead of stdout")
    a = ap.parse_args()
    api = p.Sift(p._env("REISIFT_API_KEY"))
    writes = []
    for uuid in PROPS:
        for m in messages(api, uuid):
            text = (m.get("message") or "").strip()
            if not text or not m.get("uuid") or (m.get("created") or "") < a.since or text.startswith(SKIP_PREFIX):
                continue
            au = m.get("author") or {}
            writes.append({"op": "set", "collection": "updates", "doc_id": f"ds-{m['uuid']}", "data": {
                "p": uuid,
                "kind": "call" if au.get("email", "").startswith("rahaf@") else "note",
                "text": text[:1500],
                "at": m["created"],
                "who": f"{au.get('first_name') or 'DataSift'} (DataSift)",
            }})
    writes.sort(key=lambda w: w["data"]["at"])
    blob = json.dumps(writes, ensure_ascii=False, indent=1)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(blob)
    else:
        sys.stdout.write(blob)
    print(f"{len(writes)} messages from {len(PROPS)} properties since {a.since}", file=sys.stderr)


if __name__ == "__main__":
    main()
