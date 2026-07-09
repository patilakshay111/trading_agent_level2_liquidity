from __future__ import annotations

from datetime import date
from io import BytesIO, StringIO
import csv
import json
import re
from typing import Any
from urllib.parse import urljoin
from urllib.request import Request, urlopen


CME_DAILY_BULLETIN_URL = "https://www.cmegroup.com/market-data/daily-bulletin.html"
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
_MONTHS = {
    "JAN": "01",
    "FEB": "02",
    "MAR": "03",
    "APR": "04",
    "MAY": "05",
    "JUN": "06",
    "JUL": "07",
    "AUG": "08",
    "SEP": "09",
    "OCT": "10",
    "NOV": "11",
    "DEC": "12",
}


def fetch_gc_options_positioning(config: dict[str, Any], *, timeout: float = 20.0) -> dict[str, Any]:
    cfg = config.get("options_positioning", {})
    source_url = str(cfg.get("source_url") or "").strip()
    if source_url:
        payload = _fetch_text_or_bytes(source_url, timeout)
        rows = _parse_configured_payload(payload, source_url)
        return {"ok": bool(rows), "rows": rows, "source": source_url, "message": f"Parsed {len(rows)} rows from configured source."}

    errors: list[str] = []
    try:
        bulletin_html = _fetch_text(CME_DAILY_BULLETIN_URL, timeout)
    except Exception as exc:  # noqa: BLE001 - CME may block the landing page while PDF links still work
        errors.append(f"{CME_DAILY_BULLETIN_URL}: {exc}")
        bulletin_html = ""
    pdf_urls = _candidate_bulletin_pdfs(bulletin_html)
    for pdf_url in pdf_urls:
        try:
            pdf_bytes = _fetch_bytes(pdf_url, timeout)
            text = _pdf_text(pdf_bytes)
            rows = parse_cme_gold_options_text(text, source=pdf_url)
            if rows:
                return {"ok": True, "rows": rows, "source": pdf_url, "message": f"Parsed {len(rows)} GC options OI rows from CME bulletin."}
        except Exception as exc:  # noqa: BLE001 - try the next bulletin candidate
            errors.append(f"{pdf_url}: {exc}")
    return {
        "ok": False,
        "rows": [],
        "source": CME_DAILY_BULLETIN_URL,
        "message": "Could not parse GC options open interest from CME daily bulletin.",
        "errors": errors[:5],
    }


def _fetch_text_or_bytes(url: str, timeout: float) -> str | bytes:
    data = _fetch_bytes(url, timeout)
    if url.lower().endswith(".pdf"):
        return data
    return data.decode("utf-8", errors="replace")


def _fetch_text(url: str, timeout: float) -> str:
    return _fetch_bytes(url, timeout).decode("utf-8", errors="replace")


def _fetch_bytes(url: str, timeout: float) -> bytes:
    request = Request(url, headers={"User-Agent": _USER_AGENT})
    with urlopen(request, timeout=timeout) as response:
        return response.read()


def _candidate_bulletin_pdfs(html: str) -> list[str]:
    urls: list[str] = []
    for href in re.findall(r'href=["\']([^"\']+\.pdf[^"\']*)["\']', html, flags=re.IGNORECASE):
        absolute = urljoin(CME_DAILY_BULLETIN_URL, href)
        lowered = absolute.lower()
        if "bulletin" in lowered and ("metal" in lowered or "option" in lowered):
            urls.append(absolute)
    if not urls:
        urls.extend(
            [
                "https://www.cmegroup.com/ftp/bulletin/Section64_Metals_Options.pdf",
                "https://www.cmegroup.com/ftp/bulletin/Section63_Metals_Options.pdf",
                "https://www.cmegroup.com/ftp/bulletin/Section62_Metals_Options.pdf",
                "https://www.cmegroup.com/ftp/bulletin/Section36_Metals_Options.pdf",
            ]
        )
    return list(dict.fromkeys(urls))


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _parse_configured_payload(payload: str | bytes, source_url: str) -> list[dict[str, Any]]:
    if isinstance(payload, bytes):
        return parse_cme_gold_options_text(_pdf_text(payload), source=source_url)
    stripped = payload.strip()
    if stripped.startswith("[") or stripped.startswith("{"):
        data = json.loads(stripped)
        rows = data if isinstance(data, list) else data.get("rows", [])
        return [_normalize_option_row(row, source_url) for row in rows if _normalize_option_row(row, source_url)]
    reader = csv.DictReader(StringIO(payload))
    return [_normalize_option_row(row, source_url) for row in reader if _normalize_option_row(row, source_url)]


