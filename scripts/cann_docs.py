"""Version-pinned official CANN catalog and article cache; standard library only."""

import concurrent.futures
import hashlib
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

SITE = "https://www.hiascend.com"
LATEST = SITE + "/document/detail/zh/CANNCommunityEdition/latest/API/headerliblist/hfandlf_09_0001.html"
SERVICE = SITE + "/ascendgateway/ascendservice"
CACHE_TTL = 86400
REQUEST_TIMEOUT = 25
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
WORKERS = 6
API_SECTION = re.compile(
    r"API|接口参考|接口说明|数据结构|枚举|宏定义|常量定义|语言接口|开发接口|基础.*接口|接口$", re.I
)
API_PATH = re.compile(r"/(?:API|api|api_ref|api_reference|apiref|.*_api)/", re.I)


def now():
    return datetime.now(timezone.utc).isoformat()


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def catalog_digest(catalog):
    stable = {
        key: catalog.get(key)
        for key in ("version", "prefix", "complete", "pages_total", "pages_read", "failures", "name_fingerprint")
    }
    stable["articles"] = sorted((page["url"], page["content_sha256"]) for page in catalog.get("articles", []))
    return hashlib.sha256(json.dumps(stable, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class ArticleParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = 0
        self.links = []
        self.canonical = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "link" and values.get("rel") == "canonical":
            self.canonical = values.get("href")
        if tag in {"script", "style", "nav", "head"}:
            self.skip += 1
        if tag == "a" and values.get("href"):
            self.links.append(values["href"])
        if tag in {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "pre"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "nav", "head"}:
            self.skip = max(0, self.skip - 1)
        if tag in {"p", "div", "li", "td", "th", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def article_text(raw):
    """Do not search page scripts, metadata, link destinations or navigation."""
    if "__NUXT_DATA__" in raw or 'id="__nuxt"' in raw:
        match = re.search(r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', raw, re.S)
        if not match:
            raise ValueError("SPA shell without readable article payload")
        flattened = json.loads(match.group(1))
        bodies = [
            item
            for item in flattened
            if isinstance(item, str)
            and (
                re.search(r'<h1\b|class="topictitle1"', item) or re.search(r"^(?:<!--Metadata[\s\S]*?-->\s*)?# ", item)
            )
        ]
        if not bodies:
            raise ValueError("SPA article payload is missing")
        raw = max(bodies, key=len)
    raw = re.sub(r"<!--[\s\S]*?-->", "", raw)
    if re.search(r"<(?:html|body|h1|div|p)\b", raw, re.I):
        parser = ArticleParser()
        parser.feed(raw)
        result = "".join(parser.parts)
    else:
        result = re.sub(r"!?\[([^\]]*)\]\([^\n]*?\)", r"\1", raw)
        result = re.sub(r"(?m)^\s*\[[^\]]+\]:\s*https?://.*$", "", result)
        result = re.sub(r"https?://[^\s<>]+", "", result)
        # Preserve code spans; their content can be types, constants and macros.
        result = html.unescape(result)
    if len(result.strip()) < 20 or re.search(r"^\s*(?:404|403|Page not found)", result, re.I):
        raise ValueError("Empty or error article")
    return result


def fetch(url, cache, refresh=False):
    """Cache only successful responses, retry transport errors once."""
    cache = Path(cache)
    filename = cache / (hashlib.sha256(url.encode()).hexdigest() + ".json")
    if filename.exists() and not refresh:
        try:
            record = json.loads(filename.read_text(encoding="utf-8"))
            if time.time() - record["epoch"] < CACHE_TTL and record["url"] == url:
                return record
        except (ValueError, KeyError, OSError):
            pass
    encoded = urllib.parse.quote(url, safe=":/?=&%#+")
    request = urllib.request.Request(
        encoded,
        headers={
            "User-Agent": "CANN-API-Audit/1.0 (public documentation reader)",
            "Referer": SITE + "/",
        },
    )
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                if urllib.parse.urlsplit(response.url).hostname != "www.hiascend.com":
                    raise ValueError("Documentation redirected outside www.hiascend.com")
                data = response.read(MAX_RESPONSE_BYTES + 1)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise ValueError("Documentation response exceeds size limit")
                text = data.decode("utf-8-sig")
                # A business-level error with HTTP 200 is not successful data.
                if text.lstrip().startswith("{"):
                    payload = json.loads(text)
                    if payload.get("success") is False:
                        raise ValueError("Official catalog error: " + str(payload.get("msg")))
                record = {
                    "url": url,
                    "resolved_url": response.url,
                    "fetched_at": now(),
                    "epoch": time.time(),
                    "text": text,
                }
                save_json(filename, record)
                return record
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt:
                raise
            time.sleep(0.3)
    raise RuntimeError("Request failed")


def resolve_latest(cache, refresh=False):
    # The moving latest alias must be resolved anew even while versioned articles are cached.
    record = fetch(LATEST, cache, refresh=True)
    parser = ArticleParser()
    parser.feed(record["text"])
    url = parser.canonical or record["resolved_url"]
    match = re.fullmatch(r"https://www\.hiascend\.com/document/detail/(zh/CANNCommunityEdition/([^/]+))/(.+)", url)
    if not match or match[2] == "latest":
        raise ValueError("Cannot resolve latest to an explicit CANN version")
    return {
        "version": match[2],
        "prefix": match[1],
        "entry": url,
        "latest_entry": LATEST,
        "resolved_at": record["fetched_at"],
    }


def walk_nodes(nodes, ancestors=()):
    for node in nodes:
        yield node, ancestors
        yield from walk_nodes(node.get("children", []), ancestors + (node.get("nodeName", ""),))


def selected_api(node, ancestors, chapter_names):
    names = chapter_names + ancestors + (node.get("nodeName", ""),)
    return any(API_SECTION.search(name) for name in names) or bool(API_PATH.search("/" + node.get("nodeUrl", "")))


def discover(info, cache, refresh=False):
    route = info["entry"].split("/document/detail/", 1)[1]
    route = re.sub(r"\.html$", "_90x_html", route)
    url = SERVICE + "/doc/version/new/tree?" + urllib.parse.urlencode({"route": route})
    tree = json.loads(fetch(url, cache, refresh)["text"])
    if not tree.get("success") or not isinstance(tree.get("data"), list) or not tree["data"]:
        raise ValueError("Invalid or empty official version directory")
    chapters = [
        (node, names) for node, names in walk_nodes(tree["data"]) if node.get("codePath") and not node.get("children")
    ]
    pages, summary, errors = {}, [], []
    for number, (chapter, parents) in enumerate(chapters, 1):
        code = chapter["codePath"]
        if not code.startswith(info["prefix"] + "/"):
            errors.append({"url": code, "error": "Chapter outside pinned version"})
            continue
        chapter_names = parents + (chapter.get("nodeName", code),)
        entry = {
            "chapter": code,
            "titles": list(chapter_names),
            "total": 0,
            "selected": 0,
            "catalog_url": SERVICE + "/doc/node/tree/" + code,
        }
        try:
            if chapter.get("remoteUrl"):
                # External navigation such as installation download is not an API chapter.
                entry["external"] = chapter["remoteUrl"]
                if any(API_SECTION.search(name) for name in chapter_names):
                    errors.append({"url": chapter["remoteUrl"], "error": "External API chapter needs review"})
                summary.append(entry)
                continue
            payload = json.loads(fetch(entry["catalog_url"], cache, refresh)["text"])
            nodes = payload.get("data", {}).get("directory", [])
            if not payload.get("success") or not nodes:
                raise ValueError("Empty chapter directory")
            for node, ancestors in walk_nodes(nodes):
                chosen = selected_api(node, ancestors, chapter_names)
                if chosen and node.get("childrenNum", 0) > len(node.get("children", [])):
                    errors.append({"url": node.get("nodeUrl", code), "error": "Unexpanded API directory"})
                path = node.get("nodeUrl")
                if not path:
                    if chosen and node.get("remoteUrl"):
                        errors.append({"url": node["remoteUrl"], "error": "External API page needs review"})
                    continue
                entry["total"] += 1
                if not chosen:
                    continue
                if not path.startswith(info["prefix"] + "/"):
                    errors.append({"url": path, "error": "API page outside pinned version"})
                    continue
                entry["selected"] += 1
                page_url = SITE + "/document/detail/" + path
                pages[page_url] = {
                    "url": page_url,
                    "source_url": SITE + "/doc_center/source/" + path,
                    "title": node.get("nodeName", ""),
                    "chapter": code,
                    "ancestors": list(chapter_names + ancestors),
                }
        except (ValueError, OSError, urllib.error.URLError) as error:
            errors.append({"url": entry["catalog_url"], "error": str(error)})
            entry["error"] = str(error)
        summary.append(entry)
        if number % 5 == 0:
            print(f"Official directories: {number}/{len(chapters)}; API pages: {len(pages)}", flush=True)
    if not pages:
        errors.append({"url": url, "error": "No API pages selected"})
    return list(pages.values()), summary, errors


def exact_name(text, name):
    # Punctuation separates symbols; ASCII identifier characters must not extend them.
    return bool(re.search(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])", text))


def name_matches(text, name, page):
    if exact_name(text, name):
        return True
    # Class/module qualification may be established by the article title and directory.
    if "::" in name:
        parts = name.split("::")
        leaf = parts[-1]
        context = " ".join(page.get("ancestors", [])) + " " + page.get("title", "")
        if len(parts) >= 3:
            return exact_name(text, leaf) and exact_name(context, parts[-2])
        family = parts[0]
        expected = {"AscendC": "Ascend C", "ge": "GE", "gert": "GE", "Hccl": "HCCL"}.get(family)
        return bool(expected and expected in context and exact_name(text, leaf))
    if name.startswith("acl."):
        # Do not match a Python leaf such as malloc against the C chapter.
        context = " ".join(page.get("ancestors", [])) + " " + text[:2000]
        shortened = name.removeprefix("acl.")
        return "Python" in context and exact_name(text, shortened)
    return False


def fetch_article(page, cache, refresh):
    errors = []
    for url in (page["source_url"], page["url"]):
        try:
            record = fetch(url, cache, refresh)
            text = article_text(record["text"])
            return text, record["fetched_at"], None
        except (ValueError, OSError, urllib.error.URLError) as error:
            errors.append(str(error))
    return "", None, "; ".join(errors)


def build_catalog(names, cache, refresh=False, max_pages=None, extra_pages=None):
    """Fetch every selected API article, or explicitly mark bounded smoke runs partial."""
    cache = Path(cache)
    catalog = {
        "generated_at": now(),
        "complete": False,
        "matches": {},
        "failures": [],
        "chapters": [],
        "pages_total": 0,
        "pages_read": 0,
        "pages_attempted": 0,
        "name_fingerprint": hashlib.sha256("\n".join(sorted(names)).encode()).hexdigest(),
    }
    try:
        info = resolve_latest(cache / "latest", refresh)
        catalog.update(info)
        version_cache = cache / info["version"]
        pages, summary, failures = discover(info, version_cache, refresh)
        catalog["chapters"] = summary
        catalog["failures"].extend(failures)
        if extra_pages:
            for item in extra_pages:
                url = item["url"]
                prefix = SITE + "/document/detail/" + info["prefix"] + "/"
                if not url.startswith(prefix) or not item.get("reason"):
                    raise ValueError("Extra page must have reason and belong to resolved latest version")
                pages.append(
                    {
                        "url": url,
                        "source_url": url.replace("/document/detail/", "/doc_center/source/"),
                        "title": item.get("title", ""),
                        "ancestors": ["人工补充 API 页面"],
                        "chapter": "manual",
                        "reason": item["reason"],
                    }
                )
        pages = list({page["url"]: page for page in pages}.values())
        # Fetch likely matches first, but never equate that with complete coverage.
        names_by_leaf = defaultdict(list)
        for name in names:
            names_by_leaf[re.split(r"::|\.", name)[-1]].append(name)
        leaves = set(names_by_leaf)
        pages.sort(key=lambda p: (not leaves.intersection(re.findall(r"\b[A-Za-z_]\w*\b", p["title"])), p["url"]))
        catalog["pages_total"] = len(pages)
        selected = pages if max_pages is None else pages[:max_pages]
        catalog["limited"] = len(selected) < len(pages)
        catalog["articles"] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as executor:
            futures = {executor.submit(fetch_article, page, version_cache, refresh): page for page in selected}
            for future in concurrent.futures.as_completed(futures):
                page = futures[future]
                text, fetched_at, error = future.result()
                catalog["pages_attempted"] += 1
                if error:
                    catalog["failures"].append({"url": page["url"], "error": error})
                else:
                    catalog["pages_read"] += 1
                    article = {
                        **page,
                        "fetched_at": fetched_at,
                        "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    }
                    catalog["articles"].append(article)
                    for leaf in leaves.intersection(re.findall(r"\b[A-Za-z_]\w*\b", text)):
                        for name in names_by_leaf[leaf]:
                            if name_matches(text, name, page):
                                evidence = catalog["matches"].setdefault(name, [])
                                if len(evidence) < 3:
                                    evidence.append(
                                        {"url": page["url"], "title": page["title"], "fetched_at": fetched_at}
                                    )
                if catalog["pages_attempted"] % 20 == 0:
                    print(
                        f"Official articles: {catalog['pages_attempted']}/{len(selected)}; "
                        f"names matched: {len(catalog['matches'])}; failures: {len(catalog['failures'])}",
                        flush=True,
                    )
        catalog["complete"] = bool(pages) and not catalog["limited"] and not catalog["failures"]
    except (ValueError, KeyError, TypeError, OSError, urllib.error.URLError) as error:
        catalog["failures"].append({"url": LATEST, "error": str(error)})
    return catalog
