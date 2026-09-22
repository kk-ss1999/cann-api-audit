import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import audit
import batch_audit as batch
from cann_docs import catalog_digest


class BatchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.repo = self.work / "repo"
        self.repo.mkdir()
        (self.repo / "main.cpp").write_text("sharedCall();\n", encoding="utf-8")
        self.entries = []
        for target in ("a", "b"):
            entry = f"https://example.org/{target}/"
            profile = {
                "name": target,
                "version": "1",
                "docs_entry": entry,
                "official_hosts": ["example.org"],
                "api_roots": [entry + "api/"],
                "rules": [
                    {
                        "kind": "symbol",
                        "value": "sharedCall",
                        "reason": "Fixture",
                        "evidence_url": entry + "api/call.html",
                    }
                ],
            }
            self.write(target + "-profile.json", profile)
            (self.work / (target + ".md")).write_text(
                "# API\n" + ("sharedCall" if target == "a" else "differentCall") + " documented API body.",
                encoding="utf-8",
            )
            self.write(
                target + "-docs.json",
                {
                    "docs_entry": entry,
                    "version": "1",
                    "coverage_reviewed": True,
                    "coverage_note": "One-page test catalog",
                    "pages": [
                        {
                            "url": entry + "api/call.html",
                            "title": "API",
                            "version": "1",
                            "fetched_at": "test",
                            "body_file": target + ".md",
                        }
                    ],
                },
            )
            self.entries.append(
                {
                    "docs_entry": entry,
                    "profile": target + "-profile.json",
                    "docs_manifest": target + "-docs.json",
                }
            )
        self.spec = self.work / "targets.json"
        self.output = self.work / "report.md"

    def write(self, name, value):
        (self.work / name).write_text(json.dumps(value), encoding="utf-8")

    def execute(self, entries=None):
        self.write("targets.json", {"targets": self.entries if entries is None else entries})
        return batch.run(str(self.repo), self.spec, self.output)

    def test_single_checkout_snapshot_and_same_name_evidence_isolation(self):
        with (
            patch.object(batch, "prepare_repository", wraps=batch.prepare_repository) as prepare,
            patch.object(batch, "enumerate_sources", wraps=batch.enumerate_sources) as read,
        ):
            index = self.execute()
        self.assertEqual(prepare.call_count, 1)
        self.assertEqual(read.call_count, 1)
        a, b = index["targets"]
        self.assertIn("sharedCall", audit.read_json(a["paths"]["catalog"])["matches"])
        self.assertNotIn("sharedCall", audit.read_json(b["paths"]["catalog"])["matches"])
        self.assertNotEqual(a["paths"]["review"], b["paths"]["review"])
        self.assertIn("阶段性结果", self.output.read_text(encoding="utf-8"))
        for target in index["targets"]:
            paths = target["paths"]
            review = audit.read_json(paths["review"])
            review.update(
                scope_reviewed=True,
                catalog_reviewed=True,
                catalog_fingerprint=catalog_digest(audit.read_json(paths["catalog"])),
            )
            review["symbols"]["sharedCall"] = {
                "origin": "target",
                "reason": "Fixture ownership",
                "searched": True,
                "search_note": "Checked complete fixture",
            }
            Path(paths["review"]).write_text(json.dumps(review), encoding="utf-8")
        self.assertTrue(batch.render(index, self.output))
        text = self.output.read_text(encoding="utf-8")
        self.assertIn("| a | 1 | 1 | 0 | 0 | 已完成 |", text)
        self.assertIn("| b | 1 | 0 | 1 | 0 | 已完成 |", text)

    def test_unreadable_target_does_not_hide_other_results(self):
        index = self.execute(
            [
                self.entries[0],
                {"docs_entry": "https://example.org/unreadable/", "error": "Cannot read official entry"},
            ]
        )
        self.assertNotIn("error", index["targets"][0])
        self.assertIn("error", index["targets"][1])
        self.assertFalse(batch.render(index, self.output))
        text = self.output.read_text(encoding="utf-8")
        self.assertIn("sharedCall", text)
        self.assertIn("Cannot read official entry", text)

    def test_reorder_preserves_review_and_target_change_rejects_it(self):
        first = self.execute()
        second = self.execute(list(reversed(self.entries)))
        self.assertEqual(first["targets"][0]["paths"], second["targets"][1]["paths"])
        profile = audit.read_json(self.work / "a-profile.json")
        profile["version"] = "2"
        self.write("a-profile.json", profile)
        changed = self.execute()
        self.assertIn("fingerprint", changed["targets"][0]["error"])
        self.assertNotIn("error", changed["targets"][1])

    def test_duplicates_and_empty_list_are_rejected(self):
        for entries in ([], [self.entries[0], self.entries[0]]):
            with self.assertRaises(ValueError):
                self.execute(entries)


if __name__ == "__main__":
    unittest.main()
