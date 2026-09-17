"""CLI entry point for repeatable CANN API audits and Markdown reporting."""

import argparse
import html
import json
import subprocess
import sys
import urllib.parse
from collections import Counter, defaultdict
from pathlib import Path

from cann_docs import LATEST, SITE, build_catalog, catalog_digest, now, save_json
from scan_repo import candidate_tier, prepare_repository, scan_repository

DEFAULT_CACHE = Path.home() / ".cache" / "cann-api-audit"
ORIGINS = frozenset({"cann", "target", "local", "other", "uncertain"})


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def new_review(scan):
    return {
        "scan_fingerprint": scan["scan_fingerprint"],
        "scope_reviewed": False,
        "catalog_reviewed": False,
        "catalog_fingerprint": "",
        "notes": [],
        "symbols": {},
        "additional_occurrences": [],
    }


def validate_review(scan, review):
    if review.get("scan_fingerprint") != scan["scan_fingerprint"]:
        raise ValueError("Review fingerprint differs from scan; re-check the changed repository before reusing review")
    for switch in ("scope_reviewed", "catalog_reviewed"):
        if not isinstance(review.get(switch, False), bool):
            raise ValueError(switch + " must be a JSON boolean")
    if not isinstance(review.get("symbols", {}), dict):
        raise ValueError("review.symbols must be an object")
    for name, decision in review.get("symbols", {}).items():
        if decision.get("origin") not in ORIGINS or not decision.get("reason", "").strip():
            raise ValueError("Each reviewed symbol needs origin and a nonempty reason: " + name)
        if decision.get("evidence_url") and not decision.get("evidence_note"):
            raise ValueError("Manual evidence requires evidence_note: " + name)
        if decision.get("searched") and not decision.get("search_note"):
            raise ValueError("searched requires search_note: " + name)
    for occurrence in review.get("additional_occurrences", []):
        required = {"name", "path", "line", "source", "kind", "category", "signals"}
        if not required <= occurrence.keys():
            raise ValueError("Additional occurrence lacks required fields")
        root = Path(scan["repository"]["root"]).resolve()
        path = (root / occurrence["path"]).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Additional occurrence is outside repository")
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        line = occurrence["line"]
        if (
            not isinstance(line, int)
            or not 1 <= line <= len(lines)
            or occurrence["source"].strip() != lines[line - 1].strip()
        ):
            raise ValueError("Additional occurrence must quote an existing source line")


def all_grouped_occurrences(scan, review):
    groups = defaultdict(list)
    for occurrence in scan["occurrences"] + review.get("additional_occurrences", []):
        groups[occurrence["name"]].append(occurrence)
    return groups


def occurrence_tier(occurrence):
    return occurrence.get("candidate_tier") or candidate_tier(occurrence)


def grouped_occurrences(scan, review, catalog=None):
    """Return interface candidates supported by direct or namespace evidence."""
    groups = all_grouped_occurrences(scan, review)
    if catalog is None:
        return groups
    reviewed = set(review.get("symbols", {}))
    matches = catalog.get("matches", {})
    return {
        name: items
        for name, items in groups.items()
        if name in reviewed
        or any(occurrence_tier(item) == "direct" for item in items)
        or (
            name in matches
            and any(occurrence_tier(item) == "namespace-unresolved" for item in items)
        )
    }


def has_local_definition(name, scan):
    definitions = scan.get("local_definitions", {})
    leaf = name.rsplit("::", 1)[-1].rsplit(".", 1)[-1]
    return bool(definitions.get(name) or definitions.get(leaf))


def automatic_cann_ownership(name, items, scan):
    if scan.get("target"):
        return False  # Generic ownership requires review of imports, scopes and local definitions.
    if has_local_definition(name, scan):
        return False
    return any(occurrence_tier(item) in {"direct", "namespace-unresolved"} for item in items)


