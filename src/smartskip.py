"""SmartSkip bulk skip trace client + vertical-export parser, slimmed from the deep-prospecting-v5
plugin's smartskip_trace.py / parse_smartskip.py (the plugin isn't in the repo, so GitHub Actions
can't import it). API contract verified live 2026-07-29 in that plugin.

Flow: signin -> upload CSV -> map columns -> calculate (free) -> pay (BILLS the saved card,
$0.15 per row that returns data) -> poll -> download the vertical export -> parse().

Auth: env SMARTSKIP_EMAIL / SMARTSKIP_PASSWORD. Tokens stay in memory (access ~15 min, refreshed).
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid

API = "https://api.smartskip.io"
COST_PER_ROW = 0.15
# CSV headers we send; SmartSkip's field ids
SCHEMA = {"firstName": "First Name", "lastName": "Last Name", "mailingAddress": "Mailing Address",
          "mailingCity": "Mailing City", "mailingState": "Mailing State", "mailingZip": "Mailing Zip",
          "propertyAddress": "Property Address", "propertyCity": "Property City",
          "propertyState": "Property State", "propertyZip": "Property Zip"}
REL = {"child": "Child", "parent": "Parent", "sibling": "Sibling", "spouse": "Spouse",
       "in-law": "In-Law", "in law": "In-Law", "inlaw": "In-Law"}


class SmartSkipError(RuntimeError):
    pass


def _http(method, path, token=None, body=None, raw=None, headers=None, timeout=120):
    h = {"Accept": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    if body is not None:
        raw = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(API + path, data=raw, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
            return r.status, (json.loads(data.decode()) if "json" in r.headers.get("content-type", "") else data)
    except urllib.error.HTTPError as e:
        data = e.read()
        try:
            return e.code, json.loads(data.decode())
        except ValueError:
            return e.code, data.decode(errors="replace")


class Session:
    def __init__(self, email: str | None = None, password: str | None = None):
        self.email = email or os.environ.get("SMARTSKIP_EMAIL", "")
        self.password = password or os.environ.get("SMARTSKIP_PASSWORD", "")
        self.tokens: dict = {}

    def signin(self):
        if not (self.email and self.password):
            raise SmartSkipError("SMARTSKIP_EMAIL / SMARTSKIP_PASSWORD not set")
        code, data = _http("POST", "/auth/signin", body={"email": self.email, "password": self.password})
        if code != 200:
            raise SmartSkipError(f"signin failed ({code}): {str(data)[:200]}")
        self.tokens = {"access": data["accessToken"], "refresh": data["refreshToken"]}

    def _refresh(self) -> bool:
        if not self.tokens.get("refresh"):
            return False
        code, data = _http("GET", "/auth/refresh", token=self.tokens["refresh"])
        if code == 200 and isinstance(data, dict) and data.get("accessToken"):
            self.tokens = {"access": data["accessToken"], "refresh": data.get("refreshToken", self.tokens["refresh"])}
            return True
        return False

    def call(self, method, path, ok=(200, 201), **kw):
        if not self.tokens.get("access"):
            self.signin()
        code, data = _http(method, path, token=self.tokens["access"], **kw)
        if code in (401, 403):
            if not self._refresh():
                self.signin()
            code, data = _http(method, path, token=self.tokens["access"], **kw)
        if code not in ok:
            raise SmartSkipError(f"{method} {path} -> {code}: {str(data)[:300]}")
        return data


def build_csv(rows: list[dict]) -> bytes:
    """rows: dicts keyed by SCHEMA's CSV headers."""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(SCHEMA.values()), extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k) or "" for k in SCHEMA.values()})
    return buf.getvalue().encode()


