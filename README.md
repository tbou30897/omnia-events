# omnia-events

関東圏の同人即売会・コーヒーイベントの一覧を [`events.md`](events.md) に保つリポジトリ。

- 毎朝、Claude Code のクラウドルーチンが Web を調べて `events.md` を更新する。
  手順と書式は [`CLAUDE.md`](CLAUDE.md) にある。
- ルーチンが `claude/*` ブランチへ push した場合は、
  [`merge-events.yml`](.github/workflows/merge-events.yml) が
  「変更が `events.md` だけであること」「書式が正しいこと」を確かめてから `main` へ取り込む。
- 書式の検証は `python scripts/events.py check events.md`。

掲載内容は自動収集のため誤りを含みうる。参加・申込の前に必ず公式サイトを確認すること。
