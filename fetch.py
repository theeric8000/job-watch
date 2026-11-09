#!/usr/bin/env python3
"""Job-Watch v2: zoznam firiem -> nove relevantne ponuky + stav kazdej firmy.
Nulove tokeny. Vystup: data/new_jobs.json, data/status.json, data/seen.json
 
Zmeny v2:
- Personio: ak /xml neexistuje (404), cita sa HTML stranka s pracovnymi ponukami
- Workday (napr. Weleda): citanie cez verejne JSON API (netestovane, pri chybe sa to ukaze v status.json)
- filter: vylucene veduce pozicie (Head of, Lead, Director, Chief ...), lebo hladas poziciu specialistu
- html fallback: len odkazy, ktore vyzeraju ako pracovny inzerat (nie bezne stranky webu)
"""
import json, re, time, hashlib, datetime as dt
from pathlib import Path
from urllib.parse import urlparse, urljoin
import xml.etree.ElementTree as ET
import requests
from bs4 import BeautifulSoup
 
UA = {"User-Agent": "job-watch/2.0 (private job search; 1 request per site, twice a week)"}
D = Path("data"); D.mkdir(exist_ok=True)
 
INCLUDE = re.compile(r"analyst|analytics|analyse|analytiker|datenanaly|\bdata\b|\bdaten\b|\bbi\b|business intelligence|"
                     r"e-?commerce|growth|\bcro\b|conversion|a/b|ab-?test|tracking|reporting|performance marketing|"
                     r"marketing analyst|product analyst|controlling|insights", re.I)
EXCLUDE = re.compile(r"praktik|werkstudent|ausbildung|azubi|duales? studium|intern\b|internship|minijob|aushilfe", re.I)
# Veduce pozicie: Erik hlada poziciu specialistu, nie lead
LEAD = re.compile(r"head of|\bhead\b|\blead\b|leiter|leitung|director|chief|\bcmo\b|\bcto\b|vp\b|team ?lead|geschäftsführ", re.I)
LOC_OK = re.compile(r"remote|home ?office|m(ü|ue|u)nchen|munich|ingolstadt|manching|bundesweit|deutschlandweit", re.I)
 
JOB_URL = re.compile(r"job|stelle|karriere|career|vacanc|position|festanstellung|bewerb|/jobs?/", re.I)
JOB_HOSTS = ("join.com", "softgarden", "personio", "recruitee", "workable", "myworkdayjobs", "jobs.", "rosenheimjobs")
 
 
def loc_flag(title, loc):
    t = f"{title} {loc or ''}"
    if LOC_OK.search(t): return "ok"
    if not (loc or "").strip(): return "unbekannt"
    return "ausserhalb"   # bekannter Ort, passt nicht -> nur markiert, nicht verworfen
 
 
def get(url):
    r = requests.get(url, headers=UA, timeout=25); r.raise_for_status(); return r
 
 