def manual_evidence(decision, catalog):
    url = decision.get("evidence_url", "")
    if not url:
        return []
    parsed = urllib.parse.urlsplit(url)
    if catalog.get("target"):
        if url not in {page["url"] for page in catalog.get("articles", [])}:
            raise ValueError("Manual evidence must belong to the reviewed documentation snapshot")
        return [{"url": url, "title": decision["evidence_note"], "manual": True}]
    version_prefix = SITE + "/document/detail/" + catalog.get("prefix", "UNRESOLVED") + "/"
    if parsed.scheme != "https" or not url.startswith(version_prefix):
        raise ValueError("Manual evidence must be an HTTPS official page for the resolved CANN version")
    return [{"url": url, "title": decision["evidence_note"], "manual": True}]


def classify(name, items, review, catalog, scan):
    target_name = scan.get("target", {}).get("name", "CANN")
    decision = review.get("symbols", {}).get(name, {})
    origin = decision.get("origin", "uncertain")
    canonical = decision.get("canonical", name)
    evidence = manual_evidence(decision, catalog) or catalog.get("matches", {}).get(canonical, [])
    if origin in {"local", "other"}:
        return "非 CANN / 本地定义", decision.get("reason", ""), evidence
    if origin not in {"cann", "target"}:
        if evidence and automatic_cann_ownership(name, items, scan):
            return "已匹配", "直接使用证据与最新官方文档名称同时命中", evidence
        detail = f"名称已在文档中匹配，但 {target_name} 归属尚待复核" if evidence else f"{target_name} 归属尚待复核"
        return "待核实", detail, evidence
    if evidence:
        return "已匹配", decision["reason"], evidence
    queried = canonical in catalog.get("queried_names", [])
    complete = (
        catalog.get("complete")
        and review.get("scope_reviewed")
        and review.get("catalog_reviewed")
        and not scan.get("errors")
        and queried
    )
    if complete and decision.get("searched") is True and decision.get("search_note"):
        return "官方文档未找到", decision["reason"] + "；二次核查：" + decision["search_note"], []
    return "待核实", "已确认直接依赖；文档覆盖、扫描范围或二次核查尚未完成", []


def cell(value):
    return html.escape(str(value), quote=False).replace("|", "&#124;").replace("`", "&#96;").replace("\n", "<br>")


