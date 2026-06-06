# Changelog

## [v0.0.5](https://github.com/rwlang-org/rw/compare/v0.0.4...v0.0.5) - 2026-06-06

- examples: TCP チャットサーバのサンプル追加 by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/113
- 言語: 三項演算子 / 条件式 (x if cond else y) を実装 by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/114
- docs: 条件式(三項演算子)の言語仕様を追加 by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/115
- stdlib: ファイル I/O と fd 汎用 read/write/close (#33) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/116
- runtime: ファイル I/O を非同期化（スレッドプールにオフロード） by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/117
- lang: 数値リテラル拡張（16進・8進・2進・指数・アンダースコア区切り） by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/118
- lang: ビット演算子 (& | ^ ~ << >>) を実装 by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/119
- lang: break / continue 文を実装（while ループ対象） by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/120
- lang: assert 文を実装 by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/121
- lang: 型エイリアス (type Foo = ...) を実装 by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/122
- stdlib: math 組み込み関数を実装（LLVM intrinsic） by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/123

## [v0.0.4](https://github.com/rwlang-org/rw/compare/v0.0.3...v0.0.4) - 2026-05-23
- rwc + runtime: netpoller + 最小 TCP API (echo server) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/97

## [v0.0.3](https://github.com/rwlang-org/rw/compare/v0.0.2...v0.0.3) - 2026-05-23
- rwc: 文字列の最小拡張 (len, ==, +) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/91
- rwc: Bytes 型 (immutable, echo 最小セット) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/92
- rwc: List[int] 型 (immutable, モノモーフ, echo 最小セット) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/93
- rwc: Option[int] 型と最小 match 構文 (Python 3.10 風) by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/94
- rwc: Result[int, int] 型と match の Ok/Err アーム拡張 by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/95

## [v0.0.2](https://github.com/rwlang-org/rw/compare/v0.0.1...v0.0.2) - 2026-05-20
- ランタイムを Go 風 M:N スケジューラに進化させる by @ryuichi1208 in https://github.com/rwlang-org/rw/pull/90

## [v0.0.1](https://github.com/ryuichi1208/rw/commits/v0.0.1) - 2026-05-17
- rw v0.0.1: MVP compiler with fiber-backed runtime by @ryuichi1208 in https://github.com/ryuichi1208/rw/pull/1
- ci: automate releases with tagpr by @ryuichi1208 in https://github.com/ryuichi1208/rw/pull/2
- fix(ci): pass GITHUB_TOKEN to tagpr by @ryuichi1208 in https://github.com/ryuichi1208/rw/pull/3
