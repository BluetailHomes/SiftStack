"""Read-only SiftMap lookups: property address -> neighborhood, owner name -> properties.

Used by the probate pipeline (enrichment_pipeline Step 3c Jackson tier and Step 8c
neighborhood gate) to keep leads inside config.TARGET_NEIGHBORHOODS, the same 25
neighborhoods the "Jackson - Priority 1" SiftMap presets use.

Endpoints (map.reisift.io, the ones app.reisift.io's SiftMap calls; undocumented, verified
2026-11-07 — could change without notice):
  POST /properties/search-autocomplete/  {"search": text, "entity_types": ["address"|"full_name"]}
       -> data[] rows: id, address ("4232 S Benton Ave, Kansas City, MO 64130"), city, state,
          owner{full_name}, mailing_address, counties[{county_name}], in_my_account
  GET  /properties/detail/{id}/          -> neighborhood{name}, owner_info, subdivision, apn

Auth is the Open API key (REISIFT_API_KEY, "Api-Key <key>"). Never logged.

Gotchas found in testing:
  - Addresses must be USPS-abbreviated ("S Benton Ave", not "South Benton Avenue"); Smarty
    output already is. Retried once with the street type dropped.
  - Name search is nationwide, capped at 100 rows, and prefix-matches the stored owner name
    ("John W Shinn"; trusts are last-name-first: "Shinn John W & Ruby M Trust"). A deceased
    owner stays the owner of record — there are no "Estate of" names.
  - One owner can hold many parcels, so callers decide per property.
  - ~0.7-0.8 s per address lookup; no 429s seen.
"""

from __future__ import annotations

import logging
import os
import re
import time

import requests

logger = logging.getLogger(__name__)

MAP = "https://map.reisift.io"
_HEADERS = {
    "accept": "application/json", "content-type": "application/json",
    "origin": "https://app.reisift.io", "referer": "https://app.reisift.io/",
}
_STREET_TYPES = {
    "st", "ave", "rd", "dr", "ln", "ct", "blvd", "way", "pl", "cir", "ter", "pkwy", "trl", "hwy", "loop",
}
_ADDR_RE = re.compile(r"^\s*(?P<street>.+?),\s*(?P<city>[^,]+),\s*(?P<state>[A-Z]{2})\s*(?P<zip>\d{5})?")


def norm_hood(name: str | None) -> str:
    """SiftMap neighborhood -> the form config.TARGET_NEIGHBORHOODS stores (lowercase, no ", MO")."""
    return re.sub(r",\s*[a-z]{2}$", "", (name or "").strip().lower())


def split_address(full: str) -> dict:
    """'4232 S Benton Ave, Kansas City, MO 64130' -> {street, city, state, zip} ({} if unparseable)."""
    m = _ADDR_RE.match(full or "")
    return {k: (v or "") for k, v in m.groupdict().items()} if m else {}


def _house_number(street: str) -> str:
    m = re.match(r"\s*(\d+)", street or "")
    return m.group(1) if m else ""


def _in_county(row: dict, county: str, state: str) -> bool:
    return row.get("state") == state and any(
        (c.get("county_name") or "").lower() == county.lower() for c in row.get("counties") or []
    )


def _clean_name(name: str) -> str:
    name = re.sub(r"[.,]", " ", name or "")
    name = re.sub(r"\b(jr|sr|ii|iii|iv|deceased)\b", " ", name, flags=re.I)
    return re.sub(r"\s+", " ", name).strip()


def name_variants(name: str) -> list[str]:
    """Search strings for a decedent name, most specific first: full, First Last, Last First."""
    full = _clean_name(name)
    parts = full.split()
    out = [full]
    if len(parts) >= 2:
        out += [f"{parts[0]} {parts[-1]}", f"{parts[-1]} {parts[0]}"]
    return [v for i, v in enumerate(out) if v and v not in out[:i]]