def _normalize_option_row(row: dict[str, Any], source: str) -> dict[str, Any] | None:
    try:
        strike = float(row.get("strike") or row.get("strike_price") or row.get("Strike") or 0.0)
    except (TypeError, ValueError):
        return None
    if strike <= 0:
        return None
    return {
        "as_of_date": str(row.get("as_of_date") or row.get("trade_date") or date.today().isoformat()),
        "underlying": str(row.get("underlying") or "GC").upper(),
        "expiry": str(row.get("expiry") or row.get("expiration") or "").strip() or None,
        "strike": strike,
        "call_oi": float(row.get("call_oi") or row.get("call_open_interest") or 0.0),
        "put_oi": float(row.get("put_oi") or row.get("put_open_interest") or 0.0),
        "call_volume": float(row.get("call_volume") or 0.0),
        "put_volume": float(row.get("put_volume") or 0.0),
        "source": str(row.get("source") or source),
    }


def parse_cme_gold_options_text(text: str, *, source: str = "cme_daily_bulletin") -> list[dict[str, Any]]:
    rows: dict[tuple[str, float], dict[str, Any]] = {}
    active_side: str | None = None
    active_expiry: str | None = None
    as_of_date = _extract_as_of_date(text)

    for raw_line in text.splitlines():
        line = " ".join(raw_line.replace(",", "").split())
        upper = line.upper()
        if not line:
            continue
        if "GOLD" in upper and "OPTION" in upper:
            if "CALL" in upper:
                active_side = "CALL"
            elif "PUT" in upper:
                active_side = "PUT"
        if "CALL" in upper and ("GOLD" in upper or active_side):
            active_side = "CALL"
        if "PUT" in upper and ("GOLD" in upper or active_side):
            active_side = "PUT"
        expiry = _extract_expiry(upper)
        if expiry:
            active_expiry = expiry
        if not active_side:
            continue
        parsed = _parse_oi_line(line, active_expiry)
        if not parsed:
            continue
        key = (parsed["expiry"] or "", parsed["strike"])
        item = rows.setdefault(
            key,
            {
                "as_of_date": as_of_date,
                "underlying": "GC",
                "expiry": parsed["expiry"],
                "strike": parsed["strike"],
                "call_oi": 0.0,
                "put_oi": 0.0,
                "call_volume": 0.0,
                "put_volume": 0.0,
                "source": source,
            },
        )
        if active_side == "CALL":
            item["call_oi"] = parsed["open_interest"]
            item["call_volume"] = parsed["volume"]
        else:
            item["put_oi"] = parsed["open_interest"]
            item["put_volume"] = parsed["volume"]

    return sorted(
        [row for row in rows.values() if row["strike"] > 0 and (row["call_oi"] > 0 or row["put_oi"] > 0)],
        key=lambda row: (str(row.get("expiry") or ""), float(row["strike"])),
    )


def _parse_oi_line(line: str, active_expiry: str | None) -> dict[str, Any] | None:
    numbers = [token for token in re.findall(r"(?<![A-Z])\d+(?:\.\d+)?", line) if token]
    if len(numbers) < 3:
        return None
    strike = None
    strike_index = -1
    strike_candidates = [(index, float(token)) for index, token in enumerate(numbers) if 1000.0 <= float(token) <= 10000.0]
    for candidate_number, (index, value) in enumerate(strike_candidates):
        if 2000.0 <= value <= 2099.0 and candidate_number < len(strike_candidates) - 1:
            continue
        strike = value
        strike_index = index
        break
    if strike is None:
        return None
    tail = [float(token) for token in numbers[strike_index + 1 :]]
    if len(tail) < 2:
        tail = [float(token) for token in numbers[-2:]]
    open_interest = tail[-1]
    volume = tail[-2] if len(tail) >= 2 else 0.0
    expiry = _extract_expiry(line.upper()) or active_expiry
    return {"expiry": expiry, "strike": strike, "volume": volume, "open_interest": open_interest}


def _extract_expiry(text: str) -> str | None:
    match = re.search(r"\b(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\s*(\d{2,4})\b", text)
    if not match:
        return None
    year = match.group(2)
    if len(year) == 2:
        year = f"20{year}"
    return f"{year}-{_MONTHS[match.group(1)]}"


def _extract_as_of_date(text: str) -> str:
    match = re.search(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b", text)
    if match:
        year, month, day = match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"
    return date.today().isoformat()
