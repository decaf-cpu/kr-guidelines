"""학회지에 실린 진료지침·권고문 수집 (Europe PMC REST API).

학회 홈페이지의 지침 게시판이 회원 전용이거나 robots.txt로 막혀 있어도, 같은 지침을 학회지(대부분
open access)에 논문으로 싣는 경우가 많다. 예: 대한류마티스학회 → Journal of Rheumatic Diseases.
학회지 사이트를 직접 긁지 않고, PubMed·PMC 색인을 공개 API로 제공하는 Europe PMC에서 ISSN과
제목 검색어로 찾는다. (www.ebi.ac.uk/robots.txt는 /europepmc/webservices/rest를 막지 않고
Crawl-delay 10초를 요구 → Fetcher가 그 간격을 지킨다.)

설정 (config/societies.yaml):

    journals:
      - issn: 2233-4718
        name: Journal of Rheumatic Diseases
        url: https://www.jrd.or.kr      # 학회 배지 링크 (생략하면 Europe PMC 검색 화면)
"""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import quote, urlencode

from .filters import is_guideline, lang_of, tags_for
from .parse import canonical_url
from .store import Store, item_id, today

log = logging.getLogger(__name__)

API = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
TITLE_TERMS = (
    '(TITLE:guideline* OR TITLE:recommendation* OR TITLE:consensus OR TITLE:"position statement" '
    'OR TITLE:"expert opinion" OR TITLE:지침 OR TITLE:권고)'
)
# 정정·논평 등 지침 본문이 아닌 글
SKIP_PUBTYPE = re.compile(r"(erratum|correction|retraction|comment|letter|editorial)", re.I)
# 지침을 다룬 연구·리뷰 (지침 자체가 아님)
NOISE = re.compile(
    r"(^\s*(corrigendum|erratum|correction|retraction|reply|response)\b|"
    r"review of (the )?(recent|current|international|existing|global)|\bsurvey\b|questionnaire|"
    r"adherence|awareness|implementation of|agreement (on|with)|compliance|appraisal|\bAGREE\b|"
    r"current status|trends in|comparison of|comparative|commentary|perspectives? on)",
    re.I,
)


def search_url(issn: str, cursor: str = "*", page_size: int = 100) -> str:
    q = f'ISSN:"{issn}" AND {TITLE_TERMS}'
    return API + "?" + urlencode(
        {"query": q, "format": "json", "resultType": "lite", "pageSize": page_size,
         "sort": "P_PDATE_D desc", "cursorMark": cursor},
        quote_via=quote,
    )


def browse_url(issn: str) -> str:
    return "https://europepmc.org/search?query=" + quote(f'ISSN:"{issn}" AND {TITLE_TERMS}')


def clean_title(t: str) -> str:
    t = re.sub(r"\s+", " ", t or "").strip()
    if t.startswith("[") and t.endswith("].") or t.startswith("[") and t.endswith("]"):
        t = t.strip(".").strip("[]")  # PubMed: 국문 논문의 영문 번역 제목은 [ ]로 감싼다
    return t.rstrip(".").strip()


def keep(r: dict) -> bool:
    title = clean_title(r.get("title", ""))
    if not title or SKIP_PUBTYPE.search(r.get("pubType") or "") or NOISE.search(title):
        return False
    return is_guideline(title)  # 외국 학회 지침 소개 등은 여기서 걸러진다


def to_fields(r: dict, soc: dict, journal: dict, board: str) -> dict:
    title = clean_title(r["title"])
    pmid, pmcid, doi = r.get("pmid"), r.get("pmcid"), r.get("doi")
    if pmcid:
        url = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"
    elif doi:
        url = f"https://doi.org/{doi}"
    else:
        url = f"https://europepmc.org/article/{r.get('source', 'MED')}/{r.get('id')}"
    d = r.get("firstPublicationDate")
    date, prec = (d, "day") if d and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) else (r.get("pubYear"), "year" if r.get("pubYear") else None)
    files = [{"name": "PubMed", "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/", "kind": "PubMed"}] if pmid else []
    return {
        "society": soc["id"],
        "board": board,
        "board_name": journal.get("name") or f"ISSN {journal['issn']}",
        "title": title,
        "url": url,
        "link_resolved": True,
        "lang": lang_of(title),
        "tags": tags_for(title),
        "attachments": files,
        "doi": f"https://doi.org/{doi}" if doi else None,
        "pmid": pmid,
        "journal": True,
        "date": date,
        "date_precision": prec,
        "date_source": "journal",
    }


def crawl_journal(fetcher, soc: dict, journal: dict, store: Store, mode: str = "incremental") -> dict:
    issn = str(journal["issn"])
    board = journal.get("url") or browse_url(issn)
    result = {"url": board, "name": journal.get("name") or f"ISSN {issn}", "source": "journal",
              "pages": 0, "rows": 0, "matched": 0, "new": 0, "members_only": 0, "error": None}
    first_time = not any(it.get("board") == board for it in store.items.values())
    cursor, pages = "*", (10 if mode == "full" else 1)
    for _ in range(pages):
        r = fetcher.get(search_url(issn, cursor))
        if r is None or r.status_code >= 400:
            code = getattr(r, "status_code", None)
            result["error"] = f"Europe PMC 접속 실패 (HTTP {code})" if code else "Europe PMC 접속 실패"
            return result
        try:
            doc = json.loads(r.content)
        except ValueError:
            result["error"] = "Europe PMC 응답을 읽지 못함"
            return result
        rows = (doc.get("resultList") or {}).get("result") or []
        result["pages"] += 1
        result["rows"] += len(rows)
        for row in rows:
            if not keep(row):
                continue
            result["matched"] += 1
            fields = to_fields(row, soc, journal, board)
            iid = item_id(soc["id"], canonical_url(fields["url"]), fields["title"], board)  # Store가 읽을 때 다시 계산하는 id와 같게
            it, is_new = store.upsert(iid, fields)
            it["access"] = "public" if row.get("isOpenAccess") == "Y" or row.get("pmcid") else it.get("access", "unknown")
            it["checked"] = today()
            if is_new:
                result["new"] += 1
                it["backfill"] = first_time
        nxt = doc.get("nextCursorMark")
        if not rows or not nxt or nxt == cursor:
            break
        cursor = nxt
    return result
