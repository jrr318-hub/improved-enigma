
"""
8-K CEO Departure Watch (Item 5.02) — Gemini edition
----------------------------------------------------
Upload CIKs (or tickers). The app fetches 8‑K/8‑K/A filings, filters to Item 5.02,
then asks **Gemini** to extract **CEO departure events only** (name + optional effective date)
with links to the 8‑K.

- Uses official SEC endpoints (data.sec.gov + Archives) with polite rate limiting.
- Falls back to a strict heuristic if Gemini isn’t available.
- Deliberately excludes director changes and non‑CEO officers.
"""

import os
import io
import re
import time
import json
from typing import Dict, Iterable, List, Optional

import pandas as pd
import requests
import streamlit as st

# --------------------
# UI
# --------------------
st.set_page_config(page_title="8-K CEO Departure Watch (Gemini)", layout="wide")
st.title("👔 8-K CEO Departure Watch — Gemini")
st.caption("Upload CIKs/tickers. We’ll flag Item 5.02 and list **CEO departures only** with direct links.")

with st.sidebar:
    st.header("Settings")
    ua = st.text_input(
        "User-Agent (must include contact email)",
        value=os.environ.get("SEC_UA", "FW Cook EDGAR Watch/1.0 (contact: you@example.com)"),
        help="SEC requires a descriptive User‑Agent with a contact email."
    )
    rps = st.slider("Requests per second", 0.2, 5.0, 2.0, 0.1, help="Stay well below SEC limits.")
    start_date = st.date_input("From filing date", value=None)
    end_date = st.date_input("To filing date", value=None)
    use_full_history = st.checkbox("Try extended history when available", value=True)
    st.divider()
    use_gemini = st.checkbox("Use Gemini extraction (recommended)", value=True)
    gemini_key = st.text_input("Gemini API key", type="password", value=os.environ.get("GOOGLE_API_KEY", ""))
    gemini_model = st.text_input("Gemini model", value=os.environ.get("GEMINI_MODEL", "gemini-1.5-flash"))

st.markdown("### 1) Provide companies")
col1, col2 = st.columns(2)
with col1:
    uploaded = st.file_uploader("Upload CSV with 'CIK' or 'ticker' column", type=["csv"], accept_multiple_files=False)
with col2:
    manual = st.text_area("Or paste CIKs/tickers", placeholder="0000320193\nMSFT\n0000789019", height=140)

run_btn = st.button("🚀 Run search")

# --------------------
# Helpers
# --------------------
SEC_HEADERS = lambda ua: {"User-Agent": ua.strip(), "Accept-Encoding": "gzip, deflate", "Host": "data.sec.gov"}
ARCHIVE_HEADERS = lambda ua: {"User-Agent": ua.strip(), "Accept-Encoding": "gzip, deflate", "Host": "www.sec.gov"}

# warn-once flags
SDK_WARNED = False
REST_WARNED = False

ITEM_502_REGEX = re.compile(r"item[\s\xa0]*5\.02", re.IGNORECASE)
TAG_STRIPPER = re.compile(r"<[^>]+>")

CEO_TOKEN = re.compile(r"\b(Chief Executive Officer|C\.?.?E\.?.?O\.?.?|CEO|principal executive officer)\b", re.IGNORECASE)
DEPARTURE_VERBS = re.compile(
    r"(resign(?:s|ed|ation)?|retir(?:e|es|ed|ement)|terminate(?:s|d|ion)?|ceased to serve|remov(?:e|ed)|dismiss(?:al|ed)|separate(?:s|d|ion)?|stepp?ed down|step(?:s)? down|will step down|to step down|will not stand for re-election|depart(?:s|ed|ure)?|leave(?:s|ing|t)|transition(?:s|ed)? out|end(?:ed)? employment|employment terminated)",
    re.IGNORECASE,
)
NEG_CONTINUE = re.compile(r"\b(continue|remains?|remain|retains?)\b", re.IGNORECASE)
NEG_INCOMING = re.compile(r"\b(appoint(?:ed|ment)|name(?:d|s)|elect(?:ed|ion)|succeed(?:s|ed)|assume(?:s|d) (?:the )?role|will serve as|will act as|promotion|promote(?:d)?)\b", re.IGNORECASE)
DATE_RE = re.compile(r"(January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2}, \d{4}")
NAME_RE = re.compile(r"\b([A-Z][a-z]+(?: [A-Z]\.)?(?: [A-Z][a-z]+){0,3})\b")

