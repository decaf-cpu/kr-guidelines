"""학회지에 실린 진료지침·권고문 수집 (Europe PMC REST API).

학회 홈페이지의 지침 게시판이 회원 전용이거나 robots.txt로 막혀 있어도, 같은 지침을 학회지(대부분
open access)에 논문으로 싣는 경우가 많다. 예: 대한류마티스학회 → Journal of Rheumatic Diseases.
학회지 사이트를 직접 긁지 않고, PubMed·PMC 색인을 공개 API로 제공하는 Europe PMC에서 ISSN과
제목 검색어로 찾는다. (www.ebi.ac.uk/robots.txt는 /europepmc/webservices/rest를 막지 않고
Crawl-delay 10초를 요구 → Fetcher가 그 간격을 지킨다.)

설정 (config/societies.yaml):

    journals:
      - issn: [2093-596X, 2093-5978]   # 인쇄판·전자판 (하나만 적어도 됨)
        name: Endocrinology and Metabolism
        url: https://www.e-enm.org      # 학회 배지 링크 (생략하면 Europe PMC 검색 화면)
        korean_only: true               # 기본값. 제목·저자(단체명)에 Korea가 없는 글은 제외 (외국 지침 리뷰 등)

학회지에는 다른 학회의 지침도 실린다 (예: 내분비학회지 EnM에 갑상선학회 지침). 제목이나 저자 단체명에
societies.yaml의 name_en(영문 학회명)이 있으면 그 학회의 지침으로 올린다. 없으면 학회지를 내는 학회.
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
# 정정·논평 등 지침 본문이 아닌 글 (단, 'consensus statement'·'practice guideline'으로 분류된 글은 살림)
SKIP_PUBTYPE = re.compile(r"(erratum|correction|retraction|comment|letter|editorial)", re.I)
DOC_PUBTYPE = re.compile(r"(consensus statement|practice guideline|guideline)", re.I)
# 지침을 다룬 연구·리뷰·해설 (지침 자체가 아님)
NOISE = re.compile(
    r"(^\s*(corrigendum|erratum|correction|retraction|reply|response|contemporary|current|recent|highlights?|redefining|revisiting)\b|"
    r"review of (the )?(recent|current|international|existing|global)|systematic review|narrative review|\bsurvey\b|questionnaire|"
    r"adherence|awareness|implementation of|agreement (on|with|between)|compliance|appraisal|\bAGREE\b|"
    r"current status|trends in|comparison|comparative|commentary|perspectives? on|lessons from|key updates|quality indicators?|developing|"
    r"differences (from|between)|a focus on|insights|achievement of|effects of|implications of|\bassociations? (with|between)|\bvs\.?\s|versus)",
    re.I,
)
# 연구 논문(research-article)은 제목이 지침 문서 형식일 때만
DOC_TITLE = re.compile(
    r"(position statement|consensus (guidelines?|statement|report|recommendations?)|"
    r"(clinical )?(practice )?guidelines? (for|on)|recommendations? (for|on))",
    re.I,
)
KOREA = re.compile(r"\bkorea", re.I)


def issns(journal: dict) -> list[str]:
    v = journal.get("issn") or []
    return [str(x) for x in (v if isinstance(v, list) else [v])]


def _issn_q(issn) -> str:
    vals = issn if isinstance(issn, list) else [issn]
    q = " OR ".join(f'ISSN:"{x}"' for x in vals)
    return f"({q})" if len(vals) > 1 else q


def _base_q(journal) -> str:
    """학회지(ISSN) 또는 학회 이름 검색식(query: 다른 학술지에 실린 학회 지침까지)."""
    if isinstance(journal, dict):
        return f"({journal['query']})" if journal.get("query") else _issn_q(journal["issn"])
    return _issn_q(journal)


def search_url(issn, cursor: str = "*", page_size: int = 100) -> str:
    q = f"{_base_q(issn)} AND {TITLE_TERMS}"
    return API + "?" + urlencode(
        {"query": q, "format": "json", "resultType": "lite", "pageSize": page_size,
         "sort": "P_PDATE_D desc", "cursorMark": cursor},
        quote_via=quote,
    )


def browse_url(issn) -> str:
    return "https://europepmc.org/search?query=" + quote(f"{_base_q(issn)} AND {TITLE_TERMS}")


def clean_title(t: str) -> str:
    t = re.sub(r"\s+", " ", t or "").strip()
    if t.startswith("[") and t.endswith("].") or t.startswith("[") and t.endswith("]"):
        t = t.strip(".").strip("[]")  # PubMed: 국문 논문의 영문 번역 제목은 [ ]로 감싼다
    return t.rstrip(".").strip()


def keep(r: dict, korean_only: bool = True) -> bool:
    title = clean_title(r.get("title", ""))
    pt = r.get("pubType") or ""
    if not title or NOISE.search(title):
        return False
    if SKIP_PUBTYPE.search(pt) and not DOC_PUBTYPE.search(pt):
        return False
    if "research" in pt.lower() and not DOC_PUBTYPE.search(pt) and not DOC_TITLE.search(title):
        return False
    if korean_only and not (KOREA.search(title) or KOREA.search(r.get("authorString") or "")):
        return False
    return is_guideline(title)  # 외국 학회 지침 소개 등은 여기서 걸러진다


_STOP = {"the", "and", "of", "for", "on", "in"}


def _tokens(text: str) -> str:
    """학회명 표기 차이(쉼표·of/for·단복수·대소문자)를 무시하고 비교하려고 단어만 남긴다."""
    ws = [w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in _STOP]
    return " " + " ".join(w[:-1] if len(w) > 3 and w.endswith("s") else w for w in ws) + " "


def _find(name: str, text: str) -> int:
    return _tokens(text).find(_tokens(name))


def owner_society(r: dict, owner: str, names: dict[str, list[str]]) -> str:
    """제목 → 저자 단체명 순으로 영문 학회명을 찾아 그 학회 id. 학회지를 내는 학회가 언급되면 그 학회 우선."""
    for text in (clean_title(r.get("title", "")), r.get("authorString") or ""):
        pos = {sid: min(i for n in ns if (i := _find(n, text)) >= 0) for sid, ns in names.items()
               if any(_find(n, text) >= 0 for n in ns)}
        if owner in pos:
            return owner
        if pos:
            return min(pos, key=pos.get)  # 여러 학회가 나오면 글에서 먼저 나온 학회
    return owner


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
        "board_name": journal.get("name") or f"ISSN {(issns(journal) or ['?'])[0]}",
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


def crawl_journal(fetcher, soc: dict, journal: dict, store: Store, mode: str = "incremental",
                  societies: list[dict] | None = None) -> dict:
    q = journal if journal.get("query") else issns(journal)
    board = journal.get("url") or browse_url(q)
    result = {"url": board, "name": journal.get("name") or f"ISSN {issns(journal)[0]}", "source": "journal",
              "pages": 0, "rows": 0, "matched": 0, "new": 0, "members_only": 0, "error": None, "routed": {}}
    names = {s["id"]: ([s["name_en"]] if isinstance(s.get("name_en"), str) else list(s.get("name_en") or []))
             for s in (societies or [soc])}
    names = {k: v for k, v in names.items() if v}
    by_id = {s["id"]: s for s in (societies or [soc])}
    korean_only = journal.get("korean_only", True)
    first_time = not any(it.get("board") == board for it in store.items.values())
    cursor, pages = "*", (10 if mode == "full" else 1)
    for _ in range(pages):
        r = fetcher.get(search_url(q, cursor))
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
            if not keep(row, korean_only):
                continue
            sid = owner_society(row, soc["id"], names)
            if journal.get("query") and sid != soc["id"]:
                continue  # 학회 이름 검색은 그 학회 지침만 (다른 학회 지침은 그 학회 학회지에서)
            target = by_id.get(sid, soc)
            result["matched"] += 1
            if target["id"] != soc["id"]:
                result["routed"][target["id"]] = result["routed"].get(target["id"], 0) + 1
            fields = to_fields(row, target, journal, board)
            fields["journal_of"] = soc["id"]
            iid = item_id(target["id"], canonical_url(fields["url"]), fields["title"], board)  # Store가 읽을 때 다시 계산하는 id와 같게
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
