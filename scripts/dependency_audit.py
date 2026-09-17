"""Generic audit backend. The skill derives profiles and document snapshots from the supplied entry."""

import argparse
import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import audit
from cann_docs import article_text, catalog_digest, exact_name, now, save_json
from scan_repo import (
    CPP_SUFFIXES,
    attribute_name,
    blank,
    category,
    enumerate_sources,
    git,
    markdown_code,
    prepare_repository,
    python_snippets,
    strip_comments,
)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def official_url(url, profile):
    parsed = urlsplit(url)
    return parsed.scheme == "https" and parsed.hostname in profile["official_hosts"]


def validate_profile(profile, entry):
    if profile.get("docs_entry") != entry or not profile.get("name") or not profile.get("version"):
        raise ValueError("Profile must identify the supplied entry, dependency and documentation version")
    if not official_url(entry, profile) or not profile.get("api_roots"):
        raise ValueError("Profile requires official HTTPS hosts and API directory roots")
    for root in profile["api_roots"]:
        if not official_url(root, profile):
            raise ValueError("API root is outside verified official hosts")
    rules = profile.get("rules", [])
    if not rules:
        raise ValueError("No documented recognition rules; inspect the entry before scanning")
    for rule in rules:
        if rule.get("kind") not in {"python_module", "namespace", "symbol_prefix", "symbol"}:
            raise ValueError("Unsupported recognition rule")
        if (
            not rule.get("value")
            or not rule.get("reason")
            or not official_url(rule.get("evidence_url", ""), profile)
        ):
            raise ValueError("Each recognition rule needs a value and official evidence")