def write_report(scan, catalog, review, output):
    validate_review(scan, review)
    if catalog.get("scan_fingerprint", scan["scan_fingerprint"]) != scan["scan_fingerprint"]:
        raise ValueError("Catalog belongs to a different scan; regenerate documentation evidence")
    if review.get("catalog_reviewed") and review.get("catalog_fingerprint") != catalog_digest(catalog):
        raise ValueError("Reviewed documentation snapshot differs from catalog; re-check before finalizing")
    groups = grouped_occurrences(scan, review, catalog)
    classified_rows = []
    for name, items in sorted(groups.items()):
        state, reason, evidence = classify(name, items, review, catalog, scan)
        classified_rows.append((name, items, state, reason, evidence))
    rows = [row for row in classified_rows if row[2] != "非 CANN / 本地定义"]
    counts = Counter(row[2] for row in rows)
    finalized = (
        catalog.get("complete")
        and review.get("scope_reviewed")
        and review.get("catalog_reviewed")
        and not scan.get("errors")
        and not counts["待核实"]
    )
    repository = scan["repository"]
    target_name = cell(scan.get("target", {}).get("name", "CANN"))
    entry = catalog.get("entry", LATEST)
    lines = [
        f"# {target_name} 公开接口核对报告",
        "",
        "**结论状态：" + ("已完成本次核对" if finalized else "阶段性结果，尚不能判断全部接口通过") + "**",
        "",
        "只按名称核对公开 API 文档，不检查签名，不等价于运行时兼容性保证。",
        "",
        "## 扫描基线",
        "",
        f"- 仓库：`{cell(repository.get('input', repository['root']))}`",
        f"- 本地目录：`{cell(repository['root'])}`",
        f"- Commit：`{cell(repository.get('commit', '非 Git 仓库 / 无法读取'))}`",
        f"- 扫描时间：{cell(repository['scanned_at'])}",
        f"- 报告时间：{now()}",
        f"- 内容指纹：`{scan['scan_fingerprint']}`",
        "- 工作区："
        + ("存在未提交/未跟踪文件，已扫描当前磁盘内容" if repository.get("worktree_status") else "干净或无 Git 信息"),
        f"- 读取文本文件：{repository['files_read']}；跳过：{len(scan['skipped'])}；读取失败：{len(scan['errors'])}",
        f"- 报告接口：{len(rows)} 个去重名称",
        f"- {target_name} 实际版本：`{cell(catalog.get('version', '未能解析 latest'))}`",
        f"- 官方入口：[{target_name} 官方文档]({entry})",
        f"- API 正文：已读取 {catalog.get('pages_read', 0)} / 目录选中 {catalog.get('pages_total', 0)} 页；"
        f"获取失败/目录缺口 {len(catalog.get('failures', []))} 项",
        f"- 文档自动覆盖：{'完整抓取所选 API 页面' if catalog.get('complete') else '不完整'}；"
        f"目录人工复核：{'完成' if review.get('catalog_reviewed') else '未完成'}；"
        f"扫描范围人工复核：{'完成' if review.get('scope_reviewed') else '未完成'}",
        "",
        "## 汇总",
        "",
        "| 状态 | 去重名称数 |",
        "| --- | ---: |",
    ]
    for state in ("官方文档未找到", "待核实", "已匹配"):
        lines.append(f"| {state} | {counts[state]} |")
    lines.extend(
        [
            "",
            "每个名称可有多个来源位置；名称数量不是调用次数。",
            "",
        ]
    )
    for state in ("官方文档未找到", "待核实", "已匹配"):
        lines.extend(["## " + state, ""])
        selected = [row for row in rows if row[2] == state]
        if not selected:
            lines.extend(["无。" if finalized else "本阶段无已确认条目。", ""])
        for name, items, _, reason, evidence in selected:
            decision = review.get("symbols", {}).get(name, {})
            lines.extend(
                [
                    f"### `{cell(name)}`",
                    "",
                    "- 类别：" + cell(decision.get("kind", items[0]["kind"])),
                    "- 判定：" + cell(reason),
                ]
            )
            if decision.get("canonical"):
                lines.append("- 规范名称：`" + cell(decision["canonical"]) + "`")
            if decision.get("reason") and decision["reason"] != reason:
                lines.append("- 归属依据：" + cell(decision["reason"]))
            for proof in evidence:
                lines.append(
                    f"- 文档证据{'（人工核实）' if proof.get('manual') else ''}："
                    f"[{cell(proof['title']).replace('[', '').replace(']', '')}]"
                    f"({urllib.parse.quote(proof['url'], safe=':/%?=&')})"
                )
            lines.extend(["", "| 来源 | 文件:行号 | 代码 |", "| --- | --- | --- |"])
            by_file = defaultdict(list)
            for item in items:
                by_file[item["path"]].append(item)
            for path, locations in sorted(by_file.items()):
                first = locations[0]
                positions = ", ".join(str(position) for position in sorted({item["line"] for item in locations}))
                lines.append(
                    f"| {cell(first['category'])} | `{cell(path)}:{positions}` | "
                    f"`{cell(first['source'])}` |"
                )
            lines.append("")
    lines.extend(["## 覆盖与限制", ""])
    if catalog.get("coverage_note"):
        lines.append("- 文档覆盖复核：" + cell(catalog["coverage_note"]))
    for limitation in scan["limitations"]:
        lines.append("- " + cell(limitation))
    for note in review.get("notes", []):
        lines.append("- 复核记录：" + cell(note))
    if not rows:
        lines.append(f"- 本次没有需要列入报告的直接 {target_name} 接口；在扫描范围复核完成前，不能据此认定无该依赖。")
    lines.extend(
        ["", "### 文档章节覆盖", "", "| 章节 | 页面总数 | 选中 API 页面 | 备注 |", "| --- | ---: | ---: | --- |"]
    )
    for chapter in catalog.get("chapters", []):
        lines.append(
            f"| {cell(' / '.join(chapter['titles']))} | {chapter['total']} | {chapter['selected']} | "
            f"{cell(chapter.get('error', chapter.get('external', '')))} |"
        )
    for title, entries, path_key, reason_key in (
        ("文档获取缺口", catalog.get("failures", []), "url", "error"),
        ("扫描需复核项", scan["gaps"] + scan["errors"], "path", "reason"),
        ("跳过的文件或目录", scan["skipped"], "path", "reason"),
    ):
        lines.extend(["", "### " + title, ""])
        if not entries:
            lines.append("无。")
        else:
            lines.extend(["<details>", "<summary>展开完整清单</summary>", "", "| 位置 | 原因 |", "| --- | --- |"])
            for entry in entries:
                lines.append(f"| {cell(entry[path_key])} | {cell(entry[reason_key])} |")
            lines.extend(["", "</details>"])
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dict(counts)