def personio(base):
    host = urlparse(base).netloc
    try:
        r = get(f"https://{host}/xml")
        root = ET.fromstring(r.content)
        return [dict(title=(p.findtext("name") or "").strip(), loc=(p.findtext("office") or "").strip(),
                     url=f"https://{host}/job/{p.findtext('id')}", dept=(p.findtext("department") or "").strip())
                for p in root.findall("position")], "personio-xml"
    except Exception:
        pass   # XML-Feed deaktiviert -> HTML-Seite lesen
    soup = BeautifulSoup(get(f"https://{host}/").text, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        if "/job/" not in a["href"]: continue
        url = urljoin(f"https://{host}/", a["href"].split("?")[0])
        if url in seen: continue
        seen.add(url)
        parts = [s for s in a.stripped_strings]
        if not parts: continue
        out.append(dict(title=parts[0], loc=parts[1] if len(parts) > 1 else "", url=url, dept=""))
    if not out: raise ValueError("Personio-Seite ohne lesbare Stellen (evtl. JavaScript)")
    return out, "personio-html"
 
 
def recruitee(base):
    host = urlparse(base).netloc
    r = get(f"https://{host}/api/offers/")
    return [dict(title=o.get("title", ""), loc=o.get("location", ""), url=o.get("careers_url", ""), dept=o.get("department", ""))
            for o in r.json().get("offers", [])], "recruitee"
 
 
def workday(base):
    u = urlparse(base); host = u.netloc
    tenant = host.split(".")[0]; site = u.path.strip("/").split("/")[0]
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    out, offset = [], 0
    while True:
        r = requests.post(api, json={"limit": 20, "offset": offset, "searchText": "", "appliedFacets": {}},
                          headers={**UA, "Content-Type": "application/json"}, timeout=25)
        r.raise_for_status(); j = r.json()
        for p in j.get("jobPostings", []):
            out.append(dict(title=p.get("title", ""), loc=p.get("locationsText", ""),
                            url=f"https://{host}/en-US/{site}{p.get('externalPath','')}", dept=""))
        offset += 20
        if offset >= j.get("total", 0) or not j.get("jobPostings"): break
        time.sleep(0.5)
    return out, "workday"
 
 
def html(base):
    """Fallback: Links, deren Text nach Rolle klingt UND deren URL nach Stelleninserat aussieht."""
    r = get(base); soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        txt = " ".join(a.get_text(" ").split())
        if not (5 < len(txt) < 140): continue
        url = urljoin(base, a["href"])
        h = urlparse(url).netloc.lower()
        if JOB_URL.search(urlparse(url).path) or any(x in h for x in JOB_HOSTS):
            out.append(dict(title=txt, loc="", url=url, dept=""))
    sig = hashlib.sha1(re.sub(r"\s+", " ", soup.get_text(" ")).encode()).hexdigest()
    return out, sig
 
 
def adapter(url):
    h = urlparse(url).netloc
    if "personio" in h: return "personio"
    if "recruitee" in h: return "recruitee"
    if "myworkdayjobs" in h: return "workday"
    return "html"
 
 
def main():
    seen = json.loads((D/"seen.json").read_text()) if (D/"seen.json").exists() else {}
    sigs = json.loads((D/"page_hash.json").read_text()) if (D/"page_hash.json").exists() else {}
    first_run = not seen
    status, new = [], []
    for line in Path("companies.txt").read_text().splitlines():
        if "|" not in line or line.strip().startswith("#"): continue
        name, url = [x.strip() for x in line.split("|", 1)]
        kind = adapter(url); st = dict(firma=name, typ=kind, url=url, ergebnis="", anzahl=0)
        try:
            if kind == "personio": jobs, src = personio(url)
            elif kind == "recruitee": jobs, src = recruitee(url)
            elif kind == "workday": jobs, src = workday(url)
            else:
                jobs, sig = html(url); src = "html"
                st["seite_geaendert"] = (sigs.get(name) not in (None, sig)); sigs[name] = sig
            st["quelle"] = src; st["anzahl"] = len(jobs)
            st["ergebnis"] = "ok" if (jobs and src != "html") else ("html_links" if jobs else "html_leer")
            hits = [j for j in jobs if INCLUDE.search(j["title"]) and not EXCLUDE.search(j["title"])
                    and not LEAD.search(j["title"])]
            for j in hits:
                key = j["url"]
                if key in seen: continue
                seen[key] = dict(firma=name, titel=j["title"], erstmals=dt.date.today().isoformat())
                new.append(dict(firma=name, titel=j["title"], ort=j["loc"], ort_check=loc_flag(j["title"], j["loc"]),
                                abteilung=j["dept"], url=j["url"], quelle=src))
        except Exception as e:
            st["ergebnis"] = f"fehler: {type(e).__name__} {str(e)[:80]}"
        status.append(st); time.sleep(1)
    (D/"seen.json").write_text(json.dumps(seen, ensure_ascii=False, indent=1))
    (D/"page_hash.json").write_text(json.dumps(sigs, indent=1))
    (D/"status.json").write_text(json.dumps(dict(datum=dt.datetime.utcnow().isoformat()+"Z", erster_lauf=first_run,
                                                 firmen=status), ensure_ascii=False, indent=1))
    (D/"new_jobs.json").write_text(json.dumps(dict(datum=dt.date.today().isoformat(), erster_lauf=first_run, neue=new),
                                               ensure_ascii=False, indent=1))
    ok = sum(s["ergebnis"] in ("ok", "html_links") for s in status)
    print(f"{len(status)} Firmen, lesbar: {ok}, neue Treffer: {len(new)}")
 
 
if __name__ == "__main__":
    main()