def submit(sess: Session, csv_bytes: bytes, name="obit_dp.csv") -> dict:
    """Upload + map + calculate. Free. Returns {bulkSkipId, entities}."""
    boundary = uuid.uuid4().hex
    raw = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
           f"Content-Type: text/csv\r\n\r\n").encode() + csv_bytes + f"\r\n--{boundary}--\r\n".encode()
    up = sess.call("POST", "/bulk-skip/mapping", raw=raw, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    bid = up["bulkSkipId"]
    sess.call("POST", f"/bulk-skip/fields/{bid}", body={"schema": SCHEMA})
    calc = sess.call("POST", f"/bulk-skip/calculate/{bid}")
    return {"bulkSkipId": bid, "entities": calc.get("entities")}


def pay(sess: Session, bid: str) -> str:
    """Charge the account's default card. BILLS. Returns the payment status; raises if it can't
    complete unattended (3DS) or failed."""
    pms = sess.call("GET", "/payment/payment-method", ok=(200,))
    if not pms:
        raise SmartSkipError("no saved payment method")
    pm = next((p for p in pms if p.get("isDefault")), pms[0])
    data = sess.call("POST", "/bulk-skip/payment-intent", body={"bulkSkipId": bid, "paymentMethodId": pm["id"]})
    if data.get("clientSecret"):
        raise SmartSkipError(f"payment for {bid} needs 3DS confirmation in the SmartSkip web app")
    status = data.get("status")
    if status not in ("succeeded", "processing", "requires_capture"):
        raise SmartSkipError(f"payment for {bid} status {status!r}")
    return status


def wait_and_download(sess: Session, bid: str, max_wait=900, interval=15) -> str:
    """Poll until the order completes, then return the vertical export as text."""
    t0 = time.time()
    while time.time() - t0 < max_wait:
        data = sess.call("GET", "/bulk-skip?sortField=createdAt&sortOrder=desc", ok=(200,))
        st = next((str(i.get("status") or "") for i in data.get("items", []) if i.get("_id") == bid), "").lower()
        if st == "completed":
            blob = sess.call("GET", f"/bulk-skip/download/{bid}?type=vertical", ok=(200,))
            return blob.decode("utf-8-sig") if isinstance(blob, (bytes, bytearray)) else json.dumps(blob)
        if st in ("error", "failed"):
            raise SmartSkipError(f"order {bid} ended {st}")
        time.sleep(interval)
    raise SmartSkipError(f"order {bid} not complete after {max_wait}s (paid; download it later)")


# ----------------------------------------------------------------- parse
def digits(s) -> str | None:
    d = re.sub(r"\D", "", s or "")
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d if len(d) == 10 else None


def _phones(row: dict) -> list[dict]:
    out, seen = [], set()
    for i in range(1, 19):
        n = digits(row.get(f"Phone {i} number"))
        if n and n not in seen:
            seen.add(n)
            out.append({"number": n, "type": (row.get(f"Phone {i} type") or "").strip()})
    return out


def _yes(v) -> bool:
    return str(v or "").strip().lower() in ("true", "y", "yes", "1")


def parse(text: str) -> list[dict]:
    """Vertical export -> one record per input row: subject + relatives (Associates dropped,
    ages over 100 dropped as bad data)."""
    rows = list(csv.DictReader(io.StringIO(text)))
    if rows and "Input Name" not in rows[0]:
        raise SmartSkipError("not a vertical SmartSkip export (no 'Input Name' column)")
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["Input Name"], []).append(r)
    recs = []
    for inp, grp in groups.items():
        s = next((x for x in grp if x.get("Relationship") == "Subject"), grp[0])
        rec = {"input_name": inp, "first": (s.get("First Name") or "").strip(), "last": (s.get("Last Name") or "").strip(),
               "property_address": (s.get("Property Address") or "").strip(), "deceased": _yes(s.get("Deceased")),
               "subject_phones": _phones(s), "relatives": []}
        for x in grp:
            if x.get("Relationship") != "Relative":
                continue
            try:
                if int(str(x.get("Age") or "").strip()) > 100:
                    continue
            except ValueError:
                pass
            first, last = (x.get("First Name") or "").strip(), (x.get("Last Name") or "").strip()
            rec["relatives"].append({"first": first, "last": last, "name": f"{first} {last}".strip(),
                                     "rel": REL.get((x.get("Possible Type") or "").strip().lower(), "Relative"),
                                     "age": (x.get("Age") or "").strip(), "deceased": _yes(x.get("Deceased")),
                                     "phones": _phones(x)})
        recs.append(rec)
    return recs