@st.cache_data(show_spinner=False)
def load_ticker_map(ua: str) -> Dict[str, int]:
    url = "https://www.sec.gov/files/company_tickers.json"
    s = requests.Session(); s.headers.update(ARCHIVE_HEADERS(ua))
    r = s.get(url, timeout=30); r.raise_for_status()
    data = r.json(); mapping = {}
    for _, rec in data.items() if isinstance(data, dict) else enumerate(data):
        try:
            tkr = str(rec.get("ticker", "")).upper().strip()
            cik = int(rec.get("cik_str"))
            if tkr: mapping[tkr] = cik
        except Exception:
            continue
    return mapping

@st.cache_data(show_spinner=False)
def fetch_submissions(cik: int, ua: str) -> dict:
    s = requests.Session(); url = f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json"
    r = s.get(url, headers=SEC_HEADERS(ua), timeout=60); r.raise_for_status(); return r.json()

@st.cache_data(show_spinner=False)
def fetch_company_name(cik: int, ua: str) -> str:
    try:
        js = fetch_submissions(cik, ua)
        return js.get("name", "")
    except Exception:
        return ""

@st.cache_data(show_spinner=False)
def fetch_submissions_file(cik: int, name: str, ua: str) -> Optional[dict]:
    s = requests.Session(); url = f"https://data.sec.gov/submissions/{name}"
    try: r = s.get(url, headers=SEC_HEADERS(ua), timeout=60); r.raise_for_status(); return r.json()
    except Exception: return None


def iter_filings_from_submissions(js: dict) -> Iterable[Dict[str, str]]:
    if not js: return []
    rec = js.get("filings", {}).get("recent", {})
    keys = ["accessionNumber","form","filingDate","reportDate","primaryDocument","primaryDocDescription"]
    arrays = {k: rec.get(k, []) for k in keys}
    n = max((len(v) for v in arrays.values()), default=0)
    for i in range(n):
        yield {k: (arrays[k][i] if i < len(arrays[k]) else None) for k in keys}


def fetch_extended_history(cik: int, ua: str) -> List[Dict[str, str]]:
    js = fetch_submissions(cik, ua); out = list(iter_filings_from_submissions(js))
    files = js.get("filings", {}).get("files", []) or []
    for f in files:
        name = f.get("name");
        if not name: continue
        older = fetch_submissions_file(cik, name, ua)
        if older: out += list(iter_filings_from_submissions(older))
    return out


