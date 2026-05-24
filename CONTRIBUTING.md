# Contributing to rw

rw への貢献を検討いただきありがとうございます。本ドキュメントは、開発環境のセットアップから PR を出すまでの流れと、コーディング規約をまとめたものです。

## 1. このプロジェクトについて

rw は Python-flavored, async-first, statically-typed compiled language です。

- **フロントエンド**: Python 実装 (lexer / parser / sema / irgen) 。コードは `rwc/` 配下。
- **ランタイム**: C 実装。コードは `runtime/` 配下。
- **言語仕様の一次情報**: `docs/specs/` 以下の Markdown ドキュメントを参照してください。

仕様や設計に疑問が出たら、まず `docs/specs/` を確認することをおすすめします。

## 2. 開発環境のセットアップ

### 必要なもの

- Python 3.11 以上
- [uv](https://github.com/astral-sh/uv)
- clang (C11 をサポートするもの)
- make

### 手順

```sh
git clone https://github.com/ryuichi1208/rw
cd rw
uv sync --extra dev
make -C runtime
```

`uv sync --extra dev` で開発用の Python 依存をすべて取得します。`make -C runtime` で C ランタイムをビルドします。

## 3. テスト

### Python 側

全テストを実行する場合:

```sh
uv run pytest -v
```

単一のテストファイルだけを実行する場合:

```sh
uv run pytest tests/test_irgen.py -v
```

### C ランタイム

```sh
make -C runtime test
```

### E2E サンプル

`examples/*.rw` を `rwc` でコンパイルし、実行結果を `*.rw.expected` と比較する形で E2E テストが構成されています。新しい言語機能を追加した際は、対応するサンプルと expected ファイルを追加してください。

## 4. lint / format (pre-commit)

本プロジェクトは [pre-commit](https://pre-commit.com/) を使ってフォーマットと lint を強制しています。

初回セットアップ:

```sh
pip install pre-commit  # または uv tool install pre-commit
pre-commit install
```

全ファイルに対して実行:

```sh
pre-commit run --all-files
```

使用しているツール:

- **ruff** (lint + format)
- **black**
- **mypy**

設定は `pyproject.toml` および `.pre-commit-config.yaml` にあります。mypy は段階的に厳格化していく方針なので、既存コードでエラーが残っていても、新規に書くコードからは型を素直に通せる形で書いてください。

## 5. PR フロー

1. feature ブランチを切ります。命名例:
   - `feat/issue-NN-short-desc`
   - `fix/issue-NN-short-desc`
   - `docs/issue-NN-short-desc`
2. 変更は小さく分けてコミットしてください。
3. PR テンプレートに従って記述し、関連 issue は `Closes #N` の形式で本文に含めます。
4. CI が通ったらレビュー依頼を出してください。
5. main への取り込みは **squash merge** を基本とします。
6. リリースは [tagpr](https://github.com/Songmu/tagpr) が main への push をトリガに自動でリリース PR を生成します。リリース時は、その PR をマージするだけで OK です。

## 6. コミットメッセージ規則

Conventional Commits 風 (ゆるめ) を採用しています。

```
<type>: <subject>

<optional body>
```

### type の例

- `feat`: 新機能
- `fix`: バグ修正
- `docs`: ドキュメントのみの変更
- `chore`: 雑多な変更 (ビルド設定など)
- `ci`: CI 設定の変更
- `refactor`: 振る舞いを変えないリファクタ
- `test`: テストの追加・修正
- `perf`: パフォーマンス改善

### subject のルール

- 50 字以内
- 命令形 (例: `add`, `fix`, `update` で始める)
- 末尾にピリオドを付けない

本文を書く場合は 72 字目安で wrap してください。

## 7. コーディング規約

### Python (`rwc/`)

- line length は 120 文字
- ruff + black で format。pre-commit で自動整形されます
- 型ヒントを推奨。新規モジュールは mypy で素直に通る形で書いてください
- import 順は ruff の `I` ルール (isort 互換) に従います

### C (`runtime/`)

- C11 を前提
- インデントは 4-space
- ファイル単位で既存スタイルに揃えてください
- 公開 API は `runtime.h` に集約します
- ファイバー / スケジューラ周りの実装は、`docs/specs/05-fibers.md` および `docs/specs/06-scheduler-mn.md` を参照してください

## 8. ドキュメント

- 言語仕様に関わる変更は `docs/specs/` 以下に新規 `.md` を追加するか、既存ファイルを更新してください
- 大きな変更を入れる前には、`docs/plans/YYYY-MM-DD-<topic>.md` に設計や計画を書き、それを起点に PR を出すと議論が進めやすいです

## 9. issue を選ぶ

- `good first issue` ラベルが付いた issue は入門に向いています
- 着手する前に issue にコメントしてアサインを依頼してください。二重作業を避けるためです

---

ご質問や提案があれば、issue または PR で気軽に声をかけてください。