def scan(root, metadata, profile, excluded=()):
    root = Path(root).resolve()
    sources, skipped, errors = enumerate_sources(root, {Path(p).resolve() for p in excluded})
    occurrences, gaps, definitions = [], [], {}
    rules = profile["rules"]
    modules = [r["value"] for r in rules if r["kind"] == "python_module"]
    native = [r for r in rules if r["kind"] != "python_module"]

    def owned(name):
        return any(name == m or name.startswith(m + ".") for m in modules)

    def add(name, path, line, lines):
        occurrences.append(
            {
                "name": name,
                "path": path,
                "line": line,
                "source": lines[line - 1].strip(),
                "kind": "接口/类型/属性（待细分）",
                "category": category(path),
                "signals": [],
                "candidate_tier": "direct",
            }
        )

    for path, raw in sources.items():
        suffix = Path(path).suffix.lower()
        lines = raw.splitlines()
        for snippet in python_snippets(raw, suffix):
            try:
                tree = ast.parse(snippet)
            except SyntaxError as error:
                gaps.append({"path": path, "reason": "Python parsing failed: " + str(error)})
                continue
            aliases = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for item in node.names:
                        if owned(item.name):
                            aliases[item.asname or item.name.split(".")[0]] = (
                                item.name if item.asname else item.name.split(".")[0]
                            )
                elif (
                    isinstance(node, ast.ImportFrom) and not node.level and node.module and owned(node.module)
                ):
                    for item in node.names:
                        if item.name == "*":
                            gaps.append({"path": path, "reason": "Target star import requires scope review"})
                        else:
                            aliases[item.asname or item.name] = node.module + "." + item.name
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    definitions.setdefault(node.name, []).append(f"{path}:{node.lineno}")
            bases = {id(node.value) for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
            for node in ast.walk(tree):
                name = attribute_name(node)
                first = name.split(".")[0]
                if (
                    first in aliases
                    and id(node) not in bases
                    and isinstance(getattr(node, "ctx", None), ast.Load)
                ):
                    canonical = aliases[first] + name[len(first) :]
                    if owned(canonical) and canonical not in modules:
                        add(canonical, path, node.lineno, lines)
            if aliases:
                gaps.append(
                    {
                        "path": path,
                        "reason": "Review alias scopes/shadowing, local modules, dynamic attributes and instance methods",
                    }
                )
        if suffix in CPP_SUFFIXES or suffix in {".md", ".mdx"}:
            text = strip_comments(markdown_code(raw) if suffix in {".md", ".mdx"} else raw, suffix)
            # Literal contents only count after explicit dynamic-binding review.
            text = re.sub(r'"(?:\\.|[^"\\])*"', lambda m: blank(m[0]), text)
            for rule in native:
                value = re.escape(rule["value"])
                pattern = value + (
                    r"(?:::[A-Za-z_]\w*)+"
                    if rule["kind"] == "namespace"
                    else r"\w*"
                    if rule["kind"] == "symbol_prefix"
                    else ""
                )
                for match in re.finditer(r"(?<![\w:])" + pattern + r"(?!\w)", text):
                    add(match[0], path, text.count("\n", 0, match.start()) + 1, lines)
            if native:
                gaps.append(
                    {
                        "path": path,
                        "reason": "Review local definitions, namespace aliases, receivers, macro expansion and dynamic bindings",
                    }
                )
    unique = {(o["name"], o["path"], o["line"]): o for o in occurrences}
    metadata = {
        **metadata,
        "root": str(root),
        "name": root.name,
        "scanned_at": now(),
        "files_read": len(sources),
    }
    try:
        metadata.update(commit=git(root, "rev-parse", "HEAD"), worktree_status=git(root, "status", "--short"))
        for line in git(root, "submodule", "status").splitlines():
            if line.startswith("-"):
                gaps.append({"path": line, "reason": "Submodule not checked out"})
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        metadata["git_note"] = str(error)
    return {
        "schema": 2,
        "target": profile,
        "scan_fingerprint": fingerprint({"sources": sources, "profile": profile}),
        "repository": metadata,
        "occurrences": sorted(unique.values(), key=lambda o: (o["name"], o["path"], o["line"])),
        "local_definitions": definitions,
        "errors": errors,
        "skipped": skipped,
        "gaps": gaps,
        "limitations": [
            "Only direct dependency use; names only, not signatures.",
            "Static candidates require ownership/scope review; unsupported syntax must be inspected manually.",
        ],
    }


def catalog(names, profile, manifest, base):
    if manifest.get("docs_entry") != profile["docs_entry"] or manifest.get("version") != profile["version"]:
        raise ValueError("Document snapshot belongs to a different entry/version")
    result = {
        "target": profile,
        "profile_fingerprint": fingerprint(profile),
        "entry": profile["docs_entry"],
        "version": profile["version"],
        "complete": False,
        "matches": {},
        "failures": list(manifest.get("failures", [])),
        "articles": [],
        "chapters": [
            {
                "titles": [root],
                "chapter": root,
                "total": sum(1 for p in manifest.get("pages", []) if p["url"].startswith(root)),
                "selected": sum(1 for p in manifest.get("pages", []) if p["url"].startswith(root)),
            }
            for root in profile["api_roots"]
        ],
        "coverage_note": manifest.get("coverage_note", ""),
        "queried_names": sorted(names),
        "pages_read": 0,
        "pages_total": len(manifest.get("pages", [])),
        "generated_at": now(),
    }
    for page in manifest.get("pages", []):
        url = page["url"]
        if not official_url(url, profile) or not any(url.startswith(root) for root in profile["api_roots"]):
            raise ValueError("Page outside verified API roots: " + url)
        if page.get("version") != profile["version"]:
            raise ValueError("Page version differs from selected documentation")
        try:
            body = article_text((base / page["body_file"]).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            result["failures"].append({"url": url, "error": str(error)})
            continue
        proof = {"url": url, "title": page["title"], "fetched_at": page["fetched_at"]}
        result["articles"].append({**proof, "content_sha256": hashlib.sha256(body.encode()).hexdigest()})
        result["pages_read"] += 1
        for name in names:
            if exact_name(body, name):
                result["matches"].setdefault(name, []).append(proof)
    result["complete"] = (
        bool(result["pages_read"])
        and not result["failures"]
        and manifest.get("coverage_reviewed") is True
        and bool(manifest.get("coverage_note"))
    )
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository")
    parser.add_argument("--docs-entry", required=True)
    parser.add_argument("--profile", required=True, type=Path, help="Internal profile prepared by the skill")
    parser.add_argument("--docs-manifest", required=True, type=Path, help="Internal official body snapshot")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    profile = audit.read_json(args.profile)
    validate_profile(profile, args.docs_entry)
    stem = args.output.resolve().with_suffix("")
    paths = {k: Path(str(stem) + "." + k + ".json") for k in ("scan", "catalog", "review")}
    root, metadata = prepare_repository(args.repository, audit.DEFAULT_CACHE / "repositories")
    scanned = scan(root, metadata, profile, [args.output, args.profile, args.docs_manifest, *paths.values()])
    review = audit.read_json(paths["review"]) if paths["review"].exists() else audit.new_review(scanned)
    audit.validate_review(scanned, review)
    names = {
        review.get("symbols", {}).get(n, {}).get("canonical", n)
        for n in audit.all_grouped_occurrences(scanned, review)
    }
    docs = catalog(names, profile, audit.read_json(args.docs_manifest), args.docs_manifest.resolve().parent)
    docs["scan_fingerprint"] = scanned["scan_fingerprint"]
    docs["catalog_fingerprint"] = catalog_digest(docs)
    for key, value in (("scan", scanned), ("catalog", docs), ("review", review)):
        save_json(paths[key], value)
    audit.write_report(scanned, docs, review, args.output)


if __name__ == "__main__":
    main()