def filing_doc_url(cik: int, accession: str, primary_doc: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{primary_doc}"


def filing_index_url(cik: int, accession: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}-index.html"


def pick_best_primary_doc(session: requests.Session, cik: int, accession: str, default_primary: str) -> str:
    """Use the filing index page to choose the main 8-K HTML document when possible."""
    try:
        idx_url = filing_index_url(cik, accession)
        r = session.get(idx_url, timeout=60)
        r.raise_for_status()
        html = r.text
        # Rough-parse rows: grab tuples of (href, type)
        # Match links like <a href="/Archives/edgar/data/CIK/ACC/file.htm"> and nearby Type cell
        rows = re.findall(r"<tr[\s\S]*?</tr>", html, flags=re.IGNORECASE)
        candidates: List[tuple] = []
        for row in rows:
            # document file
            mfile = re.search(r"href=\"[^\"]*/(.*?)\"", row, flags=re.IGNORECASE)
            mtype = re.search(r"<td[^>]*>\s*([A-Za-z0-9\- ]*8\-K[^<]*)\s*</td>", row, flags=re.IGNORECASE)
            if not mfile:
                continue
            fname = mfile.group(1)
            # find document type column; if not explicitly 8-K, also accept description mentioning 8-K
            is_html = fname.lower().endswith((".htm", ".html"))
            score = 0
            if is_html:
                score += 1
            if mtype:
                score += 2
            # Avoid exhibits (ex99) unless nothing else
            if re.search(r"ex-?\d|ex99|exhibit", fname, re.IGNORECASE):
                score -= 2
            candidates.append((score, fname))
        if candidates:
            candidates.sort(reverse=True)
            best = candidates[0][1]
            return best
        return default_primary
    except Exception:
        return default_primary


def enumerate_candidate_docs(session: requests.Session, cik: int, accession: str, primary: str) -> List[str]:
    """Return a prioritized list of candidate document filenames to scan within a filing.
    Includes HTML, TXT, and PDF documents, de-duplicated and ordered by likelihood.
    """
    docnames: List[str] = []
    seen = set()
    try:
        idx_url = filing_index_url(cik, accession)
        r = session.get(idx_url, timeout=60)
        r.raise_for_status()
        html = r.text
        rows = re.findall(r"<tr[\s\S]*?</tr>", html, flags=re.IGNORECASE)
        for row in rows:
            mf = re.search(r"href=\"[^\"]*/(.*?)\"", row, flags=re.IGNORECASE)
            if not mf: continue
            fname = mf.group(1)
            if fname in seen: continue
            ext = fname.lower().rsplit(".", 1)[-1] if "." in fname else ""
            if ext in ("htm", "html", "txt", "pdf"):
                seen.add(fname); docnames.append(fname)
    except Exception:
        pass
    # Ensure primary is included and at front
    if primary and primary not in seen:
        docnames.insert(0, primary)
    # Prioritize: HTML -> TXT -> PDF
    def priority(n: str) -> int:
        nlow = n.lower()
        if nlow.endswith((".htm", ".html")): return 3
        if nlow.endswith(".txt"): return 2
        if nlow.endswith(".pdf"): return 1
        return 0
    docnames.sort(key=priority, reverse=True)
    return docnames


def get_text_from_document(session: requests.Session, url: str) -> Optional[str]:
    """Fetch a document and return plain text. Supports HTML/TXT natively and tries PDF if available."""
    r = session.get(url, timeout=60)
    r.raise_for_status()
    content_type = (r.headers.get("Content-Type") or "").lower()
    r.encoding = r.apparent_encoding or "utf-8"
    if any(ext in url.lower() for ext in (".htm", ".html")) or "html" in content_type:
        return clean_html(r.text)
    if url.lower().endswith(".txt") or "text/plain" in content_type:
        return re.sub(r"\s+", " ", r.text)
    if url.lower().endswith(".pdf") or "application/pdf" in content_type:
        try:
            # Lazy import to avoid hard dep
            from pdfminer.high_level import extract_text as pdf_extract_text
            text = pdf_extract_text(io.BytesIO(r.content))
            return re.sub(r"\s+", " ", text)
        except Exception:
            return None
    # Unknown type
    return None


def clean_html(text: str) -> str:
    text = TAG_STRIPPER.sub(" ", text); return re.sub(r"\s+", " ", text)


# Maintain the legacy short-window finder for quick checks
def find_item_502(text: str) -> Optional[str]:
    m = ITEM_502_REGEX.search(text)
    if not m: return None
    start = max(m.start() - 120, 0); end = min(m.end() + 180, len(text))
    return text[start:end].strip()


def extract_item_502_section(text: str) -> Optional[str]:
    """Return the full Item 5.02 section from cleaned text, up to the next Item section or end."""
    m = ITEM_502_REGEX.search(text)
    if not m: return None
    start = m.start()
    # Match next item (e.g., Item 5.03, Item 9.01, Item 1.01, etc.). Allow nbsp/space variants
    next_item_re = re.compile(r"item[\s\xa0]*\d+\.\d+", re.IGNORECASE)
    next_m = next_item_re.search(text, m.end())
    end = next_m.start() if next_m else len(text)
    # Trim overly long sections just in case
    section = text[start:end]
    return section.strip()


def extract_all_item_502_sections(text: str) -> List[str]:
    """Extract all Item 5.02 sections from the filing text."""
    sections: List[str] = []
    next_item_re = re.compile(r"item[\s\xa0]*\d+\.\d+", re.IGNORECASE)
    for m in ITEM_502_REGEX.finditer(text):
        start = m.start()
        next_m = next_item_re.search(text, m.end())
        end = next_m.start() if next_m else len(text)
        section = text[start:end].strip()
        if 200 <= len(section) <= 200_000:
            sections.append(section)
    # Deduplicate near-duplicates
    uniq: List[str] = []
    seen = set()
    for sct in sections:
        key = sct[:400]
        if key in seen: continue
        seen.add(key); uniq.append(sct)
    return uniq

# --------------------
# Heuristic fallback (strict)
# --------------------

def extract_ceo_departures_heuristic(text: str) -> List[Dict[str, str]]:
    events: List[Dict[str, str]] = []
    sentences = re.split(r"(?<=[\.!?])\s+", text)
    for idx, sent in enumerate(sentences):
        if len(sent) < 20: continue
        if not CEO_TOKEN.search(sent): continue
        if NEG_CONTINUE.search(sent): continue  # e.g., "will continue as CEO"
        if NEG_INCOMING.search(sent): continue  # exclude appointment/incoming mentions
        if not DEPARTURE_VERBS.search(sent): continue
        # Capture name near CEO token
        m = re.search(r"([A-Z][a-z]+(?: [A-Z]\.)?(?: [A-Z][a-z]+){0,3}),?\s+(?:the )?(?:Chief Executive Officer|C\.?.?E\.?.?O\.?.?|CEO|principal executive officer)\b", sent)
        if not m:
            m = re.search(r"(?:Chief Executive Officer|C\.?.?E\.?.?O\.?.?|CEO|principal executive officer)\s+([A-Z][a-z]+(?: [A-Z]\.)?(?: [A-Z][a-z]+){0,3})", sent)
        name = m.group(1) if m else None
        if not name:
            n = NAME_RE.search(sent); name = n.group(1) if n else None
        if not name: continue
        # look for effective date in this or adjacent sentences
        d = DATE_RE.search(sent)
        if not d:
            look_window = " ".join(sentences[max(0, idx-1): min(len(sentences), idx+2)])
            d = DATE_RE.search(look_window)
        evidence = sent.strip()
        confidence = 0.6
        if d: confidence += 0.2
        if re.search(r"effective|effective immediately|effective on", evidence, re.IGNORECASE):
            confidence += 0.1
        if "CEO" in evidence or re.search(r"Chief Executive Officer", evidence, re.IGNORECASE):
            confidence += 0.05
        ev = {"name": name, "phrase": evidence[:300], "confidence": round(min(confidence, 0.95), 2)}
        if d: ev["date"] = d.group(0)
        events.append(ev)
    return events

# --------------------
# Gemini extraction
# --------------------

def extract_ceo_departures_gemini(text: str, api_key: str, model_name: str) -> List[Dict[str, str]]:
    global SDK_WARNED, REST_WARNED
    try:
        import google.generativeai as genai
    except Exception:
        if not SDK_WARNED:
            st.warning("`google-generativeai` not installed. Falling back to heuristic.")
            SDK_WARNED = True
        return extract_ceo_departures_heuristic(text)

    if not api_key:
        if not SDK_WARNED:
            st.warning("No Gemini API key provided — using heuristic only.")
            SDK_WARNED = True
        return extract_ceo_departures_heuristic(text)

    genai.configure(api_key=api_key)

    # Prefer all Item 5.02 sections
    sections = extract_all_item_502_sections(text)
    narrowed = "\n\n---\n\n".join(sections) if sections else text

    # Focus the context to likely paragraphs to reduce cost and increase accuracy
    paras = [p for p in re.split(r"\n\s*\n", narrowed) if ("CEO" in p or "Chief Executive Officer" in p or "chief executive officer" in p)]
    context = "\n\n".join(paras)[:150_000] or narrowed[:120_000]

    # Structured outputs via response schema
    response_schema = {
        "type": "object",
        "properties": {
            "events": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["departure"]},
                        "is_ceo": {"type": "boolean"},
                        "person": {"type": "string"},
                        "evidence": {"type": "string"},
                        "effective_date": {"type": ["string", "null"]},
                    },
                    "required": ["type", "is_ceo", "person", "evidence"]
                }
            }
        },
        "required": ["events"]
    }

    system = (
        "You are an extraction system for SEC 8-K Item 5.02 filings. "
        "Return ONLY CEO DEPARTURE events (resigned, retired, stepped down, terminated, removed, ceased to serve, will not stand for re-election as CEO). "
        "Exclude directors and all non-CEO officers. Exclude CEO appointments (incoming). "
        "For each event, include person (name), a short evidence quote (<=240 chars), and effective_date if present."
    )
    # Compatibility: support environments without GenerativeModel or structured outputs
    prompt_parts = [
        {"role": "user", "parts": [
            "Return JSON only with an 'events' array. \n\nExtract CEO departures only from this 8-K (Item 5.02):\n\n",
            context
        ]}
    ]
    raw_text: str = ""
    try:
        model_cls = getattr(genai, "GenerativeModel", None)
        if model_cls is None:
            raise AttributeError("GenerativeModel is not available in google-generativeai")
        model = model_cls(model_name=model_name, system_instruction=system)
        try:
            resp = model.generate_content(
                prompt_parts,
                generation_config={
                    "temperature": 0.2,
                    "response_mime_type": "application/json",
                    "response_schema": response_schema,
                    "max_output_tokens": 800,
                },
            )
            raw_text = getattr(resp, "text", "") or ""
        except Exception:
            # Fallback without response schema; ask for JSON in plain text
            resp = model.generate_content(
                [
                    {"role": "user", "parts": [
                        system + "\nReturn a strict JSON object with an 'events' array per the schema. No prose.",
                        "\n\nExtract CEO departures only from this 8-K text (Item 5.02):\n\n",
                        context,
                    ]}
                ],
                generation_config={
                    "temperature": 0.1,
                    "max_output_tokens": 800,
                },
            )
            raw_text = getattr(resp, "text", "") or ""
    except AttributeError:
        # REST fallback to Generative Language API
        try:
            endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
            payload = {
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {"text": system},
                            {"text": "\nReturn a strict JSON object with an 'events' array. No prose."},
                            {"text": "\n\nExtract CEO departures only from this 8-K text (Item 5.02):\n\n" + context},
                        ],
                    }
                ],
                "generationConfig": {
                    "temperature": 0.1,
                    "maxOutputTokens": 800,
                },
            }
            r = requests.post(
                endpoint,
                params={"key": api_key},
                headers={"Content-Type": "application/json"},
                data=json.dumps(payload),
                timeout=60,
            )
            r.raise_for_status()
            js = r.json()
            candidates = js.get("candidates", []) or []
            for c in candidates:
                content = c.get("content") or {}
                parts = content.get("parts") or []
                texts = [p.get("text", "") for p in parts if isinstance(p, dict)]
                if texts:
                    raw_text = "\n".join(texts)
                    break
            if not raw_text:
                raw_text = js.get("text", "") or ""
            if not raw_text:
                if not REST_WARNED:
                    st.warning("Gemini REST call failed; using heuristic extractor.")
                    REST_WARNED = True
                return extract_ceo_departures_heuristic(context)
        except Exception:
            if not REST_WARNED:
                st.warning("Gemini REST call failed; using heuristic extractor.")
                REST_WARNED = True
            return extract_ceo_departures_heuristic(context)

    # Parse JSON response (best effort), otherwise fallback to heuristic
    try:
        data = json.loads(raw_text)
    except Exception:
        try:
            fenced = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", raw_text)
            if fenced:
                data = json.loads(fenced.group(1))
            else:
                first = raw_text.find("{"); last = raw_text.rfind("}")
                if first != -1 and last != -1 and last > first:
                    data = json.loads(raw_text[first:last+1])
                else:
                    return extract_ceo_departures_heuristic(context)
        except Exception:
            return extract_ceo_departures_heuristic(context)

    events = data.get("events", []) if isinstance(data, dict) else []

    # Guardrail: ensure proximity of name to a CEO token in the context
    checked: List[Dict[str, str]] = []
    for e in events:
        if not (e.get("type") == "departure" and e.get("is_ceo") is True):
            continue
        name = (e.get("person") or "").strip()
        if not name:
            continue
        evidence_text = (e.get("evidence") or "")
        if NEG_INCOMING.search(evidence_text):
            continue
        pat = re.compile(rf"(.{{0,160}})(?:CEO|Chief Executive Officer|principal executive officer)(.{{0,160}}{re.escape(name)}.{{0,160}})", re.IGNORECASE)
        if pat.search(context):
            confidence = 0.7
            if e.get("effective_date"): confidence += 0.2
            if re.search(r"effective|effective immediately|effective on", evidence_text, re.IGNORECASE): confidence += 0.05
            checked.append({
                "name": name,
                "phrase": evidence_text[:300],
                "date": (e.get("effective_date") or "").strip(),
                "confidence": round(min(confidence, 0.98), 2),
            })
        else:
            for sent in re.split(r"(?<=[\.!?])\s+", context):
                if name in sent and CEO_TOKEN.search(sent) and DEPARTURE_VERBS.search(sent) and not NEG_INCOMING.search(sent):
                    confidence = 0.65
                    if e.get("effective_date"): confidence += 0.2
                    if re.search(r"effective|effective immediately|effective on", sent, re.IGNORECASE): confidence += 0.05
                    checked.append({
                        "name": name,
                        "phrase": (e.get("evidence") or sent)[:300],
                        "date": (e.get("effective_date") or "").strip(),
                        "confidence": round(min(confidence, 0.95), 2),
                    })
                    break
    return checked

