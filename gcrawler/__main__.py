"""명령줄 실행.

  python -m gcrawler crawl                 # 증분 수집 (게시판당 최대 3쪽)
  python -m gcrawler crawl --full          # 전체 수집 (게시판당 최대 30쪽)
  python -m gcrawler crawl --society kasl  # 특정 학회만
  python -m gcrawler check kasl            # 저장 없이 파싱 결과만 출력 (설정 점검용)
  python -m gcrawler build                 # _site/ 정적 사이트 생성
"""
from __future__ import annotations

import argparse
import logging
import sys

from . import adapters
from .crawl import run
from .discover import discover
from .fetch import Fetcher
from .filters import is_guideline
from .parse import extract_rows
from .store import load_societies


def cmd_check(args) -> int:
    socs = {s["id"]: s for s in load_societies()}
    soc = socs.get(args.society)
    if soc is None:
        print(f"알 수 없는 학회 id: {args.society}")
        return 2
    f = Fetcher(respect_robots=not args.ignore_robots)
    boards = soc.get("boards") or []
    if not boards:
        res = discover(f, soc["homepage"], verify=soc.get("verify_ssl", True))
        print(f"[자동 탐색] {soc['homepage']} → {res.get('error') or '후보 ' + str(len(res['candidates'])) + '개'}")
        for c in res.get("candidates", []):
            print(f"   점수 {c['score']}  {c['label']}  {c['url']}")
        boards = [{"url": c["url"], "name": c["label"], "dedicated": True} for c in res.get("candidates", [])]
    adapter = adapters.load(soc["id"])
    for j in soc.get("journals") or []:
        import json as _json
        from .journal import issns, keep, search_url
        r = f.get(search_url(issns(j)))
        print(f"\n== 학회지 {j.get('name') or ''} (ISSN {j['issn']}) — Europe PMC")
        if r is None or r.status_code >= 400:
            print(f"   접속 실패 (HTTP {getattr(r, 'status_code', None)})")
            continue
        rows = (_json.loads(r.content).get("resultList") or {}).get("result") or []
        print(f"   검색 결과 {len(rows)}건")
        for row in rows[: args.limit]:
            print(f"   {'✔' if keep(row, j.get('korean_only', True)) else '·'} {row.get('firstPublicationDate') or row.get('pubYear') or '----------'}  {row.get('title', '')[:70]}")
    if soc.get("enabled") is False:
        boards = []  # 꺼진 학회는 학회지만 확인
    for b in boards:
        html, r = f.get_html(b["url"], verify=soc.get("verify_ssl", True))
        print(f"\n== {b.get('name') or ''} {b['url']}")
        if html is None:
            print(f"   접속 실패 (HTTP {getattr(r, 'status_code', None)})")
            continue
        rows = adapter.parse_list(html, r.url, b) if adapter and hasattr(adapter, "parse_list") else extract_rows(html, r.url, b)
        print(f"   행 {len(rows)}개")
        for row in rows[: args.limit]:
            mark = "✔" if is_guideline(row.title, b.get("dedicated", False)) else "·"
            print(f"   {mark} {row.date or '----------'}  {row.title[:70]}")
            print(f"       {row.url}  첨부 {len(row.attachments)}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="gcrawler")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("crawl")
    c.add_argument("--full", action="store_true", help="게시판 전체 페이지 수집")
    c.add_argument("--society", action="append", help="학회 id (여러 번 지정 가능)")
    c.add_argument("--max-pages", type=int)
    c.add_argument("--detail", type=int, default=None, help="학회당 상세 페이지 확인 최대 횟수")
    c.add_argument("--rediscover", action="store_true", help="게시판 자동 탐색 캐시 무시")
    c.add_argument("--delay", type=float, default=1.5, help="같은 사이트 요청 간격(초)")
    c.add_argument("--workers", type=int, default=6, help="동시에 수집할 학회 수")

    k = sub.add_parser("check")
    k.add_argument("society")
    k.add_argument("--limit", type=int, default=15)
    k.add_argument("--ignore-robots", action="store_true")

    pr = sub.add_parser("probe", help="진단용 HTML 저장")
    pr.add_argument("ids", nargs="*")

    b = sub.add_parser("build")
    b.add_argument("--base-url", default="./")

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")

    if args.cmd == "crawl":
        mode = "full" if args.full else "incremental"
        detail = args.detail if args.detail is not None else (120 if args.full else 30)
        ids = [i.strip() for v in (args.society or []) for i in v.split(",") if i.strip()] or None
        res = run(ids, mode=mode, max_pages=args.max_pages, detail_per_society=detail,
                  rediscover=args.rediscover, fetcher=Fetcher(delay=args.delay), workers=args.workers)
        print(f"완료: 신규 {res['new']}건 / 누적 {res['total']}건 / 요청 {res['requests']}회")
        return 0
    if args.cmd == "probe":
        from .probe import run as probe_run

        probe_run(args.ids or None)
        return 0
    if args.cmd == "check":
        return cmd_check(args)
    if args.cmd == "build":
        from .build import build

        res = build(args.base_url)
        print(f"사이트 빌드: 항목 {res['items']}건 (회원 전용 제외 {res['hidden_members_only']}건) → _site/")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
