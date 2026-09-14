"""Conservative static candidates with source locations, not a compiler/ownership oracle."""

import ast
import hashlib
import io
import os
import re
import subprocess
import tempfile
import tokenize
import urllib.parse
from bisect import bisect_right
from collections import Counter, defaultdict
from pathlib import Path

from cann_docs import now

SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "build",
        "dist",
        ".tox",
    }
)
BINARY_SUFFIXES = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".pdf",
        ".zip",
        ".gz",
        ".whl",
        ".so",
        ".dll",
        ".exe",
        ".a",
        ".lib",
        ".o",
        ".obj",
        ".pyc",
        ".bin",
        ".safetensors",
        ".pt",
        ".onnx",
        ".mp4",
        ".ttf",
        ".woff",
        ".woff2",
    }
)
CPP_SUFFIXES = frozenset({".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hxx", ".hh", ".cu", ".cuh", ".inc"})
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_GIT_SECONDS = 180
LOOKAHEAD_CHARS = 1024
MAX_INLINE_DEFINITIONS = 3
CANN_PYTHON_ROOTS = frozenset({"acl", "tbe", "te", "tik", "ge", "dataflow", "llm_datadist", "hccl", "hixl", "pypto"})
CANN_LANGUAGE_SYMBOLS = frozenset(
    {
        "__aicore__",
        "__global__",
        "__gm__",
        "__ubuf__",
        "__cbuf__",
        "__ca__",
        "__cb__",
        "__cc__",
        "__simt_vf__",
        "half",
        "bfloat16_t",
        "float16_t",
        "float8_e4m3fn_t",
        "float8_e5m2_t",
        "hifloat8_t",
        "int4b_t",
    }
)
TOKEN = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
CANN_PREFIX = re.compile(
    r"^(?:acl(?:nn|rt|mdl|op|dvpp|tdt|prof)?[A-Z][A-Za-z0-9_]*|ACL[A-Z0-9_]*|"
    r"Hccl[A-Za-z0-9_]+|hccl[A-Z][A-Za-z0-9_]*|HCCL_[A-Z0-9_]+|"
    r"Hcomm[A-Za-z0-9_]+|HCOMM_[A-Z0-9_]+|rt[A-Z][A-Za-z0-9_]*|RT_[A-Z0-9_]+|"
    r"asc_[a-z0-9_]+|ASC_[A-Z0-9_]+|ge[A-Z][A-Za-z0-9_]*|GE_[A-Z0-9_]+)$"
)
NAMESPACES = frozenset(
    {
        "AscendC",
        "ge",
        "gert",
        "optiling",
        "ops",
        "op",
        "fe",
        "platform_ascendc",
        "matmul",
        "tiling",
        "hixl",
        "Hccl",
        "atb",
        "pto",
    }
)
QUALIFIED = re.compile(r"\b(?:" + "|".join(sorted(NAMESPACES)) + r")(?:::[A-Za-z_]\w*)+")
CANN_HEADER = re.compile(
    r"^(?:(?:acl|aclnn|aclnnop|hccl|hcomm|hixl|runtime|graph|exe_graph|register|"
    r"experiment|external|tiling|atb|ascendc|aicpu)/|"
    r"(?:kernel_operator|kernel_tiling|kernel_tensor|kernel_utils|hccl|acl|"
    r"aclnn_base|tiling_api|lib_api|ascendc|securec)\.(?:h|hpp)$)"
)
CPP_LEXEMES = re.compile(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
CPP_IGNORE = frozenset(
    {
        "NULL",
        "TRUE",
        "FALSE",
        "DTYPE",
        "if",
        "for",
        "while",
        "switch",
        "return",
        "sizeof",
        "alignof",
        "decltype",
        "static_cast",
        "reinterpret_cast",
        "const_cast",
        "dynamic_cast",
        "defined",
        "assert",
        "void",
        "int",
        "float",
        "double",
        "bool",
        "char",
        "long",
        "short",
        "auto",
        "const",
        "constexpr",
        "consteval",
        "constinit",
        "inline",
        "static",
        "extern",
        "volatile",
        "restrict",
        "register",
        "unsigned",
        "signed",
        "typename",
        "template",
        "class",
        "struct",
        "enum",
        "namespace",
        "using",
        "public",
        "private",
        "protected",
        "virtual",
        "override",
        "final",
        "noexcept",
        "new",
        "delete",
        "operator",
        "throw",
        "try",
        "catch",
        "else",
        "do",
        "case",
        "break",
        "continue",
        "default",
        "friend",
        "mutable",
        "union",
        "typedef",
        "true",
        "false",
        "nullptr",
        "this",
        "requires",
        "size_t",
        "ptrdiff_t",
        "int8_t",
        "int16_t",
        "int32_t",
        "int64_t",
        "uint8_t",
        "uint16_t",
        "uint32_t",
        "uint64_t",
        "uintptr_t",
        "intptr_t",
        "__VA_ARGS__",
    }
)


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=MAX_GIT_SECONDS, check=False)
    if result.returncode:
        raise ValueError(result.stderr.decode("utf-8", errors="replace").strip())
    return result.stdout.decode("utf-8", errors="replace").strip()


