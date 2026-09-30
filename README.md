# Edaori（枝折）

**AIとの会話に、帰り道を。** — AIチャットの分岐ナビ

*A branch navigator for AI chats: mark every fork in the conversation, come back with the context intact, and always know what's next.*

AIと話していると、選択肢や並行して考えることが次々に出てきます。1つを進めているうちに分岐点を忘れ、ほかの話が中途半端なまま終わってしまう。Edaori はこの問題を解決するツールです。会話を「枝分かれする作業ツリー」として記録し、次の4つをいつでも見えるようにします。

1. **全体を見渡せる**：どこで枝分かれし、どの枝がまだ続いているか
2. **分岐点に戻れる**：戻った瞬間に、その時点の文脈をAIに渡し直せる
3. **未完了が見える**：保留したまま放置している枝を忘れない
4. **次にやることが分かる**：1つ片付くと「次はこの枝」と提案される

> **名前の由来**：「栞（しおり）」の語源は「枝折り（しおり）」です。昔の人は山道で木の枝を折って目印にし、帰り道が分かるようにしました。Edaori は会話の分岐点に同じような目印を残します。

```
 会話 ──◆fork──▶ 1つ選んで進む ──▶ done（結論1行）/ park（止めた位置）
        │                                   │
        └ スナップショット保存               ▼
          （分岐時点の記憶）          NEXT: 次の枝を提案
                                            │
 resume ◀───────────────────────────────────┘
   └▶ 復帰パック = ①当時の記憶 + ②途中経過 + ③その後分かったこと
```

## 対応ツール

| ツール | 使い方 |
| --- | --- |
| Claude Code | Agent Skill（`~/.claude/skills/edaori`） |
| GitHub Copilot（VS Code・Agentモード） | Agent Skill（`~/.claude/skills` または `.agents/skills`） |
| Cursor | Agent Skill（`~/.claude/skills` または `.agents/skills` / `.cursor/skills`） |
| Claude（Coworkなど、ファイルに書ける環境） | 同じスキル |

スキルの中身は日本語の手順書（`SKILL.md`）と、`branches.json` を更新する小さなCLI（`edaori.py`）です。CLIはPython 3.9以上の標準ライブラリだけで動きます。

## インストール

```bash
git clone https://github.com/kozuka-233/Edaori.git
cd Edaori
python skills/edaori/scripts/edaori.py install   # ~/.claude/skills/edaori にコピー
```

`~/.claude/skills/` は Claude Code・VS Code Copilot・Cursor のどれからも読み込まれます。特定のリポジトリだけで使いたい場合は、`skills/edaori` をそのリポジトリの `.agents/skills/edaori` にコピーしてください。

> Windowsでは `python`、macOS/Linuxでは `python3` を使ってください。

## 使い方

### AIに任せる

インストール後はチャットで `/edaori` を呼ぶか、そのまま話を進めてください。選択肢が出るとAIが `fork` を、枝が片付くと `done` を呼んで記録します。確実に記録したいときは、次のように明示してください。

- 「ここで分岐して記録して」→ `fork`
- 「これは保留」→ `park`
- 「Bの話に戻りたい」→ `resume`
- 「次は何だっけ？」→ `next`
- 「今日のまとめ」→ `wrap-up`（実施レポート）

### 自分で操作する

```bash
E="python ~/.claude/skills/edaori/scripts/edaori.py"
$E init "副業の方向性を決める"
$E fork "何を売る？" --options "A. 動画" "B. テンプレ" "C. 受託" \
      --snapshot-file snapshot.txt --pick 1 --tool claude
$E note "週3本なら回せる"
$E done "まず週3本で開始"            # → NEXT: B. テンプレ（分岐点「何を売る？」の残り）
$E resume 4                          # 復帰パックを出力して B に戻る
$E park "Aの反応待ち" --stopped-at "案3つまで。次は価格" --until "Aの結論"
$E tree
$E wrap-up
```

```
▶ 副業の方向性を決める  [n_001]
└─◆ 何を売る？  [n_002]  ← HEAD
   ├─✓ A. 動画  [n_003] → まず週3本で開始
   ├─‖ B. テンプレ  [n_004]  (保留: Aの反応待ち / 再開: Aの結論)
   └─○ C. 受託  [n_005]  ← NEXT
```

記号：○ 未着手　▶ 進行中　‖ 保留　✓ 完了　× 却下　◆ 分岐点

### ナビ画面

```bash
$E view        # ブラウザで開き、2秒ごとに自動更新
```

画面には NOW / NEXT / グラフ / 未完了 / 復帰カードが並びます。復帰カードでは再開プロンプトをコピーできます。`skills/edaori/viewer/index.html` を直接開き、`branches.json` を読み込んで使うこともできます。

## コマンド一覧

| コマンド | 内容 |
| --- | --- |
| `init "テーマ" [--where repo\|home]` | テーマを作る（git の中なら既定で repo） |
| `fork "問い" --options A B ... [--snapshot-file F] [--pick N]` | 分岐を記録する（スナップショットを強く推奨） |
| `note "メモ"` | 進捗メモを追加する |
| `park "理由" --stopped-at "止めた位置" [--until "再開条件"]` | 保留にする |
| `done "結論"` / `reject "理由"` | 完了にする / 却下する（HEAD は分岐点に戻る） |
| `resume [id]` | 枝に戻り、復帰パックを出力する |
| `next` / `status` / `tree` | 次にやること / 全体の状況 / ツリー表示 |
| `ready <id>` / `pin <id>` | 保留を再開OKにする / Next で最優先にする |
| `wrap-up [--out F]` | 実施レポートを出力する |
| `export --format json\|mermaid\|tree` | 書き出す |
| `themes` / `use <slug>` | テーマの一覧 / 切り替え |
| `view` / `install` | ナビ画面を開く / スキルをインストールする |

## データの保存先

- gitリポジトリ内：`<repo>/.edaori/themes/<テーマ>/branches.json`
  - `.git/info/exclude` に自動で登録されるため、コミットされません。`.gitignore` は変更しません
- それ以外：`~/.edaori/themes/<テーマ>/branches.json`
  - 保存先は `EDAORI_HOME` で変更できます
- 全テーマの台帳：`~/.edaori/index.json`

Edaori は個人用のツールで、チームで共有する前提ではありません。

## 開発

```bash
python -m unittest discover -s tests -v
```

設計の詳細は [docs/design.md](docs/design.md) を参照してください。

## ロードマップ

- [x] CLI・スキル・ナビ画面（MVP）
- [ ] ナビ画面でマインドマップ表示に切り替え
- [ ] wrap-up での記録漏れ候補の自動照合
- [ ] ファイルに書けない Claude アプリ向けの MCP サーバー
- [ ] 画面オーバーレイ型アシスタントとの統合

## License

MIT
