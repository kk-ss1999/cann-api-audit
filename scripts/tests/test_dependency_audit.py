import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import audit
import dependency_audit as generic
from cann_docs import catalog_digest


class GenericAuditTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = {
            "name": "PTA",
            "version": "test-v1",
            "docs_entry": "https://docs.example.org/pta/",
            "official_hosts": ["docs.example.org"],
            "api_roots": ["https://docs.example.org/pta/api/"],
            "rules": [
                {
                    "kind": "python_module",
                    "value": "torch_npu",
                    "evidence_url": "https://docs.example.org/pta/api/index.html",
                    "reason": "Fixture import documentation",
                }
            ],
        }
        (self.root / "usage.py").write_text(
            "import torch_npu as n\nimport torch\nx = n.npu_op(t)\n"
            "# n.comment_only()\nmessage = 'n.string_only()'\n"
            "torch.relu(t)\ndef local_fn(): pass\n",
            encoding="utf-8",
        )

    def test_target_imports_without_cann_or_comment_noise(self):
        generic.validate_profile(self.profile, self.profile["docs_entry"])
        scanned = generic.scan(self.root, {}, self.profile)
        self.assertEqual([o["name"] for o in scanned["occurrences"]], ["torch_npu.npu_op"])
        self.assertEqual(scanned["occurrences"][0]["line"], 3)
        self.assertIn("local_fn", scanned["local_definitions"])
        self.assertTrue(scanned["gaps"])

    def test_native_rules_keep_locations_and_ignore_comment_literals(self):
        profile = {
            **self.profile,
            "rules": [
                {"kind": "namespace", "value": "Example"},
                {"kind": "symbol_prefix", "value": "exApi"},
            ],
        }
        (self.root / "kernel.cpp").write_text(
            '// Example::Comment();\nconst char* s = "Example::String\n";\n'
            "Example::Run();\nexApiCreate();\nother::exApiFake();\n",
            encoding="utf-8",
        )
        scanned = generic.scan(self.root, {}, profile)
        self.assertEqual(
            [(o["name"], o["line"]) for o in scanned["occurrences"]],
            [("Example::Run", 4), ("exApiCreate", 5)],
        )

    def test_target_change_invalidates_review(self):
        first = generic.scan(self.root, {}, self.profile)
        second = generic.scan(self.root, {}, {**self.profile, "name": "Different"})
        with self.assertRaises(ValueError):
            audit.validate_review(second, audit.new_review(first))

    def snapshot(self):
        (self.root / "body.md").write_text(
            "# torch_npu.npu_op\nOfficial fixture API body for test use.", encoding="utf-8"
        )
        return {
            "docs_entry": self.profile["docs_entry"],
            "version": "test-v1",
            "coverage_reviewed": True,
            "coverage_note": "Fixture has exactly one API page",
            "pages": [
                {
                    "url": "https://docs.example.org/pta/api/op.html",
                    "title": "npu_op",
                    "version": "test-v1",
                    "fetched_at": "test-time",
                    "body_file": "body.md",
                }
            ],
        }

    def test_snapshot_failures_and_wrong_scope_do_not_pass(self):
        manifest = self.snapshot()
        manifest["pages"][0]["url"] = "https://docs.example.org/cann/api/op.html"
        with self.assertRaises(ValueError):
            generic.catalog(["torch_npu.npu_op"], self.profile, manifest, self.root)
        manifest = self.snapshot()
        manifest["pages"][0]["body_file"] = "missing.md"
        result = generic.catalog(["torch_npu.npu_op"], self.profile, manifest, self.root)
        self.assertFalse(result["complete"])
        self.assertTrue(result["failures"])

    def test_same_leaf_is_not_automatically_equivalent(self):
        manifest = self.snapshot()
        (self.root / "body.md").write_text(
            "# npu_op\nThis body lacks module ownership context.", encoding="utf-8"
        )
        result = generic.catalog(["torch_npu.npu_op"], self.profile, manifest, self.root)
        self.assertFalse(result["matches"])

    def test_generic_report_requires_ownership_and_preserves_source(self):
        scanned = generic.scan(self.root, {}, self.profile)
        result = generic.catalog(["torch_npu.npu_op"], self.profile, self.snapshot(), self.root)
        review = audit.new_review(scanned)
        items = scanned["occurrences"]
        self.assertEqual(audit.classify("torch_npu.npu_op", items, review, result, scanned)[0], "待核实")
        review["symbols"]["torch_npu.npu_op"] = {"origin": "target", "reason": "Verified fixture import"}
        review.update(scope_reviewed=True, catalog_reviewed=True, catalog_fingerprint=catalog_digest(result))
        output = self.root / "report.md"
        audit.write_report(scanned, result, review, output)
        report = output.read_text(encoding="utf-8")
        self.assertIn("PTA", report)
        self.assertIn(self.profile["docs_entry"], report)
        self.assertNotIn("CANN", report)
        self.assertIn("usage.py:3", report)
        self.assertIn("已匹配", report)
        self.assertNotIn("识别线索", report)

    def test_cli_creates_reviewable_artifacts(self):
        work = self.root / "artifacts"
        work.mkdir()
        profile_path = work / "profile.json"
        manifest_path = self.root / "documents.json"
        profile_path.write_text(json.dumps(self.profile), encoding="utf-8")
        manifest_path.write_text(json.dumps(self.snapshot()), encoding="utf-8")
        output = work / "report.md"
        generic.main(
            [
                str(self.root),
                "--docs-entry",
                self.profile["docs_entry"],
                "--profile",
                str(profile_path),
                "--docs-manifest",
                str(manifest_path),
                "--output",
                str(output),
            ]
        )
        self.assertTrue(output.exists())
        self.assertTrue((work / "report.scan.json").exists())
        self.assertIn("阶段性结果", output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