def prepare_repository(value, cache):
    candidate = Path(value).expanduser()
    if candidate.is_dir():
        return candidate.resolve(), {"input": str(candidate.resolve()), "cloned": False}
    parsed = urllib.parse.urlsplit(value)
    remote = parsed.scheme in {"https", "ssh"} or bool(re.match(r"^[\w.-]+@[\w.-]+:[\w./-]+$", value))
    if not remote:
        raise ValueError("Repository must be an existing local directory or HTTPS/SSH Git URL")
    if parsed.scheme == "https" and (parsed.username or parsed.password):
        raise ValueError("Use Git credential manager; do not put credentials in repository URLs")
    Path(cache).mkdir(parents=True, exist_ok=True)
    destination = Path(tempfile.mkdtemp(prefix="repo-", dir=cache)) / "checkout"
    print("Cloning repository into " + str(destination), flush=True)
    result = subprocess.run(
        ["git", "clone", "--depth", "1", "--no-recurse-submodules", "--", value, str(destination)],
        capture_output=True,
        timeout=MAX_GIT_SECONDS,
        check=False,
    )
    if result.returncode:
        raise ValueError("Git clone failed: " + result.stderr.decode("utf-8", errors="replace"))
    return destination.resolve(), {"input": value, "cloned": True}


def category(path):
    parts = {part.lower() for part in Path(path).parts}
    if parts & {"test", "tests", "testing"}:
        return "测试"
    if parts & {"example", "examples", "sample", "samples", "demo", "demos", "benchmarks"}:
        return "示例/基准"
    if Path(path).suffix.lower() in {".md", ".mdx", ".rst"} or parts & {"docs", "doc"}:
        return "文档"
    if Path(path).suffix.lower() in {".cmake", ".sh", ".yaml", ".yml", ".toml", ".json"} or Path(path).name in {
        "CMakeLists.txt",
        "Dockerfile",
    }:
        return "构建/配置"
    return "正式源码"


def blank(text):
    return "".join("\n" if char == "\n" else " " for char in text)


def markdown_code(text):
    lines = text.splitlines(keepends=True)
    output = []
    fence = None
    for line in lines:
        found = re.match(r"^\s*(`{3,}|~{3,})(.*)$", line)
        if found:
            if fence is None:
                fence = found[1]
            elif found[1][0] == fence[0] and len(found[1]) >= len(fence) and not found[2].strip():
                fence = None
            output.append(blank(line))
        elif fence or line.startswith("    ") or line.startswith("\t"):
            output.append(line)
        else:
            result = list(blank(line))
            for match in re.finditer(r"(`+)(.+?)\1", line):
                result[match.start(2) : match.end(2)] = match[2]
            output.append("".join(result))
    return "".join(output)


