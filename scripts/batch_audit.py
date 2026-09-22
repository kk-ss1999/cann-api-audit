"""Audit one repository snapshot against independently prepared official documentation targets."""

import argparse
import re
from pathlib import Path

import audit
import dependency_audit as generic
from cann_docs import catalog_digest, now, save_json
from scan_repo import enumerate_sources, prepare_repository


def render(index, output):
    """Regenerate each target from its own evidence/review, then assemble one Markdown report."""
    lines = [
        "# 代码仓与多个官方接口文档核对",
        "",
        "- 代码仓：" + audit.cell(index["repository"]),
        "- 源码快照：`" + index["source_fingerprint"] + "`",
        "- 报告时间：" + now(),
        "",
    ]
    summary, details = [], []
    complete = True
    for target in index["targets"]:
        name = audit.cell(target.get("name", target["docs_entry"]))
        version = audit.cell(target.get("version", "未确定"))
        error = target.get("error")
        counts = {}
        body = ""
        finalized = False
        if not error:
            try:
                paths = target["paths"]
                scanned = audit.read_json(paths["scan"])
                catalog = audit.read_json(paths["catalog"])
                review = audit.read_json(paths["review"])
                if scanned.get("source_fingerprint") != index["source_fingerprint"]:
                    raise ValueError("Target belongs to a different repository snapshot")
                if (
                    scanned["target"]["docs_entry"] != target["docs_entry"]
                    or catalog.get("target") != scanned["target"]
                ):
                    raise ValueError("Target evidence does not belong to this documentation entry")
                counts = audit.write_report(scanned, catalog, review, paths["report"])
                finalized = bool(
                    catalog.get("complete")
                    and review.get("scope_reviewed")
                    and review.get("catalog_reviewed")
                    and not scanned.get("errors")
                    and not counts.get("待核实")
                )
                body = Path(paths["report"]).read_text(encoding="utf-8")
                # Nest the existing report without modifying interface names or evidence URLs.
                body = "\n".join(
                    "#" + line if re.match(r"^#{1,5} ", line) else line for line in body.splitlines()[1:]
                )
            except (ValueError, KeyError, TypeError, OSError) as exc:
                error = str(exc)
        complete = complete and finalized
        status = "获取/复核失败" if error else "已完成" if finalized else "待复核"
        summary.append(
            f"| {name} | {version} | {counts.get('已匹配', 0)} | {counts.get('官方文档未找到', 0)} | {counts.get('待核实', 0)} | {status} |"
        )
        details.append(f"## {name}（{version}）\n\n入口：{audit.cell(target['docs_entry'])}\n")
        details.append("缺口：" + audit.cell(error) if error else body)
    lines += [
        "**结论状态：" + ("已完成全部依赖库核对" if complete else "阶段性结果，部分依赖库尚未完成") + "**",
        "",
        "各依赖库的名称、版本和证据独立核对；某库的匹配不能替代另一库的证据。",
        "",
        "| 依赖库 | 版本 | 已匹配 | 官方文档未找到 | 待核实 | 状态 |",
        "| --- | --- | ---: | ---: | ---: | --- |",
        *summary,
        "",
        *details,
    ]
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    Path(output).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return complete


def run(repository, specification, output):
    specification, output = Path(specification).resolve(), Path(output).resolve()
    spec = audit.read_json(specification)
    targets = spec.get("targets", [])
    if not targets:
        raise ValueError("At least one official documentation entry is required")
    entries = [target["docs_entry"] for target in targets]
    if len(entries) != len(set(entries)):
        raise ValueError("Duplicate documentation entries; merge duplicate inputs first")
    work = output.with_suffix(".targets")
    root, metadata = prepare_repository(repository, audit.DEFAULT_CACHE / "repositories")
    root = Path(root).resolve()
    if output.is_relative_to(root) or specification.is_relative_to(root):
        raise ValueError("Store batch inputs and output outside the scanned repository")
    snapshot = enumerate_sources(root, set())
    source_fingerprint = generic.fingerprint(snapshot)
    index = {"repository": repository, "source_fingerprint": source_fingerprint, "targets": []}
    for item in targets:
        entry = item["docs_entry"]
        # Stable per-entry directories survive input reordering and never use product display names as keys.
        folder = work / generic.fingerprint(entry)
        target = {"docs_entry": entry}
        index["targets"].append(target)
        try:
            if item.get("error"):
                raise ValueError(item["error"])
            profile_path = specification.parent / item["profile"]
            manifest_path = specification.parent / item["docs_manifest"]
            profile = audit.read_json(profile_path)
            target.update(name=profile["name"], version=profile["version"])
            generic.validate_profile(profile, entry)
            paths = {key: str(folder / ("report." + key + ".json")) for key in ("scan", "catalog", "review")}
            paths["report"] = str(folder / "report.md")
            target["paths"] = paths
            scanned = generic.scan(root, metadata, profile, source_snapshot=snapshot)
            scanned["source_fingerprint"] = source_fingerprint
            review = (
                audit.read_json(paths["review"])
                if Path(paths["review"]).exists()
                else audit.new_review(scanned)
            )
            audit.validate_review(scanned, review)
            names = {
                review.get("symbols", {}).get(n, {}).get("canonical", n)
                for n in audit.all_grouped_occurrences(scanned, review)
            }
            catalog = generic.catalog(names, profile, audit.read_json(manifest_path), manifest_path.parent)
            catalog["scan_fingerprint"] = scanned["scan_fingerprint"]
            catalog["catalog_fingerprint"] = catalog_digest(catalog)
            for key, value in (("scan", scanned), ("catalog", catalog), ("review", review)):
                save_json(paths[key], value)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            target["error"] = str(exc)
    save_json(output.with_suffix(".batch.json"), index)
    render(index, output)
    return index


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    analyze = commands.add_parser("analyze")
    analyze.add_argument("repository")
    analyze.add_argument(
        "--targets", required=True, type=Path, help="Internal per-entry profiles and snapshots"
    )
    analyze.add_argument("--output", required=True, type=Path)
    report = commands.add_parser("report")
    report.add_argument("index", type=Path)
    report.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == "analyze":
        run(args.repository, args.targets, args.output)
    else:
        render(audit.read_json(args.index), args.output)


if __name__ == "__main__":
    main()
