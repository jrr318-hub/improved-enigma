
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


def clean_html(text: str) -> str:
    text = TAG_STRIPPER.sub(" ", text); return re.sub(r"\s+", " ", text)


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

# --------------------
# Heuristic fallback (strict)
# --------------------

def extract_ceo_departures_heuristic(text: str) -> List[Dict[str, str]]:
    events: List[Dict[str, str]] = []
    sentences = re.split(r"(?<=[\.!?])\s+", text)
    for sent in sentences:
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
        d = DATE_RE.search(sent)
        ev = {"name": name, "phrase": sent.strip()[:300]}
        if d: ev["date"] = d.group(0)
        events.append(ev)
    return events

# --------------------
# Gemini extraction
# --------------------

def extract_ceo_departures_gemini(text: str, api_key: str, model_name: str) -> List[Dict[str, str]]:
    try:
        import google.generativeai as genai
    except Exception:
        st.warning("`google-generativeai` not installed. Falling back to heuristic.")
        return extract_ceo_departures_heuristic(text)

    if not api_key:
        st.warning("No Gemini API key provided — using heuristic only.")
        return extract_ceo_departures_heuristic(text)

    genai.configure(api_key=api_key)

    # Prefer the Item 5.02 section strictly
    section = extract_item_502_section(text)
    narrowed = section if section else text

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

    model = genai.GenerativeModel(model_name=model_name, system_instruction=system)

    resp = model.generate_content(
        [
            {"role": "user", "parts": [
                "Extract CEO departures only from this 8-K text (Item 5.02):\n\n",
                context
            ]}
        ],
        generation_config={
            "temperature": 0.2,
            "response_mime_type": "application/json",
            "response_schema": response_schema,
            "max_output_tokens": 800,
        },
    )

    try:
        data = json.loads(resp.text)
    except Exception:
        # If the model didn't adhere to JSON, fallback
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
        # reject if the evidence reads like an incoming appointment
        evidence_text = (e.get("evidence") or "")
        if NEG_INCOMING.search(evidence_text):
            continue
        pat = re.compile(rf"(.{{0,120}})(?:CEO|Chief Executive Officer|principal executive officer)(.{{0,120}}{re.escape(name)}.{{0,120}})", re.IGNORECASE)
        if pat.search(context):
            checked.append({
                "name": name,
                "phrase": evidence_text[:300],
                "date": (e.get("effective_date") or "").strip(),
            })
        else:
            # fallback sentence check
            for sent in re.split(r"(?<=[\.!?])\s+", context):
                if name in sent and CEO_TOKEN.search(sent) and DEPARTURE_VERBS.search(sent) and not NEG_INCOMING.search(sent):
                    checked.append({
                        "name": name,
                        "phrase": (e.get("evidence") or sent)[:300],
                        "date": (e.get("effective_date") or "").strip(),
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

        url = filing_doc_url(cik, acc, primary)
        try:
            resp = s.get(url, timeout=60); resp.raise_for_status(); resp.encoding = resp.apparent_encoding or "utf-8"
            text = clean_html(resp.text)
            section = extract_item_502_section(text)
            if not section:
                time.sleep(delay_s); continue
            if use_gemini:
                events = extract_ceo_departures_gemini(section, api_key, model_name)
            else:
                events = extract_ceo_departures_heuristic(section)
            if not events:
                time.sleep(delay_s); continue
            for ev in events:
                out.append({
                    "company": company,
                    "cik": f"{int(cik):010d}",
                    "filingDate": fdate,
                    "accessionNumber": acc,
                    "ceo_name": ev.get("name", ""),
                    "departing": True,  # departures only
                    "context": ev.get("phrase", ""),
                    "effective_date": ev.get("date", ""),
                    "documentUrl": url,
                    "filingDetailUrl": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}-index.html",
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
                "documentUrl": url,
                "filingDetailUrl": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{(acc or '').replace('-', '')}-index.html",
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

    delay = 1.0 / max(rps, 0.2)
    for i, cik in enumerate(ciks, start=1):
        with st.spinner(f"Scanning CIK {int(cik):010d} for CEO departures"):
            rows += scan_cik_for_ceo_departures(cik, ua, delay, use_full_history, use_gemini, gemini_key, gemini_model)
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
            "context": st.column_config.TextColumn(label="Context (snippet)", width="medium"),
        }
    )

    csv = df[["company","cik","filingDate","ceo_name","effective_date","documentUrl","filingDetailUrl"]].to_csv(index=False).encode("utf-8")
    st.download_button("⬇️ Download CSV", data=csv, file_name="ceo_departures_item_5_02.csv", mime="text/csv")

    st.info("Only CEO departures are shown. Director-only changes and other officers are intentionally excluded. If no API key is set, a strict heuristic is used.")