def strip_comments(text, suffix):
    if suffix == ".py":
        lines = text.splitlines(keepends=True)
        try:
            for token in tokenize.generate_tokens(io.StringIO(text).readline):
                if token.type == tokenize.COMMENT:
                    start, end = token.start[1], token.end[1]
                    line = lines[token.start[0] - 1]
                    lines[token.start[0] - 1] = line[:start] + " " * (end - start) + line[end:]
        except (tokenize.TokenError, IndentationError):
            pass
        return "".join(lines)
    return CPP_LEXEMES.sub(lambda match: blank(match[0]) if match[0].startswith(("//", "/*")) else match[0], text)


def infer_kind(name, after=""):
    leaf = name.split("::")[-1].split(".")[-1]
    if leaf.isupper():
        return "宏/常量/枚举值（待细分）"
    if after.lstrip().startswith(("(", "<")):
        return "函数/方法/模板（待细分）"
    return "类型/结构体/枚举/符号（待细分）"


def enumerate_sources(root, excluded):
    sources, skipped, errors = {}, [], []
    for directory, dirs, files in os.walk(root, followlinks=False):
        current = Path(directory)
        for name in list(dirs):
            child = current / name
            if name in SKIP_DIRS or child.is_symlink() or child.resolve() in excluded:
                dirs.remove(name)
                skipped.append(
                    {"path": child.relative_to(root).as_posix(), "reason": "生成物/缓存/元数据目录或符号链接"}
                )
        for name in sorted(files):
            path = current / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink() or path.resolve() in excluded:
                skipped.append({"path": relative, "reason": "符号链接或本次输出"})
                continue
            if path.suffix.lower() in BINARY_SUFFIXES:
                skipped.append({"path": relative, "reason": "二进制类型"})
                continue
            try:
                if path.stat().st_size > MAX_SOURCE_BYTES:
                    errors.append({"path": relative, "reason": "文件超过 8 MiB，需单独检查"})
                    continue
                data = path.read_bytes()
                if b"\0" in data[:8192]:
                    skipped.append({"path": relative, "reason": "检测到二进制内容"})
                    continue
                text = data.decode("utf-8-sig")
                sources[relative] = text
            except (UnicodeError, OSError) as error:
                errors.append({"path": relative, "reason": str(error)})
    return sources, skipped, errors


def header_contexts(sources):
    by_basename = defaultdict(list)
    for path in sources:
        by_basename[Path(path).name].append(path)
    edges, context = {}, {}
    for path, raw in sources.items():
        text = markdown_code(raw) if Path(path).suffix in {".md", ".mdx"} else raw
        includes = re.findall(r'^\s*#\s*include\s*[<"]([^>"\n]+)[>"]', text, re.M)
        linked, signals = [], []
        for header in includes:
            relative = (Path(path).parent / header).as_posix()
            normalized = os.path.normpath(relative).replace("\\", "/")
            if normalized in sources:
                linked.append(normalized)
            elif header in sources:
                linked.append(header)
            elif len(by_basename[Path(header).name]) == 1:
                linked.extend(by_basename[Path(header).name])
            elif CANN_HEADER.search(header):
                signals.append("外部头文件: " + header)
        if re.search(r"\b(?:AscendC|platform_ascendc)::", text) or "using namespace AscendC" in text:
            signals.append("Ascend C 命名空间")
        context[path] = signals
        edges[path] = linked
    # Local include propagation reaches a fixed point; no external dependency traversal.
    changed = True
    while changed:
        changed = False
        for path, targets in edges.items():
            if not context[path]:
                target = next((target for target in targets if context[target]), None)
                if target:
                    context[path] = ["经本仓头文件: " + target] + context[target][:2]
                    changed = True
    return context