# --------------------
# Company input parsing
# --------------------

def parse_company_inputs(upload: Optional[io.BytesIO], manual_text: str, ua: str) -> List[int]:
    ciks: List[int] = []; tickers: List[str] = []
    if upload is not None:
        df = pd.read_csv(upload)
        cols = {c.lower(): c for c in df.columns}
        if "cik" in cols: ciks += [int(str(x).strip()) for x in df[cols["cik"]].dropna().tolist()]
        if "cik_str" in cols: ciks += [int(str(x).strip()) for x in df[cols["cik_str"]].dropna().tolist()]
        if "ticker" in cols: tickers += [str(x).strip() for x in df[cols["ticker"]].dropna().tolist()]
    if manual_text:
        for tok in re.split(r"[\s,]+", manual_text.strip()):
            if not tok: continue
            if tok.isdigit(): ciks.append(int(tok))
            else: tickers.append(tok)
    tmap = load_ticker_map(ua) if tickers else {}
    for t in tickers:
        cik = tmap.get(t.upper());
        if cik: ciks.append(cik)
        else: st.warning(f"Ticker '{t}' not found – skipping.")
    return sorted(set(int(x) for x in ciks if str(x).isdigit()))

# --------------------
# Scanning
# --------------------

def scan_cik_for_ceo_departures(cik: int, ua: str, delay_s: float, use_full_history_flag: bool, use_gemini: bool, api_key: str, model_name: str) -> List[Dict[str, str]]:
    try:
        js = fetch_submissions(cik, ua)
        company = js.get("name", "")
    except Exception:
        company = ""

    s = requests.Session(); s.headers.update(ARCHIVE_HEADERS(ua))
    out: List[Dict[str, str]] = []

    if use_full_history_flag:
        filings = fetch_extended_history(cik, ua)
    else:
        try:
            js = fetch_submissions(cik, ua)
            filings = list(iter_filings_from_submissions(js))
        except Exception:
            filings = []
    for row in filings:
        if not row.get("form", "").upper().startswith("8-K"): continue
        acc, primary, fdate = row.get("accessionNumber"), row.get("primaryDocument"), row.get("filingDate")
        if not acc or not primary: continue
        if start_date and fdate and fdate < start_date.strftime("%Y-%m-%d"): continue
        if end_date and fdate and fdate > end_date.strftime("%Y-%m-%d"): continue

        # Use filing index to pick the main document when possible
        chosen_doc = pick_best_primary_doc(s, cik, acc, primary)
        url = filing_doc_url(cik, acc, chosen_doc)
        try:
            # Enumerate all candidate documents in this filing and scan each
            docs = enumerate_candidate_docs(s, cik, acc, chosen_doc)
            sections_all: List[str] = []
            for fname in docs:
                doc_url = filing_doc_url(cik, acc, fname)
                try:
                    text = get_text_from_document(s, doc_url)
                except Exception:
                    text = None
                if not text:
                    continue
                sections = extract_all_item_502_sections(text)
                if sections:
                    sections_all.extend(sections)
                # Early exit if we already found sections in an HTML/TXT doc
                if sections_all and (fname.lower().endswith((".htm", ".html", ".txt"))):
                    break
            if not sections_all:
                time.sleep(delay_s); continue
            joined = "\n\n---\n\n".join(sections_all)
            if use_gemini:
                events = extract_ceo_departures_gemini(joined, api_key, model_name)
            else:
                events = extract_ceo_departures_heuristic(joined)
            if not events:
                time.sleep(delay_s); continue
            for ev in events:
                out.append({
                    "company": company,
                    "cik": f"{int(cik):010d}",
                    "filingDate": fdate,
                    "accessionNumber": acc,
                    "ceo_name": ev.get("name", ""),
                    "departing": True,
                    "context": ev.get("phrase", ""),
                    "effective_date": ev.get("date", ""),
                    "confidence": ev.get("confidence", 0.6),
                    "documentUrl": url,
                    "filingDetailUrl": filing_index_url(cik, acc),
                })
        except Exception as e:
            out.append({
                "company": company,
                "cik": f"{int(cik):010d}",
                "filingDate": fdate,
                "accessionNumber": acc or "",
                "ceo_name": "",
                "departing": False,
                "context": f"ERROR: {e}",
                "effective_date": "",
                "confidence": 0.0,
                "documentUrl": url,
                "filingDetailUrl": filing_index_url(cik, acc),
            })
        time.sleep(delay_s)
    return out

