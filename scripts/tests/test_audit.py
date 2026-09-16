"""Behavioral fixtures: scanner, evidence boundaries, failures and report classification."""

import json
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audit
import cann_docs
import scan_repo


class AuditTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="cann audit test ")
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def scan(self):
        return scan_repo.scan_repository(self.root, {"input": str(self.root)})

    def test_direct_c_functions_types_enums_macros_and_source_lines(self):
        self.write(
            "src/main.cpp",
            '#include "acl/acl.h"\naclrtStream stream;\naclrtMalloc(&p, n, ACL_MEM_MALLOC_HUGE_FIRST);\n',
        )
        result = self.scan()
        names = {item["name"] for item in result["occurrences"]}
        self.assertTrue({"aclrtStream", "aclrtMalloc", "ACL_MEM_MALLOC_HUGE_FIRST"} <= names)
        malloc = next(item for item in result["occurrences"] if item["name"] == "aclrtMalloc")
        self.assertEqual(malloc["line"], 3)
        self.assertIn("外部头文件: acl/acl.h", malloc["signals"])

    def test_python_aliases_do_not_emit_module_as_api(self):
        self.write(
            "main.py", "import acl as a\nfrom acl.rt import malloc as allocate\na.rt.malloc(1, 0)\nallocate(1, 0)\n"
        )
        occurrences = self.scan()["occurrences"]
        self.assertEqual({item["name"] for item in occurrences}, {"acl.rt.malloc"})
        self.assertEqual({item["line"] for item in occurrences}, {3, 4})

    def test_third_party_calls_are_not_traversed(self):
        self.write("main.py", "import torch_npu\ntorch_npu.npu_fusion_attention(x)\ntorch.ops.npu.npu_add(x)\n")
        self.assertFalse(self.scan()["occurrences"])

    def test_cann_python_modules_beyond_acl(self):
        self.write(
            "operator.py",
            "from tbe import dsl as d\nfrom te import tik\n"
            "d.vadd(a, b)\ninstance = tik.Tik()\ninstance.data_move(a,b)\n",
        )
        result = self.scan()
        names = {item["name"] for item in result["occurrences"]}
        self.assertTrue({"tbe.dsl.vadd", "te.tik.Tik"} <= names)
        self.assertTrue(any("实例方法" in item["reason"] for item in result["gaps"]))

    def test_dynamic_hccl_and_cpp_dlsym_are_candidates(self):
        self.write(
            "wrapper.py", 'import ctypes\nlib = ctypes.CDLL("libhccl.so")\nf = getattr(lib, "HcclCommInitRootInfo")\n'
        )
        self.write("src/main.cpp", '#include "runtime/base.h"\nauto f = dlsym(handle, "rtGetDevice");\n')
        names = {item["name"] for item in self.scan()["occurrences"]}
        self.assertTrue({"HcclCommInitRootInfo", "rtGetDevice"} <= names)

    def test_ascend_class_receiver_namespace_alias_and_lowercase_intrinsic(self):
        self.write(
            "kernel.cpp",
            '#include "kernel_operator.h"\nusing namespace AscendC;\nLocalTensor<float> x;\nx.GetValue(0);\n'
            "namespace AC = AscendC;\nAC::DataCopy(a,b,n);\npipe_barrier(PIPE_ALL);\n",
        )
        names = {item["name"] for item in self.scan()["occurrences"]}
        self.assertTrue(
            {"LocalTensor", "AscendC::LocalTensor::GetValue", "AscendC::DataCopy", "pipe_barrier", "PIPE_ALL"} <= names
        )
        self.assertNotIn("GetValue", names)

    def test_cann_language_qualifiers_types_and_non_api_cpp_tokens(self):
        self.write(
            "kernel.cpp",
            '#include "kernel_operator.h"\n__aicore__ inline void run() {\n'
            'half value;\nconstexpr int n = 1;\nconst char* message = "NOT_AN_API";\n}\n',
        )
        names = {item["name"] for item in self.scan()["occurrences"]}
        self.assertTrue({"__aicore__", "half"} <= names)
        self.assertFalse({"constexpr", "const", "NOT_AN_API", "void"} & names)

    def test_local_includes_propagate_context_and_local_definitions_are_flagged(self):
        self.write("src/bridge.h", '#include "kernel_operator.h"\n')
        self.write(
            "src/kernel.cpp", '#include "bridge.h"\nvoid aclnnLocal() {}\nLocalTensor<float> data;\naclnnLocal();\n'
        )
        result = self.scan()
        self.assertIn("aclnnLocal", result["local_definitions"])
        item = next(x for x in result["occurrences"] if x["name"] == "LocalTensor")
        self.assertTrue(any("经本仓头文件" in value for value in item["signals"]))

    def test_markdown_fences_inline_and_comments(self):
        self.write(
            "docs/demo.md",
            "plain aclrtNotUsage prose\n```cpp\naclInit(nullptr);\n"
            "// aclrtCommentOnly();\n```\nUse `aclFinalize()` here.\n",
        )
        names = {item["name"] for item in self.scan()["occurrences"]}
        self.assertEqual(names, {"aclInit", "aclFinalize"})

    def test_python_aliases_inside_document_code_blocks(self):
        self.write("docs/sample.md", "# Example\n```python\nimport acl as a\na.rt.malloc(16, 0)\n```\n")
        result = self.scan()
        matched = [item for item in result["occurrences"] if item["name"] == "acl.rt.malloc"]
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["line"], 4)

    def test_star_import_is_a_gap_even_without_known_names(self):
        self.write("operator.py", "from tbe import *\nunknown_function(x)\n")
        self.assertTrue(self.scan()["gaps"])

    def test_tests_examples_and_hidden_configuration_are_scanned(self):
        for path in ["tests/test_api.cpp", "examples/demo.cpp", ".github/workflows/check.yml"]:
            self.write(path, "aclInit(0)\n")
        categories = {item["category"] for item in self.scan()["occurrences"]}
        self.assertEqual(categories, {"测试", "示例/基准", "构建/配置"})

    def test_generated_directories_and_binary_files_are_accounted_for(self):
        self.write(".venv/foo.py", "aclInit(0)")
        (self.root / "binary.data").write_bytes(b"\x00aclInit")
        self.write("main.py", "print('hello')")
        result = self.scan()
        self.assertFalse(result["occurrences"])
        self.assertEqual(len(result["skipped"]), 2)

    def test_unreadable_text_and_unchecked_submodule_are_visible(self):
        (self.root / "legacy.cpp").write_bytes(b"\xff\xfe")
        with patch.object(scan_repo, "git", side_effect=["abc", "", "-deadbeef third_party/missing"]):
            result = self.scan()
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(len(result["gaps"]), 1)

    def test_bad_python_does_not_silently_disappear(self):
        self.write("bad.py", "if broken\n aclrtMalloc()")
        result = self.scan()
        self.assertTrue(result["gaps"])
        self.assertIn("aclrtMalloc", {item["name"] for item in result["occurrences"]})

    def test_git_url_is_argument_not_shell_code_and_clone_failure_is_clear(self):
        with patch.object(
            scan_repo.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, b"", b"denied")
        ) as run:
            with self.assertRaisesRegex(ValueError, "Git clone failed"):
                scan_repo.prepare_repository("https://github.com/example/repo.git", self.root / "cache")
            command = run.call_args.args[0]
            self.assertIn("--no-recurse-submodules", command)
            self.assertEqual(command[-2], "https://github.com/example/repo.git")
        with self.assertRaises(ValueError):
            scan_repo.prepare_repository("--upload-pack=evil", self.root)

    def test_html_navigation_and_script_cannot_supply_evidence(self):
        raw = (
            "<html><head><title>aclFakeTitle</title></head><body><nav>aclFakeNav</nav><h1>aclInit</h1>"
            "<script>aclFakeScript</script><p>Initialize the runtime.</p></body></html>"
        )
        text = cann_docs.article_text(raw)
        self.assertIn("aclInit", text)
        self.assertNotIn("aclFake", text)

    def test_markdown_destinations_metadata_and_error_pages_are_not_evidence(self):
        raw = (
            "<!--Metadata aclSecret -->\n# API reference\nUse [the page](https://site/aclSecret) for details.\n"
            "`aclInit` initializes the runtime.\n"
        )
        text = cann_docs.article_text(raw)
        self.assertNotIn("aclSecret", text)
        self.assertIn("aclInit", text)
        with self.assertRaises(ValueError):
            cann_docs.article_text("404 Page not found")

    def test_nuxt_uses_article_payload_only(self):
        data = ["navigation aclFake", "<h1>aclInit</h1><p>Initialize the CANN runtime.</p>"]
        raw = '<script id="__NUXT_DATA__">' + json.dumps(data) + "</script>"
        self.assertIn("aclInit", cann_docs.article_text(raw))
        self.assertNotIn("aclFake", cann_docs.article_text(raw))

    def test_identifier_boundary_case_and_class_collision(self):
        page = {"title": "GetValue", "ancestors": ["Ascend C API", "GlobalTensor"]}
        self.assertFalse(cann_docs.name_matches("aclrtMallocHost", "aclrtMalloc", page))
        self.assertFalse(cann_docs.name_matches("aclinit", "aclInit", page))
        self.assertFalse(cann_docs.name_matches("GetValue", "AscendC::LocalTensor::GetValue", page))
        self.assertTrue(cann_docs.name_matches("GetValue", "AscendC::GlobalTensor::GetValue", page))

    def test_api_selection_is_not_restricted_to_API_directory(self):
        node = {"nodeName": "HcclInit", "nodeUrl": "zh/CANNCommunityEdition/v/commlib/hcclug/api_ref/init.md"}
        self.assertTrue(cann_docs.selected_api(node, ("使用通信库API实现通信功能",), ("通信库", "HCCL集合通信库")))
        self.assertFalse(cann_docs.selected_api({"nodeName": "安装", "nodeUrl": "x/install.html"}, (), ("发行与安装",)))

    def test_latest_failure_is_incomplete_not_empty_success(self):
        with patch.object(cann_docs, "resolve_latest", side_effect=ValueError("network unavailable")):
            result = cann_docs.build_catalog(["aclInit"], self.root)
        self.assertFalse(result["complete"])
        self.assertTrue(result["failures"])

    def baseline(self):
        self.write("main.cpp", '#include "acl/acl.h"\naclPrivateCall();\n')
        scan = self.scan()
        review = audit.new_review(scan)
        review.update(scope_reviewed=True, catalog_reviewed=True)
        review["symbols"]["aclPrivateCall"] = {
            "origin": "cann",
            "reason": "Verified CANN declaration",
            "searched": True,
            "search_note": "Opened current API chapters and checked exact name",
        }
        catalog = {
            "complete": True,
            "matches": {},
            "queried_names": ["aclPrivateCall"],
            "prefix": "zh/CANNCommunityEdition/920beta2",
        }
        return scan, review, catalog

    def test_missing_requires_complete_coverage_origin_and_second_check(self):
        scan, review, catalog = self.baseline()
        result = audit.classify("aclPrivateCall", [], review, catalog, scan)
        self.assertEqual(result[0], "官方文档未找到")
        for key in ("scope_reviewed", "catalog_reviewed"):
            incomplete = {**review, key: False}
            self.assertEqual(audit.classify("aclPrivateCall", [], incomplete, catalog, scan)[0], "待核实")
        self.assertEqual(
            audit.classify("aclPrivateCall", [], review, {**catalog, "complete": False}, scan)[0], "待核实"
        )
        self.assertEqual(
            audit.classify("aclPrivateCall", [], review, {**catalog, "queried_names": []}, scan)[0], "待核实"
        )

    def test_direct_cann_syntax_plus_documentation_is_auto_matched(self):
        scan, review, catalog = self.baseline()
        catalog["matches"]["aclPrivateCall"] = [{"url": "https://www.hiascend.com/x", "title": "test"}]
        review["symbols"] = {}
        items = [item for item in scan["occurrences"] if item["name"] == "aclPrivateCall"]
        self.assertEqual(audit.classify("aclPrivateCall", items, review, catalog, scan)[0], "已匹配")

    def test_file_context_tokens_are_hidden_from_interface_report(self):
        self.write(
            "kernel.cpp",
            '#include "kernel_operator.h"\n#define ACLNN_CHECK(x) (x)\n'
            "constexpr int A8W4_BASEK = 8;\nHelperOp();\naclrtMalloc(&p, n, 0);\n",
        )
        scan = self.scan()
        review = audit.new_review(scan)
        catalog = {"matches": {}, "queried_names": [], "complete": False}
        groups = audit.grouped_occurrences(scan, review, catalog)
        self.assertIn("aclrtMalloc", groups)
        self.assertNotIn("A8W4_BASEK", groups)
        self.assertNotIn("ACLNN_CHECK", groups)
        self.assertNotIn("HelperOp", groups)

    def test_using_ascend_unqualified_name_requires_document_match(self):
        self.write(
            "kernel.cpp",
            '#include "kernel_operator.h"\nusing namespace AscendC;\nDocumentedOp(x);\nUnknownOp(x);\n',
        )
        scan = self.scan()
        review = audit.new_review(scan)
        evidence = [{"url": "https://www.hiascend.com/x", "title": "DocumentedOp"}]
        catalog = {"matches": {"DocumentedOp": evidence}, "queried_names": [], "complete": False}
        groups = audit.grouped_occurrences(scan, review, catalog)
        self.assertIn("DocumentedOp", groups)
        self.assertNotIn("UnknownOp", groups)
        self.assertEqual(audit.classify("DocumentedOp", groups["DocumentedOp"], review, catalog, scan)[0], "已匹配")

    def test_local_api_is_excluded_even_if_not_in_documents(self):
        scan, review, catalog = self.baseline()
        review["symbols"]["aclPrivateCall"] = {"origin": "local", "reason": "Defined in this repository"}
        self.assertEqual(audit.classify("aclPrivateCall", [], review, catalog, scan)[0], "非 CANN / 本地定义")

    def test_stale_review_and_wrong_version_manual_evidence_rejected(self):
        scan, review, catalog = self.baseline()
        review["scan_fingerprint"] = "stale"
        with self.assertRaises(ValueError):
            audit.validate_review(scan, review)
        with self.assertRaises(ValueError):
            audit.manual_evidence(
                {
                    "evidence_url": "https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/800/API/test.html",
                    "evidence_note": "Old",
                },
                catalog,
            )

    def test_changed_catalog_snapshot_cannot_reuse_review(self):
        scan, review, catalog = self.baseline()
        with self.assertRaisesRegex(ValueError, "snapshot"):
            audit.write_report(scan, catalog, review, self.root / "report.md")
        review["catalog_fingerprint"] = cann_docs.catalog_digest(catalog)
        audit.write_report(scan, catalog, review, self.root / "report.md")
        catalog["version"] = "new-version"
        with self.assertRaisesRegex(ValueError, "snapshot"):
            audit.write_report(scan, catalog, review, self.root / "report.md")

    def test_catalog_for_other_scan_is_rejected(self):
        scan, review, catalog = self.baseline()
        catalog["scan_fingerprint"] = "different-repository"
        with self.assertRaisesRegex(ValueError, "different scan"):
            audit.write_report(scan, catalog, review, self.root / "report.md")

    def test_network_retry_cache_and_http_200_error(self):
        url = "https://www.hiascend.com/doc/example"

        class Response:
            def __init__(self, content):
                self.url = url
                self.content = content

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, limit):
                return self.content

        with patch.object(
            cann_docs.urllib.request,
            "urlopen",
            side_effect=[urllib.error.URLError("temporary"), Response(b"Valid documentation")],
        ) as opened:
            first = cann_docs.fetch(url, self.root / "cache")
            second = cann_docs.fetch(url, self.root / "cache")
            self.assertEqual(first, second)
            self.assertEqual(opened.call_count, 2)
        with patch.object(
            cann_docs.urllib.request,
            "urlopen",
            return_value=Response(b'{"code":406,"success":false,"msg":"Referer error"}'),
        ):
            with self.assertRaisesRegex(ValueError, "Referer error"):
                cann_docs.fetch(url, self.root / "bad-cache")
        self.assertFalse(list((self.root / "bad-cache").glob("*.json")))

    def test_latest_is_resolved_again_not_taken_from_day_old_cache(self):
        page = '<link rel="canonical" href="https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/new/API/x.html">'
        with patch.object(cann_docs, "fetch", return_value={"text": page, "fetched_at": "now"}) as fetched:
            result = cann_docs.resolve_latest(self.root)
        self.assertEqual(result["version"], "new")
        self.assertTrue(fetched.call_args.kwargs["refresh"])

    def test_additional_occurrence_checks_location_and_repository_boundary(self):
        scan, review, _ = self.baseline()
        review["additional_occurrences"] = [
            {
                "name": "aclOther",
                "path": "../escape.cpp",
                "line": 1,
                "source": "anything",
                "kind": "函数",
                "category": "正式源码",
                "signals": [],
            }
        ]
        with self.assertRaises(ValueError):
            audit.validate_review(scan, review)

    def test_markdown_report_keeps_code_locations_and_unfinished_status(self):
        scan, review, catalog = self.baseline()
        review["catalog_reviewed"] = False
        output = self.root / "report space.md"
        audit.write_report(scan, catalog, review, output)
        content = output.read_text(encoding="utf-8")
        self.assertIn("main.cpp:2", content)
        self.assertIn("阶段性结果", content)
        self.assertIn("待核实", content)
        self.assertEqual(audit.cell("<script>|`"), "&lt;script&gt;&#124;&#96;")

    def test_limited_catalog_cannot_be_complete(self):
        info = {"version": "v", "prefix": "zh/CANNCommunityEdition/v", "entry": "https://www.hiascend.com/x"}
        pages = [
            {
                "url": "https://www.hiascend.com/" + str(i),
                "source_url": "https://www.hiascend.com/source/" + str(i),
                "title": "aclInit",
                "ancestors": [],
                "chapter": "API",
            }
            for i in range(2)
        ]
        with (
            patch.object(cann_docs, "resolve_latest", return_value=info),
            patch.object(cann_docs, "discover", return_value=(pages, [], [])),
            patch.object(cann_docs, "fetch_article", return_value=("aclInit initialize runtime", "today", None)),
        ):
            result = cann_docs.build_catalog(["aclInit"], self.root, max_pages=1)
        self.assertFalse(result["complete"])
        self.assertEqual(result["pages_read"], 1)
        self.assertIn("aclInit", result["matches"])


if __name__ == "__main__":
    unittest.main()
