# rw 言語 概要

## 何の言語か

rw は **Python の書き味で書ける、非同期ファーストの静的型コンパイル言語** である。
LLVM をバックエンドに用い、macOS arm64 / Linux x86_64 のネイティブ実行ファイルを
生成する。

## 設計の柱

1. **非同期が中心**: `Future[T]` は型システムの一級市民。`spawn`/`await` は予約語。
2. **Python 似の見た目**: インデントベース、`def`、`elif`、`and/or/not`、`true/false`。
3. **静的型・型注釈必須**: 引数・戻り値・ローカル変数すべて型注釈を書く。MVP は
   型推論なし。
4. **薄いランタイム + LLVM**: Cで書いた `librw.a` をリンクし、コア機能(スレッド、
   Future、print)を提供。
5. **学習・実験フレンドリー**: コンパイラは Python 製。`rwc emit-ir` / `emit-ast`
   で内部を覗ける。

## ターゲット

- macOS arm64
- Linux x86_64

Windows と組み込みは MVP では対象外。

## ツールチェーン

| ツール | 用途 |
|---|---|
| Python 3.11 | コンパイラ本体 |
| llvmlite | LLVM IR 構築 |
| clang | リンカ呼び出し + librw.a とのリンク |
| make + cc | librw.a のビルド |

## パイプライン

```
.rw → Lexer → Parser → Sema → IRGen → Driver → 実行ファイル
                                          ↓
                                     librw.a (C)
```

## CLI

```
rwc build foo.rw [-o foo]
rwc run   foo.rw
rwc emit-ir  foo.rw
rwc emit-ast foo.rw
```

## MVP のゴール

`examples/` 配下の 7 本(hello, arith, fib, while_count, spawn_basic, spawn_many,
spawn_string)が **macOS arm64 と Linux x86_64 の両方で緑** になること。

## やらないこと(将来拡張)

- list / dict / for / class / import
- 型推論
- GC(現状はリーク許容、Future と malloc/free ペアのみ)
- 文字列連結・スライス
- Python 直接呼び出し(将来は `extern "c"` + プロセス分離で対応)
- 例外 / Result 型
- 複数エラー回復(MVP は最初のエラーで停止)
