#!/usr/bin/env python3
"""Job-Watch: zoznam firiem -> nove relevantne ponuky + stav kazdej firmy.
Nulove tokeny. Vystup: data/new_jobs.json, data/status.json, data/seen.json"""
import json, re, sys, time, hashlib, datetime as dt
from pathlib import Path
from urllib.parse import urlparse, urljoin
import xml.etree.ElementTree as ET
import requests
from bs4 import BeautifulSoup

UA = {"User-Agent": "job-watch/1.0 (private job search; 1 request per site, twice a week)"}
D = Path("data"); D.mkdir(exist_ok=True)

INCLUDE = re.compile(r"analyst|analytics|analyse|analytiker|datenanaly|\bdata\b|\bdaten\b|\bbi\b|business intelligence|"
                     r"e-?commerce|growth|\bcro\b|conversion|a/b|ab-?test|tracking|reporting|performance marketing|"
                     r"marketing analyst|product analyst|controlling|insights", re.I)
EXCLUDE = re.compile(r"praktik|werkstudent|ausbildung|azubi|duales? studium|intern\b|internship|minijob|aushilfe", re.I)
LOC_OK = re.compile(r"remote|home ?office|m(ü|ue|u)nchen|munich|ingolstadt|manching|bundesweit|deutschlandweit", re.I)

def loc_flag(title, loc):
    t = f"{title} {loc or ''}"
    if LOC_OK.search(t): return "ok"
    if not (loc or "").strip(): return "unbekannt"
    return "ausserhalb"          # bekannter Ort, passt nicht -> wird nur markiert, nicht verworfen

def get(url):
    r = requests.get(url, headers=UA, timeout=25); r.raise_for_status(); return r

def personio(base):
    host = urlparse(base).netloc
    r = get(f"https://{host}/xml")
    root = ET.fromstring(r.content)
    out = []
    for p in root.findall("position"):
        out.append(dict(title=(p.findtext("name") or "").strip(), loc=(p.findtext("office") or "").strip(),
                        url=f"https://{host}/job/{p.findtext('id')}", dept=(p.findtext("department") or "").strip()))
    return out

def recruitee(base):
    host = urlparse(base).netloc
    r = get(f"https://{host}/api/offers/")
    return [dict(title=o.get("title",""), loc=o.get("location",""), url=o.get("careers_url",""), dept=o.get("department",""))
            for o in r.json().get("offers", [])]

def html(base):
    """Fallback: Links, deren Text nach Rolle klingt. JS-Seiten liefern oft nichts -> Status 'html_leer'."""
    r = get(base); soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        txt = " ".join(a.get_text(" ").split())
        if 5 < len(txt) < 140:
            out.append(dict(title=txt, loc="", url=urljoin(base, a["href"]), dept=""))
    sig = hashlib.sha1(re.sub(r"\s+", " ", soup.get_text(" ")).encode()).hexdigest()
    return out, sig

def adapter(url):
    h = urlparse(url).netloc
    if "personio" in h: return "personio"
    if "recruitee" in h: return "recruitee"
    return "html"

def main():
    seen = json.loads((D/"seen.json").read_text()) if (D/"seen.json").exists() else {}
    sigs = json.loads((D/"page_hash.json").read_text()) if (D/"page_hash.json").exists() else {}
    first_run = not seen
    status, new = [], []
    for line in Path("companies.txt").read_text().splitlines():
        if "|" not in line: continue
        name, url = [x.strip() for x in line.split("|", 1)]
        kind = adapter(url); st = dict(firma=name, typ=kind, url=url, ergebnis="", anzahl=0)
        try:
            if kind == "personio": jobs = personio(url)
            elif kind == "recruitee": jobs = recruitee(url)
            else:
                jobs, sig = html(url)
                st["seite_geaendert"] = (sigs.get(name) not in (None, sig)); sigs[name] = sig
            st["anzahl"] = len(jobs)
            st["ergebnis"] = "ok" if (jobs and kind != "html") else ("html_links" if jobs else "html_leer")
            hits = [j for j in jobs if INCLUDE.search(j["title"]) and not EXCLUDE.search(j["title"])]
            # bei html nur Treffer, deren Text wirklich nach Job klingt (kein Menue-Rauschen)
            for j in hits:
                key = j["url"]
                if key in seen: continue
                seen[key] = dict(firma=name, titel=j["title"], erstmals=dt.date.today().isoformat())
                new.append(dict(firma=name, titel=j["title"], ort=j["loc"], ort_check=loc_flag(j["title"], j["loc"]),
                                abteilung=j["dept"], url=j["url"], quelle=kind))
        except Exception as e:
            st["ergebnis"] = f"fehler: {type(e).__name__} {str(e)[:80]}"
        status.append(st); time.sleep(1)
    (D/"seen.json").write_text(json.dumps(seen, ensure_ascii=False, indent=1))
    (D/"page_hash.json").write_text(json.dumps(sigs, indent=1))
    (D/"status.json").write_text(json.dumps(dict(datum=dt.datetime.utcnow().isoformat()+"Z", erster_lauf=first_run, firmen=status), ensure_ascii=False, indent=1))
    (D/"new_jobs.json").write_text(json.dumps(dict(datum=dt.date.today().isoformat(), erster_lauf=first_run, neue=new), ensure_ascii=False, indent=1))
    ok = sum(s["ergebnis"] in ("ok","html_links") for s in status)
    print(f"{len(status)} Firmen, lesbar: {ok}, neue Treffer: {len(new)}")

if __name__ == "__main__": main()
