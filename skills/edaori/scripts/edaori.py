#!/usr/bin/env python3
"""Edaori (枝折) — AIチャットの分岐ナビ CLI.

会話の分岐・進捗・保留・完了を branches.json に記録し、
「今どこ」「次に何」「分岐点に戻るための文脈」を出力する。

標準ライブラリのみ。Python 3.9+。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional

VERSION = "0.1.0"
SCHEMA_VERSION = 1

STATUS = ("todo", "active", "parked", "done", "rejected")
CLOSED = ("done", "rejected")
MARK = {"todo": "○", "active": "▶", "parked": "‖", "done": "✓", "rejected": "×"}
FORK_MARK = "◆"

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # Windows のコンソール対策
    except Exception:
        pass


# ---------------------------------------------------------------- utilities
class EdaoriError(Exception):
    pass


def now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def home_dir() -> Path:
    return Path(os.environ.get("EDAORI_HOME", Path.home() / ".edaori")).expanduser()


def slugify(text: str) -> str:
    s = re.sub(r"[^\w\-]+", "-", text.strip().lower(), flags=re.UNICODE).strip("-")
    return s[:48] or _dt.datetime.now().strftime("theme-%Y%m%d-%H%M%S")


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def git_root(cwd: Optional[Path] = None) -> Optional[Path]:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(cwd or Path.cwd()), capture_output=True, text=True, check=True,
        ).stdout.strip()
        return Path(out) if out else None
    except Exception:
        return None


def ensure_git_exclude(repo: Path) -> bool:
    """.edaori/ を .git/info/exclude に登録（.gitignore は触らない）。"""
    try:
        rel = subprocess.run(
            ["git", "rev-parse", "--git-path", "info/exclude"],
            cwd=str(repo), capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:
        return False
    path = Path(rel) if Path(rel).is_absolute() else repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if any(line.strip() in (".edaori/", ".edaori", "/.edaori/") for line in text.splitlines()):
        return True
    with path.open("a", encoding="utf-8") as f:
        if text and not text.endswith("\n"):
            f.write("\n")
        f.write("# Edaori (personal, not shared)\n.edaori/\n")
    return True


def read_text_arg(value: Optional[str], file: Optional[str]) -> Optional[str]:
    if file:
        if file == "-":
            return sys.stdin.read().strip()
        return Path(file).expanduser().read_text(encoding="utf-8").strip()
    return value.strip() if value else value


# ---------------------------------------------------------------- index
def index_path() -> Path:
    return home_dir() / "index.json"


def load_index() -> Dict[str, Any]:
    idx = read_json(index_path(), {"version": SCHEMA_VERSION, "current": None, "themes": {}})
    idx.setdefault("themes", {})
    return idx


def save_index(idx: Dict[str, Any]) -> None:
    write_json(index_path(), idx)


# ---------------------------------------------------------------- theme
class Theme:
    def __init__(self, slug: str, path: Path, data: Dict[str, Any]):
        self.slug = slug
        self.path = path
        self.data = data

    # -- loading
    @classmethod
    def open(cls, slug: Optional[str] = None) -> "Theme":
        idx = load_index()
        slug = slug or os.environ.get("EDAORI_THEME") or idx.get("current")
        if not slug:
            raise EdaoriError("テーマがありません。先に `edaori.py init \"テーマ名\"` を実行してください。")
        entry = idx["themes"].get(slug)
        if not entry:
            raise EdaoriError(f"テーマ '{slug}' が見つかりません。`edaori.py themes` で一覧を確認してください。")
        path = Path(entry["path"])
        if not path.exists():
            raise EdaoriError(f"テーマ '{slug}' のファイルがありません: {path}")
        return cls(slug, path, read_json(path, {}))

    @classmethod
    def create(cls, title: str, slug: Optional[str], where: str) -> "Theme":
        idx = load_index()
        slug = slug or slugify(title)
        if slug in idx["themes"]:
            raise EdaoriError(f"テーマ '{slug}' は既にあります。`use {slug}` で切り替えられます。")
        repo = git_root()
        if where == "auto":
            where = "repo" if repo else "home"
        if where == "repo":
            if not repo:
                raise EdaoriError("git リポジトリの中ではないため --where repo は使えません。")
            base = repo / ".edaori" / "themes" / slug
            ensure_git_exclude(repo)
        else:
            base = home_dir() / "themes" / slug
        path = base / "branches.json"
        t = now()
        root = new_node("n_001", None, "topic", title, "active", t)
        data = {
            "version": SCHEMA_VERSION,
            "session": {"id": slug, "title": title, "head": root["id"], "next": None,
                        "createdAt": t, "updatedAt": t, "seq": 1},
            "nodes": [root],
            "events": [],
        }
        theme = cls(slug, path, data)
        theme.log("init", root["id"], title)
        theme.save()
        idx["themes"][slug] = {"title": title, "path": str(path), "where": where,
                               "createdAt": t, "updatedAt": t}
        idx["current"] = slug
        save_index(idx)
        return theme

    def save(self) -> None:
        s = self.data["session"]
        s["updatedAt"] = now()
        nxt = self.next_candidate()
        s["next"] = nxt["node"]["id"] if nxt and nxt.get("node") else None
        write_json(self.path, self.data)
        idx = load_index()
        if self.slug in idx["themes"]:
            idx["themes"][self.slug]["updatedAt"] = s["updatedAt"]
            save_index(idx)

    # -- nodes
    @property
    def nodes(self) -> List[Dict[str, Any]]:
        return self.data["nodes"]

    @property
    def head(self) -> Dict[str, Any]:
        return self.get(self.data["session"]["head"])

    def get(self, node_id: Optional[str]) -> Dict[str, Any]:
        if node_id is None:
            return self.head
        for n in self.nodes:
            if n["id"] == node_id:
                return n
        # 番号だけでも受け付ける（例: 7 → n_007）
        if re.fullmatch(r"\d+", str(node_id)):
            return self.get(f"n_{int(node_id):03d}")
        raise EdaoriError(f"ノード '{node_id}' がありません。`edaori.py tree` でIDを確認してください。")

    def children(self, node_id: str) -> List[Dict[str, Any]]:
        return [n for n in self.nodes if n["parentId"] == node_id]

    def parent(self, node: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self.get(node["parentId"]) if node["parentId"] else None

    def ancestors(self, node: Dict[str, Any]) -> List[Dict[str, Any]]:
        out = []
        p = self.parent(node)
        while p:
            out.append(p)
            p = self.parent(p)
        return out  # 近い順

    def new_id(self) -> str:
        s = self.data["session"]
        s["seq"] = int(s.get("seq", len(self.nodes))) + 1
        return f"n_{s['seq']:03d}"

    def add(self, parent_id: str, type_: str, title: str, status: str, **extra: Any) -> Dict[str, Any]:
        n = new_node(self.new_id(), parent_id, type_, title, status, now())
        n.update({k: v for k, v in extra.items() if v is not None})
        self.nodes.append(n)
        return n

    def touch(self, node: Dict[str, Any]) -> None:
        node["updatedAt"] = now()

    def set_head(self, node: Dict[str, Any]) -> None:
        self.data["session"]["head"] = node["id"]
        if node["status"] in ("todo", "parked"):
            node["status"] = "active"
            self.touch(node)

    def leave(self, node: Dict[str, Any]) -> None:
        """閉じた枝が HEAD なら、HEAD を親（分岐点）へ戻す。"""
        if self.data["session"]["head"] == node["id"] and node["parentId"]:
            self.data["session"]["head"] = node["parentId"]

    def log(self, op: str, node_id: str, text: str = "", tool: Optional[str] = None) -> None:
        ev = {"t": now(), "op": op, "node": node_id, "text": text}
        if tool:
            ev["tool"] = tool
        self.data.setdefault("events", []).append(ev)

    # -- navigation
    def _open(self, n: Dict[str, Any]) -> bool:
        if n["status"] == "todo" or n["status"] == "active":
            return True
        return n["status"] == "parked" and bool(n.get("resumeReady"))

    def _first_open_in(self, node: Dict[str, Any], skip: set) -> Optional[Dict[str, Any]]:
        if node["id"] in skip:
            return None
        if self._open(node) and not self.children(node["id"]):
            return node
        if self._open(node) and node["type"] != "fork":
            # 子を持つ枝: 未完了の子があればそちらを優先
            for c in self.children(node["id"]):
                hit = self._first_open_in(c, skip)
                if hit:
                    return hit
            return node
        for c in self.children(node["id"]):
            hit = self._first_open_in(c, skip)
            if hit:
                return hit
        return None

    def next_candidate(self) -> Optional[Dict[str, Any]]:
        head = self.head
        skip = {head["id"]} | {a["id"] for a in self.ancestors(head)}
        # 1. ピン留め
        for n in self.nodes:
            if n.get("pinned") and n["id"] not in skip and self._open(n):
                return {"node": n, "reason": "★ピン留めした枝"}
        # 2-3. HEAD から近い順に、未着手・再開OK の枝
        chain = [head] + self.ancestors(head)
        for depth, x in enumerate(chain):
            for c in self.children(x["id"]):
                hit = self._first_open_in(c, skip)
                if hit:
                    if x["type"] == "fork":
                        why = f"分岐点「{x['title']}」の残り"
                    elif depth == 0:
                        why = "今の枝の中の未完了"
                    else:
                        why = f"「{x['title']}」の残り"
                    return {"node": hit, "reason": why}
        # 4. 子が全部閉じたのに開いたままの分岐点
        for n in self.nodes:
            kids = self.children(n["id"])
            if n["type"] == "fork" and n["status"] not in CLOSED and kids and all(k["status"] in CLOSED for k in kids):
                return {"node": n, "reason": "子の枝が全部閉じた分岐点 → まとめて done にする"}
        # 5. 保留中（再開条件待ち）
        parked = [n for n in self.nodes if n["status"] == "parked"]
        if parked:
            return {"node": None, "reason": f"残りは保留中の枝のみ（{len(parked)}件）。再開条件を満たしたら `ready <id>`"}
        return None

    # -- rendering
    def label(self, n: Dict[str, Any]) -> str:
        mark = FORK_MARK if n["type"] == "fork" else MARK.get(n["status"], "?")
        s = f"{mark} {n['title']}  [{n['id']}]"
        if n["type"] == "fork" and n["status"] in CLOSED:
            s += " ✓"
        if n.get("conclusion"):
            s += f" → {n['conclusion']}"
        if n["status"] == "parked":
            s += f"  (保留: {n.get('parkReason') or '-'}"
            if n.get("resumeWhen"):
                s += f" / 再開: {n['resumeWhen']}"
            s += "{})".format(" ✔再開OK" if n.get("resumeReady") else "")
        if n["status"] == "rejected" and n.get("rejectReason"):
            s += f"  (却下: {n['rejectReason']})"
        if n.get("pinned"):
            s += " ★"
        return s

    def tree(self) -> str:
        head_id = self.data["session"]["head"]
        next_id = self.data["session"].get("next")
        lines: List[str] = []

        def walk(n: Dict[str, Any], prefix: str, last: bool, top: bool) -> None:
            tag = ""
            if n["id"] == head_id:
                tag += "  ← HEAD"
            if n["id"] == next_id:
                tag += "  ← NEXT"
            branch = "" if top else ("└─" if last else "├─")
            lines.append(f"{prefix}{branch}{self.label(n)}{tag}")
            kids = self.children(n["id"])
            new_prefix = prefix if top else prefix + ("   " if last else "│  ")
            for i, c in enumerate(kids):
                walk(c, new_prefix, i == len(kids) - 1, False)

        for r in [n for n in self.nodes if n["parentId"] is None]:
            walk(r, "", True, True)
        return "\n".join(lines)

    def open_items(self) -> List[Dict[str, Any]]:
        head_id = self.data["session"]["head"]
        anc = {a["id"] for a in self.ancestors(self.head)}
        return [n for n in self.nodes
                if n["status"] in ("todo", "parked", "active")
                and n["id"] != head_id and n["id"] not in anc
                and not (n["type"] == "fork" and n["status"] == "active")]

    def resume_pack(self, node: Dict[str, Any]) -> str:
        path = list(reversed(self.ancestors(node))) + [node]
        forks = [a for a in self.ancestors(node) if a["type"] == "fork"]
        out = [f"# Edaori 復帰パック: {node['title']}  [{node['id']}]",
               f"テーマ: {self.data['session']['title']}",
               "経路: " + " > ".join(p["title"] for p in path), ""]
        if forks:
            near = forks[0]
            out += [f"## ① 分岐した時点の記憶（{near['title']}）",
                    near.get("snapshot") or "（スナップショットなし）", ""]
            far = forks[1:]
            if far:
                out.append("### さらに上の分岐点（結論のみ）")
                for f in far:
                    decided = [f"{c['title']}: {c.get('conclusion') or c.get('rejectReason') or ''}"
                               for c in self.children(f["id"]) if c["status"] in CLOSED]
                    out.append(f"- {f['title']}" + (f"（{' / '.join(decided)}）" if decided else ""))
                out.append("")
        if node.get("snapshot"):
            out += ["## この分岐点のスナップショット", node["snapshot"], ""]
        out.append("## ② この枝の途中経過")
        for note in node.get("notes", []):
            out.append(f"- {note}")
        if node.get("stoppedAt"):
            out.append(f"- 止めた位置: {node['stoppedAt']}")
        if node.get("parkReason"):
            out.append(f"- 止めた理由: {node['parkReason']}")
        if node.get("resumeWhen"):
            out.append(f"- 再開条件: {node['resumeWhen']}")
        if len(out) and out[-1] == "## ② この枝の途中経過":
            out.append("- （記録なし）")
        out.append("")
        since = node.get("updatedAt", "")
        learned = [n for n in self.nodes
                   if n["id"] != node["id"] and n["status"] == "done" and n.get("conclusion")
                   and n.get("updatedAt", "") >= since]
        out.append("## ③ その後、他の枝で分かったこと")
        if learned:
            for n in learned[-10:]:
                out.append(f"- {n['title']}: {n['conclusion']}")
        else:
            out.append("- （なし）")
        out += ["", "## 再開前にやること",
                "このパックを読んだら、目的・前提・次の一手を3行で復唱し、ユーザーの確認を取ってから再開してください。"]
        return "\n".join(out)

    def report(self) -> str:
        s = self.data["session"]
        tools = sorted({e.get("tool") for e in self.data.get("events", []) if e.get("tool")})
        out = [f"■ 実施レポート: {s['title']}   {s['createdAt'][:10]}〜{s['updatedAt'][:10]}"
               + (f"  ({' / '.join(tools)})" if tools else ""), ""]
        byid = {n["id"]: n for n in self.nodes}
        done_ev = [e for e in self.data.get("events", []) if e["op"] in ("done", "note", "fork", "reject")]
        out.append("✓ 実施したこと（時系列）")
        if not done_ev:
            out.append("  （なし）")
        for e in done_ev:
            n = byid.get(e["node"], {"title": "?"})
            t = e["t"][5:16].replace("T", " ")
            tool = f"  [{e['tool']}]" if e.get("tool") else ""
            if e["op"] == "done":
                out.append(f"  {t} {n['title']} → {e['text']}{tool}")
            elif e["op"] == "fork":
                out.append(f"  {t} 分岐: {n['title']}（{e['text']}）{tool}")
            elif e["op"] == "reject":
                out.append(f"  {t} 却下: {n['title']}（{e['text']}）{tool}")
            else:
                out.append(f"  {t} {n['title']}: {e['text']}{tool}")
        decided = [n for n in self.nodes if n["status"] in CLOSED and n["parentId"]]
        out += ["", "◆ 決めたこと"]
        out += [f"  ・{n['title']}: {n.get('conclusion') or '却下 — ' + (n.get('rejectReason') or '')}"
                for n in decided] or ["  （なし）"]
        parked = [n for n in self.nodes if n["status"] == "parked"]
        out += ["", "‖ 保留中"]
        out += [f"  ・{n['title']}  止めた位置: {n.get('stoppedAt') or '-'} / 再開: {n.get('resumeWhen') or '-'}"
                for n in parked] or ["  （なし）"]
        todo = [n for n in self.nodes if n["status"] == "todo"]
        out += ["", "○ 未着手"]
        out += [f"  ・{n['title']}" for n in todo] or ["  （なし）"]
        nxt = self.next_candidate()
        out += ["", "→ 次にやること: " + (nxt["node"]["title"] if nxt and nxt.get("node")
                                       else (nxt["reason"] if nxt else "なし（すべて完了）"))]
        return "\n".join(out)

    def mermaid(self) -> str:
        shape = {"fork": ("{{", "}}"), "topic": ("([", "])"), "branch": ("[", "]")}
        lines = ["flowchart TD"]
        for n in self.nodes:
            a, b = shape.get(n["type"], ("[", "]"))
            title = n["title"].replace('"', "'")
            lines.append(f'  {n["id"]}{a}"{MARK.get(n["status"], "")} {title}"{b}')
        for n in self.nodes:
            if n["parentId"]:
                lines.append(f"  {n['parentId']} --> {n['id']}")
        lines.append(f"  style {self.data['session']['head']} stroke-width:3px")
        return "\n".join(lines)


def new_node(id_: str, parent: Optional[str], type_: str, title: str, status: str, t: str) -> Dict[str, Any]:
    return {"id": id_, "parentId": parent, "type": type_, "title": title, "status": status,
            "notes": [], "conclusion": None, "createdAt": t, "updatedAt": t}


# ---------------------------------------------------------------- commands
def print_status(th: Theme) -> None:
    head = th.head
    nxt = th.next_candidate()
    print(f"テーマ: {th.data['session']['title']}  ({th.slug})")
    print(f"NOW  : {th.label(head)}")
    if nxt and nxt.get("node"):
        print(f"NEXT : {th.label(nxt['node'])}   理由: {nxt['reason']}")
    else:
        print(f"NEXT : {nxt['reason'] if nxt else 'なし（すべて完了）'}")
    items = th.open_items()
    print(f"未完了: {len(items)}件")
    for n in items:
        print(f"  {th.label(n)}")


def cmd_init(a):
    th = Theme.create(a.title, a.slug, a.where)
    print(f"テーマを作成しました: {a.title}  ({th.slug})\n保存先: {th.path}")


def cmd_use(a):
    idx = load_index()
    if a.slug not in idx["themes"]:
        raise EdaoriError(f"テーマ '{a.slug}' がありません。")
    idx["current"] = a.slug
    save_index(idx)
    print(f"現在のテーマ: {a.slug}")


def cmd_themes(a):
    idx = load_index()
    if not idx["themes"]:
        print("テーマはまだありません。")
    for slug, e in sorted(idx["themes"].items(), key=lambda kv: kv[1].get("updatedAt", ""), reverse=True):
        cur = "*" if slug == idx.get("current") else " "
        print(f"{cur} {slug:24} {e['title']}  [{e.get('where')}] {e.get('updatedAt', '')[:16]}\n    {e['path']}")


def cmd_topic(a):
    th = Theme.open(a.theme)
    parent = th.get(a.parent) if a.parent else th.head
    n = th.add(parent["id"], "branch", a.title, "active", source=src(a))
    th.set_head(n)
    th.log("topic", n["id"], a.title, a.tool)
    th.save()
    print(f"新しい枝を開始: {th.label(n)}")


def cmd_fork(a):
    th = Theme.open(a.theme)
    base = th.get(a.parent) if a.parent else th.head
    snap = read_text_arg(a.snapshot, a.snapshot_file)
    if not snap:
        print("警告: スナップショットがありません。戻ったときの文脈が失われます（--snapshot / --snapshot-file）。",
              file=sys.stderr)
    fk = th.add(base["id"], "fork", a.question, "active", snapshot=snap, source=src(a))
    kids = [th.add(fk["id"], "branch", o, "todo") for o in a.options]
    th.log("fork", fk["id"], " / ".join(a.options), a.tool)
    if a.pick:
        if not (1 <= a.pick <= len(kids)):
            raise EdaoriError("--pick は選択肢の番号（1始まり）で指定してください。")
        th.set_head(kids[a.pick - 1])
        th.log("switch", kids[a.pick - 1]["id"], "", a.tool)
    else:
        th.data["session"]["head"] = fk["id"]
    th.save()
    print(f"分岐を記録しました: {th.label(fk)}")
    for i, k in enumerate(kids, 1):
        print(f"  {i}. {th.label(k)}")
    print(f"HEAD: {th.head['title']}")


def cmd_note(a):
    th = Theme.open(a.theme)
    n = th.get(a.id)
    n.setdefault("notes", []).append(a.text)
    th.touch(n)
    th.log("note", n["id"], a.text, a.tool)
    th.save()
    print(f"メモを追加: {n['title']} ← {a.text}")


def cmd_park(a):
    th = Theme.open(a.theme)
    n = th.get(a.id)
    n.update({"status": "parked", "parkReason": a.reason, "resumeWhen": a.until,
              "stoppedAt": a.stopped_at, "resumeReady": False})
    th.touch(n)
    th.leave(n)
    th.log("park", n["id"], a.reason, a.tool)
    th.save()
    print(f"保留にしました: {th.label(n)}")
    print_next(th)


def cmd_done(a):
    th = Theme.open(a.theme)
    n = th.get(a.id)
    n.update({"status": "done", "conclusion": a.conclusion, "resumeReady": False})
    th.touch(n)
    th.leave(n)
    th.log("done", n["id"], a.conclusion, a.tool)
    p = th.parent(n)
    th.save()
    print(f"完了: {th.label(n)}")
    if p and p["type"] == "fork":
        print(f"  → 分岐点「{p['title']}」に集約されました")
    print_next(th)


def cmd_reject(a):
    th = Theme.open(a.theme)
    n = th.get(a.id)
    n.update({"status": "rejected", "rejectReason": a.reason})
    th.touch(n)
    th.leave(n)
    th.log("reject", n["id"], a.reason, a.tool)
    th.save()
    print(f"却下: {th.label(n)}")
    print_next(th)


def cmd_ready(a):
    th = Theme.open(a.theme)
    n = th.get(a.id)
    if n["status"] != "parked":
        raise EdaoriError("保留中の枝ではありません。")
    n["resumeReady"] = True
    th.touch(n)
    th.log("ready", n["id"], "", a.tool)
    th.save()
    print(f"再開OKにしました: {th.label(n)}")


def cmd_pin(a):
    th = Theme.open(a.theme)
    n = th.get(a.id)
    n["pinned"] = not a.off
    th.touch(n)
    th.save()
    print(("ピン留め: " if not a.off else "ピン解除: ") + th.label(n))


def cmd_resume(a):
    th = Theme.open(a.theme)
    if a.id:
        n = th.get(a.id)
    else:
        nxt = th.next_candidate()
        if not nxt or not nxt.get("node"):
            raise EdaoriError("戻る先がありません。IDを指定してください。")
        n = nxt["node"]
    pack = th.resume_pack(n)  # 状態を変える前の記録で組み立てる
    th.set_head(n)
    th.log("resume", n["id"], "", a.tool)
    th.save()
    print(pack)


def cmd_next(a):
    th = Theme.open(a.theme)
    print_next(th)


def print_next(th: Theme) -> None:
    nxt = th.next_candidate()
    if nxt and nxt.get("node"):
        print(f"NEXT: {th.label(nxt['node'])}   理由: {nxt['reason']}")
        print(f"      戻るには: edaori.py resume {nxt['node']['id']}")
    else:
        print(f"NEXT: {nxt['reason'] if nxt else 'なし（すべて完了）'}")


def cmd_status(a):
    print_status(Theme.open(a.theme))


def cmd_tree(a):
    th = Theme.open(a.theme)
    print(th.tree())


def cmd_report(a):
    th = Theme.open(a.theme)
    text = th.report()
    if a.out:
        Path(a.out).write_text(text + "\n", encoding="utf-8")
        print(f"レポートを書き出しました: {a.out}")
    else:
        print(text)


def cmd_export(a):
    th = Theme.open(a.theme)
    if a.format == "mermaid":
        print(th.mermaid())
    elif a.format == "tree":
        print(th.tree())
    else:
        print(json.dumps(th.data, ensure_ascii=False, indent=2))


def cmd_show(a):
    th = Theme.open(a.theme)
    print(json.dumps(th.get(a.id), ensure_ascii=False, indent=2))


def cmd_view(a):
    th = Theme.open(a.theme)
    viewer = Path(__file__).resolve().parent.parent / "viewer" / "index.html"
    if not viewer.exists():
        raise EdaoriError(f"ビューアが見つかりません: {viewer}")
    data_path = th.path

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/data.json"):
                body = data_path.read_bytes()
                ctype = "application/json; charset=utf-8"
            elif self.path in ("/", "/index.html") or self.path.startswith("/?"):
                body = viewer.read_bytes()
                ctype = "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # 静かに
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", a.port), H)
    url = f"http://127.0.0.1:{srv.server_address[1]}/?live=1"
    print(f"Edaori ナビ: {url}  （Ctrl+C で終了）")
    if not a.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def cmd_install(a):
    src_dir = Path(__file__).resolve().parent.parent
    dest = Path(a.dest).expanduser() if a.dest else Path.home() / ".claude" / "skills" / "edaori"
    if dest.resolve() == src_dir.resolve():
        print("既にインストール先から実行しています。")
        return
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src_dir, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    print(f"スキルをインストールしました: {dest}")
    print("Claude Code / VS Code Copilot / Cursor が個人スキルとして読み込みます。")


def src(a) -> Optional[Dict[str, str]]:
    d = {k: v for k, v in (("tool", a.tool), ("chatUrl", getattr(a, "url", None)),
                           ("quote", getattr(a, "quote", None))) if v}
    return d or None


# ---------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="edaori.py", description="Edaori — AIチャットの分岐ナビ")
    p.add_argument("--version", action="version", version=f"edaori {VERSION}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--theme", help="テーマ（省略時は現在のテーマ）")
    common.add_argument("--tool", help="記録したAI/ツール名（claude, copilot, cursor など）")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="テーマを作る", parents=[common])
    s.add_argument("title")
    s.add_argument("--slug")
    s.add_argument("--where", choices=["auto", "repo", "home"], default="auto",
                   help="保存先: repo=リポジトリ内 .edaori/ / home=~/.edaori/（既定: gitの中ならrepo）")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("use", help="現在のテーマを切り替える")
    s.add_argument("slug")
    s.set_defaults(func=cmd_use)

    s = sub.add_parser("themes", help="テーマ一覧")
    s.set_defaults(func=cmd_themes)

    s = sub.add_parser("topic", help="HEADの下に新しい枝を作って進む", parents=[common])
    s.add_argument("title")
    s.add_argument("--parent")
    s.add_argument("--url")
    s.set_defaults(func=cmd_topic)

    s = sub.add_parser("fork", help="分岐を記録（スナップショット必須推奨）", parents=[common])
    s.add_argument("question", help="分岐点の問い（例: 何を売る？）")
    s.add_argument("--options", nargs="+", required=True, help="選択肢")
    s.add_argument("--snapshot", help="分岐時点の記憶（目的/前提/決定済み/選択肢/未解決/原文抜粋）")
    s.add_argument("--snapshot-file", help="スナップショットをファイルから（- で標準入力）")
    s.add_argument("--pick", type=int, help="すぐ進む選択肢の番号（1始まり）")
    s.add_argument("--parent", help="分岐元（省略時は HEAD）")
    s.add_argument("--url")
    s.add_argument("--quote")
    s.set_defaults(func=cmd_fork)

    s = sub.add_parser("note", help="進捗メモを追加（進む）", parents=[common])
    s.add_argument("text")
    s.add_argument("--id")
    s.set_defaults(func=cmd_note)

    s = sub.add_parser("park", help="保留にする", parents=[common])
    s.add_argument("reason", help="止めた理由")
    s.add_argument("--stopped-at", required=True, help="どこまでやったか・次に何をする予定だったか")
    s.add_argument("--until", help="再開条件")
    s.add_argument("--id")
    s.set_defaults(func=cmd_park)

    s = sub.add_parser("done", help="完了（結論1行）", parents=[common])
    s.add_argument("conclusion")
    s.add_argument("--id")
    s.set_defaults(func=cmd_done)

    s = sub.add_parser("reject", help="却下（理由を残す）", parents=[common])
    s.add_argument("reason")
    s.add_argument("--id")
    s.set_defaults(func=cmd_reject)

    s = sub.add_parser("ready", help="保留の枝を再開OKにする", parents=[common])
    s.add_argument("id")
    s.set_defaults(func=cmd_ready)

    s = sub.add_parser("pin", help="Next で最優先にする", parents=[common])
    s.add_argument("id")
    s.add_argument("--off", action="store_true")
    s.set_defaults(func=cmd_pin)

    s = sub.add_parser("resume", help="枝に戻る（復帰パックを出力）", parents=[common])
    s.add_argument("id", nargs="?")
    s.set_defaults(func=cmd_resume)

    for name, fn, hlp in (("next", cmd_next, "次にやる枝"), ("status", cmd_status, "NOW/NEXT/未完了"),
                          ("tree", cmd_tree, "ツリー表示")):
        s = sub.add_parser(name, help=hlp, parents=[common])
        s.set_defaults(func=fn)

    s = sub.add_parser("report", aliases=["wrap-up"], help="実施レポート", parents=[common])
    s.add_argument("--out")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("export", help="書き出し", parents=[common])
    s.add_argument("--format", choices=["json", "mermaid", "tree"], default="json")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("show", help="ノードのJSON", parents=[common])
    s.add_argument("id", nargs="?")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("view", help="ナビ画面をブラウザで開く", parents=[common])
    s.add_argument("--port", type=int, default=0)
    s.add_argument("--no-browser", action="store_true")
    s.set_defaults(func=cmd_view)

    s = sub.add_parser("install", help="スキルを ~/.claude/skills/edaori にコピー")
    s.add_argument("--dest")
    s.set_defaults(func=cmd_install)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
        return 0
    except EdaoriError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
