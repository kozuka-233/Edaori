import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "edaori" / "scripts"))
import edaori  # noqa: E402


def run(*args):
    buf = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        code = edaori.main(list(args))
    return code, buf.getvalue(), err.getvalue()


class EdaoriTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        os.environ["EDAORI_HOME"] = str(self.home)
        os.environ.pop("EDAORI_THEME", None)
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)  # git の外

    def tearDown(self):
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def data(self, slug="t"):
        idx = json.loads((self.home / "index.json").read_text(encoding="utf-8"))
        return json.loads(Path(idx["themes"][slug]["path"]).read_text(encoding="utf-8"))

    def scenario(self):
        self.assertEqual(run("init", "副業の方向性", "--slug", "t")[0], 0)
        run("fork", "何を売る？", "--options", "A 動画", "B テンプレ", "C 受託",
            "--snapshot", "目的: 月10万\n前提: 平日2時間", "--pick", "1", "--tool", "claude")

    def test_fork_and_head(self):
        self.scenario()
        d = self.data()
        titles = {n["id"]: n for n in d["nodes"]}
        head = titles[d["session"]["head"]]
        self.assertEqual(head["title"], "A 動画")
        self.assertEqual(head["status"], "active")
        fork = titles[head["parentId"]]
        self.assertEqual(fork["type"], "fork")
        self.assertIn("月10万", fork["snapshot"])

    def test_done_moves_next_to_sibling(self):
        self.scenario()
        code, out, _ = run("done", "まず週3本")
        self.assertEqual(code, 0)
        self.assertIn("NEXT", out)
        self.assertIn("B テンプレ", out)
        self.assertIn("分岐点「何を売る？」の残り", out)
        self.assertEqual(self.data()["session"]["head"], "n_002")  # HEAD は分岐点へ戻る

    def test_park_requires_stopped_at_and_resume_pack(self):
        self.scenario()
        run("resume", "n_004")  # B へ
        run("note", "Notion案を3つ出した")
        run("park", "Aの結果待ち", "--stopped-at", "案3つまで。次は価格決め", "--until", "Aの結論")
        run("resume", "n_003")
        run("done", "週3本でいく")
        code, out, _ = run("resume", "n_004")
        self.assertEqual(code, 0)
        self.assertIn("月10万", out)                 # ① 分岐時点の記憶
        self.assertIn("案3つまで", out)               # ② 止めた位置
        self.assertIn("週3本でいく", out)             # ③ その後分かったこと
        self.assertIn("3行で復唱", out)

    def test_parked_not_next_until_ready(self):
        self.scenario()
        run("park", "後で", "--stopped-at", "調査前")        # A を保留
        run("reject", "時間が売れない", "--id", "n_005")      # C 却下
        code, out, _ = run("resume", "n_004")                  # B へ
        run("done", "やらない")
        _, out, _ = run("next")
        self.assertIn("保留中の枝のみ", out)
        run("ready", "n_003")
        _, out, _ = run("next")
        self.assertIn("A 動画", out)

    def test_fork_closed_suggests_wrap(self):
        self.scenario()
        run("done", "a", "--id", "n_003")
        run("done", "b", "--id", "n_004")
        run("reject", "c", "--id", "n_005")
        _, out, _ = run("next")
        self.assertIn("子の枝が全部閉じた分岐点", out)

    def test_pin_wins(self):
        self.scenario()
        run("pin", "n_005")
        _, out, _ = run("next")
        self.assertIn("C 受託", out)

    def test_report_and_tree(self):
        self.scenario()
        run("done", "週3本", "--tool", "cursor")
        _, rep, _ = run("report")
        self.assertIn("実施レポート", rep)
        self.assertIn("A 動画 → 週3本", rep)
        self.assertIn("claude / cursor", rep)
        _, tree, _ = run("tree")
        self.assertIn("◆ 何を売る？", tree)
        self.assertIn("← NEXT", tree)
        _, mm, _ = run("export", "--format", "mermaid")
        self.assertTrue(mm.startswith("flowchart TD"))

    def test_number_id_and_errors(self):
        self.scenario()
        self.assertEqual(run("show", "4")[0], 0)
        code, _, err = run("show", "n_999")
        self.assertEqual(code, 1)
        self.assertIn("ありません", err)
        self.assertEqual(run("init", "x", "--slug", "t")[0], 1)

    def test_fork_without_snapshot_warns(self):
        run("init", "t", "--slug", "t")
        _, _, err = run("fork", "Q", "--options", "x", "y")
        self.assertIn("スナップショットがありません", err)

    def test_repo_storage_and_git_exclude(self):
        repo = Path(self.tmp.name) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        os.chdir(repo)
        self.assertEqual(run("init", "開発", "--slug", "dev")[0], 0)
        self.assertTrue((repo / ".edaori" / "themes" / "dev" / "branches.json").exists())
        exclude = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        self.assertIn(".edaori/", exclude)
        self.assertFalse((repo / ".gitignore").exists())
        st = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout
        self.assertNotIn(".edaori", st)
        # 2回目で重複登録しない
        run("init", "開発2", "--slug", "dev2")
        exclude = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        self.assertEqual(exclude.count(".edaori/"), 1)

    def test_themes_switch(self):
        run("init", "one", "--slug", "one")
        run("init", "two", "--slug", "two")
        _, out, _ = run("themes")
        self.assertIn("* two", out)
        run("use", "one")
        _, out, _ = run("status")
        self.assertIn("(one)", out)


if __name__ == "__main__":
    unittest.main()