def python_aliases(text):
    tree = ast.parse(text)
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.name.split(".")[0] in CANN_PYTHON_ROOTS:
                    aliases[item.asname or item.name.split(".")[0]] = (
                        item.name if item.asname else item.name.split(".")[0]
                    )
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] in CANN_PYTHON_ROOTS:
            for item in node.names:
                if item.name != "*":
                    aliases[item.asname or item.name] = node.module + "." + item.name
    return tree, aliases


def python_snippets(raw, suffix):
    if suffix == ".py":
        return [raw]
    if suffix not in {".md", ".mdx"}:
        return []
    snippets, block, fence, start, language = [], [], None, 0, ""
    for number, line in enumerate(raw.splitlines(keepends=True)):
        marker = re.match(r"^\s*(`{3,}|~{3,})(.*)$", line)
        if marker and fence is None:
            fence, language, start = marker[1], marker[2].strip().lower(), number + 1
            block = []
        elif marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
            content = "".join(block)
            if language in {"python", "py", "python3"} or (
                not language and re.search(r"\b(?:import|from)\s+(?:acl|tbe|te|tik)\b", content)
            ):
                snippets.append("\n" * start + content)
            fence = None
        elif fence:
            block.append(line)
    return snippets


def attribute_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = attribute_name(node.value)
        return parent + "." + node.attr if parent else ""
    return ""