# --------------------
# Run
# --------------------
if run_btn:
    if not ua or "@" not in ua:
        st.error("Please provide a descriptive User-Agent string with a contact email.")
        st.stop()

    ciks = parse_company_inputs(uploaded, manual, ua)
    if not ciks:
        st.warning("No valid CIKs or tickers provided.")
        st.stop()

    st.success(f"Processing {len(ciks)} CIK(s)…")
    bar = st.progress(0)
    rows: List[Dict[str, str]] = []
    # Placeholders for incremental UI
    results_placeholder = st.empty()
    log_placeholder = st.empty()
    log_lines: List[str] = []
    # Build a CIK->ticker map for nicer progress messages
    try:
        _tmap = load_ticker_map(ua)
        cik_to_ticker = {cik: t for t, cik in _tmap.items()}
    except Exception:
        cik_to_ticker = {}

    delay = 1.0 / max(rps, 0.2)
    for i, cik in enumerate(ciks, start=1):
        prev_n = len(rows)
        with st.spinner(f"Scanning CIK {int(cik):010d} for CEO departures"):
            new_rows = scan_cik_for_ceo_departures(cik, ua, delay, use_full_history, use_gemini, gemini_key, gemini_model)
            rows += new_rows
        # Log per-ticker completion
        label = cik_to_ticker.get(int(cik)) or fetch_company_name(cik, ua) or f"CIK {int(cik):010d}"
        num_new = len(rows) - prev_n
        plural = "s" if num_new != 1 else ""
        log_lines.append(f"{label} finished searching ({num_new} result{plural})")
        log_placeholder.markdown("\n".join(f"- {m}" for m in log_lines))
        # Incremental results table
        if rows:
            df_live = pd.DataFrame(rows)
            results_placeholder.dataframe(
                df_live.sort_values(["filingDate", "company"], ascending=[False, True]),
                use_container_width=True,
                height=560,
                column_config={
                    "documentUrl": st.column_config.LinkColumn(label="Open 8-K", display_text="Open 8-K"),
                    "filingDetailUrl": st.column_config.LinkColumn(label="Index", display_text="Index"),
                    "departing": st.column_config.CheckboxColumn(label="Departing"),
                    "ceo_name": st.column_config.TextColumn(label="CEO Name"),
                    "effective_date": st.column_config.TextColumn(label="Effective Date"),
                    "confidence": st.column_config.NumberColumn(label="Confidence", help="0-1 score", format="%0.2f"),
                    "context": st.column_config.TextColumn(label="Context (snippet)", width="medium"),
                }
            )
        bar.progress(int(i / len(ciks) * 100))

    if not rows:
        st.warning("No CEO departures detected in Item 5.02 for the selected companies/date range.")
        st.stop()

    df = pd.DataFrame(rows)

    st.markdown("### 2) CEO departures (Item 5.02)")
    st.dataframe(
        df.sort_values(["filingDate", "company"], ascending=[False, True]),
        use_container_width=True,
        height=560,
        column_config={
            "documentUrl": st.column_config.LinkColumn(label="Open 8-K", display_text="Open 8-K"),
            "filingDetailUrl": st.column_config.LinkColumn(label="Index", display_text="Index"),
            "departing": st.column_config.CheckboxColumn(label="Departing"),
            "ceo_name": st.column_config.TextColumn(label="CEO Name"),
            "effective_date": st.column_config.TextColumn(label="Effective Date"),
            "confidence": st.column_config.NumberColumn(label="Confidence", help="0-1 score", format="%0.2f"),
            "context": st.column_config.TextColumn(label="Context (snippet)", width="medium"),
        }
    )

    csv = df[["company","cik","filingDate","ceo_name","effective_date","confidence","documentUrl","filingDetailUrl"]].to_csv(index=False).encode("utf-8")
    st.download_button("⬇️ Download CSV", data=csv, file_name="ceo_departures_item_5_02.csv", mime="text/csv")

    st.info("Only CEO departures are shown. Director-only changes and other officers are intentionally excluded. If Gemini is unavailable, a strict heuristic with confidence scoring is used.")