class SiftMap:
    def __init__(self, api_key: str | None = None, timeout: int = 60):
        key = api_key or os.getenv("REISIFT_API_KEY", "")
        if not key:
            raise RuntimeError("REISIFT_API_KEY not set")
        self.session = requests.Session()
        self.session.headers.update({**_HEADERS, "authorization": "Api-Key " + key})
        self.timeout = timeout

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        for attempt in range(4):
            r = self.session.request(method, MAP + path, json=body, timeout=self.timeout)
            if r.status_code == 429 and attempt < 3:
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        return {}

    def autocomplete(self, text: str, entity_type: str = "address") -> list[dict]:
        body = {"search": text, "entity_types": [entity_type]}
        return self._call("POST", "/properties/search-autocomplete/", body).get("data") or []

    def neighborhood(self, property_id: str) -> str:
        d = self._call("GET", f"/properties/detail/{property_id}/")
        return (d.get("neighborhood") or {}).get("name") or ""

    def lookup_address(self, street: str, city: str = "", state: str = "MO", zip_code: str = "",
                       county: str = "Jackson") -> dict | None:
        """Property address -> {id, address, neighborhood} or None when SiftMap has no match.

        A match must be in the county and share the house number — autocomplete happily
        returns a neighbor's parcel otherwise.
        """
        number = _house_number(street)
        if not number:
            return None
        tail = " ".join(p for p in (city, state, zip_code) if p)
        queries = [f"{street} {tail}".strip()]
        words = street.split()
        if words and words[-1].lower().rstrip(".") in _STREET_TYPES:
            queries.append(f"{' '.join(words[:-1])} {tail}".strip())
        for q in queries:
            for row in self.autocomplete(q):
                if row.get("searchType") != "address" or not _in_county(row, county, state):
                    continue
                if _house_number(split_address(row.get("address", "")).get("street", "")) != number:
                    continue
                return {"id": row["id"], "address": row.get("address", ""),
                        "neighborhood": self.neighborhood(row["id"])}
        return None

    def lookup_owner(self, name: str, county: str = "Jackson", state: str = "MO") -> list[dict]:
        """Decedent name -> every distinct property in county/state SiftMap lists under it.

        Tries name_variants() in order and stops at the first one with county hits, so
        "John W Shinn" doesn't get widened to every "John Shinn" once it has matched.
        Rows: {id, address, owner, mailing_address}. No neighborhood (one detail call each).
        """
        for q in name_variants(name):
            seen, out = set(), []
            for row in self.autocomplete(q, "full_name"):
                if not _in_county(row, county, state) or row.get("id") in seen:
                    continue
                seen.add(row.get("id"))
                out.append({"id": row.get("id"), "address": row.get("address", ""),
                            "owner": (row.get("owner") or {}).get("full_name", ""),
                            "mailing_address": row.get("mailing_address", "")})
            if out:
                return out
        return []


def probate_property_lookup_siftmap(notices, county: str = "Jackson", state: str = "MO", sm: SiftMap | None = None) -> None:
    """Jackson probate tier: decedent name -> property address, via SiftMap owner of record.

    Jackson has no public owner-searchable parcel API (see mo_parcel_lookup's docstring), but a
    deceased owner stays the owner of record in SiftMap. Fills the address only on exactly ONE
    county property whose owner name carries the decedent's surname as a whole word — zero or
    several hits leave the record without an address (it then passes the gates as unplaced).
    The address still goes through Smarty and Zillow downstream. Modifies notices in place.
    """
    candidates = [n for n in notices if not n.address.strip() and n.decedent_name.strip()]
    if not candidates:
        return
    sm = sm or SiftMap()
    for n in candidates:
        surname = (_clean_name(n.decedent_name).split() or [""])[-1].lower()
        try:
            hits = [h for h in sm.lookup_owner(n.decedent_name, county, state)
                    if surname and surname in _clean_name(h["owner"]).lower().split()]
        except requests.RequestException as e:
            logger.warning("    SiftMap owner lookup failed for %s: %s", n.decedent_name, e)
            continue
        if len(hits) != 1:
            logger.info("    %s: %d %s County properties under that name — address left blank",
                        n.decedent_name, len(hits), county)
            continue
        parts = split_address(hits[0]["address"])
        if not parts.get("street"):
            continue
        # Always overwrite zip: a blank here beats keeping a parser-fallback ZIP (often the PR's).
        n.address, n.city, n.state, n.zip = parts["street"], parts["city"], parts["state"], parts["zip"]
        n.address_source = "siftmap_owner"
        logger.info("    %s -> %s (SiftMap owner %s)", n.decedent_name, hits[0]["address"], hits[0]["owner"])