def scan_repository(root, metadata, excluded=()):
    root = Path(root).resolve()
    sources, skipped, errors = enumerate_sources(root, {Path(p).resolve() for p in excluded})
    contexts = header_contexts(sources)
    occurrences, seen, gaps = [], set(), []
    definitions = defaultdict(list)
    digest = hashlib.sha256()
    for path in sorted(sources):
        digest.update(path.encode() + b"\0" + sources[path].encode() + b"\0")
    metadata = {**metadata, "root": str(root), "name": root.name, "scanned_at": now(), "files_read": len(sources)}
    try:
        metadata["commit"] = git(root, "rev-parse", "HEAD")
        metadata["worktree_status"] = git(root, "status", "--short")
        submodules = git(root, "submodule", "status")
        metadata["submodules"] = submodules
        for line in submodules.splitlines():
            if line.startswith("-"):
                gaps.append({"path": line, "reason": "子模块未检出；未自动追踪/下载其实现"})
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        metadata["git_note"] = str(error)

    def add(name, path, line, source, kind, signals):
        key = (name, path, line)
        if key not in seen:
            seen.add(key)
            occurrences.append(
                {
                    "name": name,
                    "path": path,
                    "line": line,
                    "source": source.strip(),
                    "kind": kind,
                    "category": category(path),
                    "signals": list(signals),
                }
            )

    for file_number, (path, raw) in enumerate(sources.items(), 1):
        if file_number % 400 == 0:
            print(f"Source files: {file_number}/{len(sources)}; occurrences: {len(occurrences)}", flush=True)
        suffix = Path(path).suffix.lower()
        text = markdown_code(raw) if suffix in {".md", ".mdx"} else raw
        if suffix == ".rst" and re.search(r"CANN|AscendC|aclrt|Hccl", raw):
            gaps.append({"path": path, "reason": "RST 文档按文本候选扫描，需区分代码块和普通叙述"})
        text = strip_comments(text, suffix)
        lines = raw.splitlines()
        context = contexts[path]
        if suffix in CPP_SUFFIXES and context and re.search(r"##|\bdlsym\b|\bnamespace\s+\w+\s*=", text):
            gaps.append({"path": path, "reason": "宏拼接、动态符号或 namespace 别名需检查展开/归属"})
        for snippet in python_snippets(raw, suffix):
            try:
                tree, aliases = python_aliases(snippet)
                attribute_bases = {id(node.value) for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
                for node in ast.walk(tree):
                    name = attribute_name(node)
                    root_name = name.split(".")[0]
                    if name and root_name in aliases and id(node) not in attribute_bases:
                        canonical = aliases[root_name] + name[len(root_name) :]
                        if canonical.split(".")[0] in CANN_PYTHON_ROOTS and (
                            isinstance(node, ast.Attribute) or "." in aliases[root_name]
                        ):
                            add(
                                canonical,
                                path,
                                node.lineno,
                                lines[node.lineno - 1],
                                "Python 接口/属性",
                                ["Python AST 导入: " + aliases[root_name], "别名作用域/遮蔽待复核"],
                            )
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        definitions[node.name].append(f"{path}:{node.lineno}")
                star_import = any(
                    isinstance(node, ast.ImportFrom)
                    and node.module
                    and node.module.split(".")[0] in CANN_PYTHON_ROOTS
                    and any(item.name == "*" for item in node.names)
                    for node in ast.walk(tree)
                )
                if star_import or (aliases and re.search(r"\b(?:getattr|import_module|__import__)\s*\(", snippet)):
                    gaps.append(
                        {"path": path, "reason": "CANN Python 动态导入/属性或星号导入需补查；实例方法需检查接收者类型"}
                    )
                if aliases and re.search(r"=\s*\w+(?:\.\w+)+\s*\(", snippet):
                    gaps.append({"path": path, "reason": "可能创建 CANN Python 对象：需追踪本仓实例方法并补充规范名称"})
            except (SyntaxError, ValueError) as error:
                gaps.append({"path": path, "reason": "Python AST 失败，保留词法候选: " + str(error)})
        # Namespace aliases are resolved lexically, then explicitly left for scope review.
        aliases = {
            match[1]: match[2]
            for match in re.finditer(r"\bnamespace\s+(\w+)\s*=\s*(AscendC|ge|gert|optiling|matmul)\s*;", text)
        }
        if aliases:
            alias_pattern = re.compile(r"\b(" + "|".join(map(re.escape, aliases)) + r")((?:::\w+)+)")
            for match in alias_pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                add(
                    aliases[match[1]] + match[2],
                    path,
                    line,
                    lines[line - 1],
                    "命名空间接口",
                    ["namespace 别名: " + match[1]] + context,
                )
        covered = []
        for match in QUALIFIED.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            add(
                match[0],
                path,
                line,
                lines[line - 1],
                infer_kind(match[0], text[match.end() :]),
                ["限定命名空间（需验证归属）"] + context,
            )
            covered.append((match.start(), match.end()))
        # Infer simple declared receiver types. Complex templates/auto remain review candidates.
        receivers = {}
        declarations = re.finditer(
            r"\b((?:AscendC::)?(?:LocalTensor|GlobalTensor|TQue|TQueBind|TBuf|TPipe|Matmul|"
            r"DataCopyPadParams|DataCopyParams))\s*(?:<[^;{}\n]+>)?\s*[*&]?\s+(\w+)\s*[;=,{]",
            text,
        )
        for match in declarations:
            receivers[match[2]] = match[1] if "::" in match[1] else "AscendC::" + match[1]
        if context:
            for match in re.finditer(r"\b(\w+)\s*(?:\.|->)\s*(\w+)\s*(?:<[^;\n]+?>)?\s*\(", text):
                line = text.count("\n", 0, match.start()) + 1
                receiver, method = match[1], match[2]
                if receiver in receivers:
                    add(
                        receivers[receiver] + "::" + method,
                        path,
                        line,
                        lines[line - 1],
                        "类方法",
                        ["接收者类型（词法推断）: " + receivers[receiver]] + context,
                    )
                    covered.append((match.start(2), match.end(2)))
                elif method[0].isupper():
                    add(
                        method,
                        path,
                        line,
                        lines[line - 1],
                        "方法（接收者类型待核实）",
                        ["未解析接收者: " + receiver] + context,
                    )
        covered.sort()
        covered_starts = [start for start, _ in covered]
        string_spans = [
            (match.start(), match.end()) for match in CPP_LEXEMES.finditer(text) if match[0].startswith(('"', "'"))
        ]
        string_starts = [start for start, _ in string_spans]
        using_ascend = "using namespace AscendC" in text
        for match in TOKEN.finditer(text):
            name = match[0]
            covered_index = bisect_right(covered_starts, match.start()) - 1
            if covered_index >= 0 and match.start() < covered[covered_index][1]:
                continue
            if name in CPP_IGNORE or name in NAMESPACES or len(name) == 1:
                continue
            prefix = bool(CANN_PREFIX.fullmatch(name))
            string_index = bisect_right(string_starts, match.start()) - 1
            if not prefix and string_index >= 0 and match.start() < string_spans[string_index][1]:
                continue
            after = text[match.end() : match.end() + LOOKAHEAD_CHARS]
            broad = bool(
                context
                and (suffix in CPP_SUFFIXES or category(path) == "文档")
                and (name[0].isupper() or name in CANN_LANGUAGE_SYMBOLS or re.match(r"\s*\(", after))
            )
            if not prefix and not broad:
                continue
            before = text[max(0, match.start() - 80) : match.start()]
            # A third-party Python attribute is not a directly used CANN interface.
            if re.search(r"(?:torch_npu|torch\.ops\.npu|torch\.npu)\.[\w.]*$", before):
                continue
            if broad and not prefix:
                if re.search(r"(?:std|torch|at|c10)::\s*$", before):
                    continue
                if (
                    not re.match(r"\s*(?:[<(]|::|[*&]?\s*[A-Za-z_]\w*\s*[,;=)({])", after)
                    and not name.isupper()
                    and name not in CANN_LANGUAGE_SYMBOLS
                ):
                    continue
            line = text.count("\n", 0, match.start()) + 1
            signals = ["CANN 风格名称（仅线索）"] if prefix else ["CANN 上下文中的未限定符号"]
            signals += context
            if using_ascend:
                signals.append("using namespace AscendC；需排除局部同名")
            if re.search(r"(?:#\s*define|class|struct|enum(?:\s+class)?|typedef\s+\w+|using)\s*$", before):
                definitions[name].append(f"{path}:{line}")
            if re.match(r"\s*\([^;{}]*\)\s*(?:const\s*)?\{", after):
                definitions[name].append(f"{path}:{line}")
            # Dynamic binding strings and macro arguments intentionally survive lexical scanning.
            if '"' + name + '"' in lines[line - 1] or "'" + name + "'" in lines[line - 1]:
                signals.append("字符串符号：需验证动态绑定/接口引用")
            add(name, path, line, lines[line - 1], infer_kind(name, after), signals)
    occurrences.sort(key=lambda item: (item["name"], item["path"], item["line"]))
    definition_locations = {name: sorted(set(locations)) for name, locations in definitions.items()}
    for occurrence in occurrences:
        local = definition_locations.get(occurrence["name"])
        if local:
            unique = local
            occurrence["signals"].append(
                "本仓同名定义（可能是本地接口/包装/绑定）: "
                + ", ".join(unique[:MAX_INLINE_DEFINITIONS])
                + (
                    f"；共 {len(unique)} 处，完整位置见 local_definitions"
                    if len(unique) > MAX_INLINE_DEFINITIONS
                    else ""
                )
            )
    return {
        "schema": 1,
        "scan_fingerprint": digest.hexdigest(),
        "repository": metadata,
        "occurrences": occurrences,
        "skipped": skipped,
        "errors": errors,
        "gaps": gaps,
        "local_definitions": definition_locations,
        "counts_by_category": dict(Counter(item["category"] for item in occurrences)),
        "limitations": [
            "静态候选非完整语义解析：需人工检查宏展开、类型推导、导入遮蔽和同名本地接口",
            "不会深入第三方依赖组件内部追踪间接 CANN 调用",
        ],
    }