def positive(value):
    value = int(value)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("scan", "analyze"):
        command = commands.add_parser(name)
        command.add_argument("repository", help="Local directory, HTTPS Git URL, or SSH Git URL")
        command.add_argument("--output", required=True, type=Path)
        command.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
        if name == "analyze":
            command.add_argument(
                "--max-pages", type=positive, help="Smoke test only; always report incomplete coverage"
            )
            command.add_argument("--refresh", action="store_true")
            command.add_argument("--extra-pages", type=Path)
    report = commands.add_parser("report")
    report.add_argument("scan", type=Path)
    report.add_argument("--catalog", type=Path, required=True)
    report.add_argument("--review", type=Path, required=True)
    report.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "report":
            counts = write_report(read_json(args.scan), read_json(args.catalog), read_json(args.review), args.output)
        else:
            output = args.output.resolve()
            stem = output.with_suffix("")
            scan_path = Path(str(stem) + ".scan.json")
            catalog_path = Path(str(stem) + ".catalog.json")
            review_path = Path(str(stem) + ".review.json")
            root, metadata = prepare_repository(args.repository, args.cache_dir / "repositories")
            scan = scan_repository(
                root, metadata, excluded=[output, scan_path, catalog_path, review_path, args.cache_dir]
            )
            if args.command == "scan":
                save_json(output, scan)
                print(
                    f"Scanned {scan['repository']['files_read']} files; "
                    f"{len({item['name'] for item in scan['occurrences']})} interface candidate names collected; "
                    f"saved {output}"
                )
                return 0
            save_json(scan_path, scan)
            if review_path.exists():
                review = read_json(review_path)
                validate_review(scan, review)
            else:
                review = new_review(scan)
                save_json(review_path, review)
            names = sorted(
                {
                    review.get("symbols", {}).get(name, {}).get("canonical", name)
                    for name in grouped_occurrences(scan, review)
                }
            )
            print(
                f"Scanned {scan['repository']['files_read']} files; "
                f"{len(names)} interface candidate names queued for documentation evidence",
                flush=True,
            )
            catalog = build_catalog(
                names,
                args.cache_dir / "docs",
                refresh=args.refresh,
                max_pages=args.max_pages,
                extra_pages=read_json(args.extra_pages) if args.extra_pages else None,
            )
            catalog["queried_names"] = names
            catalog["scan_fingerprint"] = scan["scan_fingerprint"]
            catalog["catalog_fingerprint"] = catalog_digest(catalog)
            save_json(catalog_path, catalog)
            counts = write_report(scan, catalog, review, output)
        print(json.dumps({"report": str(args.output.resolve()), "counts": counts}, ensure_ascii=False))
        return 0
    except (ValueError, KeyError, TypeError, OSError, subprocess.TimeoutExpired) as error:
        print("Audit failed: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
