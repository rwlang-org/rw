# rw 標準ライブラリリファレンス

rw の標準ライブラリは `stdlib/` に置かれた rw のソースファイルです。C ランタイムに組み込まれた builtin（`read`、`str_find`、`fs_kind` など）とは別物で、ユーザーのプログラムと同じ言語で書かれ、同じようにコンパイルされます。この文書は各モジュールの公開 API・エラー時の挙動・使用例をまとめたものです。記述は `stdlib/*.rw` のコードに基づいています。

各モジュールの設計意図は `docs/specs/` の仕様書（54, 58, 59, 60, 61, 62, 66）にあります。

## 目次

- [モジュール一覧](#モジュール一覧)
- [モジュールの探索パス](#モジュールの探索パス)
- [モジュール共通の約束事](#モジュール共通の約束事)
- [bufio: バッファ付き読み込み](#bufio-バッファ付き読み込み)
- [fs: ファイルとディレクトリ](#fs-ファイルとディレクトリ)
- [json: JSON の値・パース・シリアライズ](#json-json-の値パースシリアライズ)
- [log: レベル付きロギング](#log-レベル付きロギング)
- [path: POSIX パス文字列](#path-posix-パス文字列)
- [process: 子プロセス](#process-子プロセス)
- [template: Mustache 風テンプレート](#template-mustache-風テンプレート)
- [url: パーセントエンコーディングとクエリ文字列](#url-パーセントエンコーディングとクエリ文字列)
- [ライブラリの例: Web フレームワーク](#ライブラリの例-web-フレームワーク)
- [標準ライブラリにモジュールを追加する](#標準ライブラリにモジュールを追加する)

## モジュール一覧

| モジュール | import 文 | 用途 | 仕様 |
|---|---|---|---|
| [`bufio`](#bufio-バッファ付き読み込み) | `import bufio` | 任意のハンドル（ソケット、パイプ、ファイル）からの行単位・区切り単位・バイト数指定の読み込み | 54 |
| [`fs`](#fs-ファイルとディレクトリ) | `import fs` | ファイルの読み書き、ディレクトリ操作、メタデータ取得 | 59 |
| [`json`](#json-json-の値パースシリアライズ) | `import json` | JSON の値モデル、パーサ、シリアライザ | 58 |
| [`log`](#log-レベル付きロギング) | `import log` | レベル・プレフィックス・タイムスタンプ付きの 1 行ログ | 60 |
| [`path`](#path-posix-パス文字列) | `import path` | POSIX パスの文字列操作（ファイルシステムには触れない） | 59 |
| [`process`](#process-子プロセス) | `import process` | 子プロセスの起動、入出力、終了待ち、シグナル送信 | 61 |
| [`template`](#template-mustache-風テンプレート) | `import template` | `json.Value` を入力とする Mustache 風テンプレート | 66 |
| [`url`](#url-パーセントエンコーディングとクエリ文字列) | `import url` | パーセントエンコード／デコード、クエリ文字列の解析 | 62 |

モジュール間の依存は `template` → `json` の 1 本だけです。

## モジュールの探索パス

`import foo` は次の順に `foo.rw` を探します（`rwc/loader.py`）。

1. **エントリファイルと同じディレクトリ**（`rwc run app.rw` なら `app.rw` のあるディレクトリ）
2. **標準ライブラリのディレクトリ**。既定ではリポジトリ直下の `stdlib/`（`rwc` パッケージの隣、`runtime/` と同じ階層）です。環境変数 `RW_STDLIB` を設定すると、そのディレクトリが `stdlib/` の**代わりに**使われます（追加ではなく置き換えです）。

どちらにも見つからなければ、両方のパスを示すエラーになります。

```text
error: cannot find module 'nosuchmod' (looked for ./nosuchmod.rw and /path/to/rw/stdlib/nosuchmod.rw)
```

import の 3 つの書き方はすべて標準ライブラリにも使えます（仕様 17）。クラス名も import できます。

```rw
import json                    # json.parse(...), json.Value
import json as js              # js.parse(...), js.Value
from json import Value, parse  # parse(...), Value
```

### 注意点

- **ローカルのモジュールが優先されます。** エントリファイルの隣に `url.rw` を置くと、標準ライブラリの `url` は読み込まれません。
- **標準ライブラリのモジュールが import するのは標準ライブラリだけです。** `template` の `import json` は、エントリファイルの隣に `json.rw` があっても `stdlib/json.rw` を読みます。ただし、プログラムが自作の `json` と、`json` に依存する標準ライブラリのモジュール（`template`）を両方使うと、モジュール名が衝突して次のエラーになります。

  ```text
  error: module 'json' is both .../stdlib/json.rw and ./json.rw: a local module shadows a standard library module used by another import; rename the local one
  ```

  標準ライブラリと同じ名前（`bufio` `fs` `json` `log` `path` `process` `template` `url`）を自作モジュールに付けるのは避けてください。
- モジュール名の空間は 1 つだけです。同じ名前のモジュールが複数読み込まれることはありません。
- 標準ライブラリのモジュールは通常の rw モジュールです。プログラムと一緒にコンパイルされ、互いに import でき、`main` を定義してはいけません。

## モジュール共通の約束事

### 「無いかもしれない」は `Option`

値が存在しない場合や操作が失敗した場合に `None` を返す関数は `Option[T]` を返します（`fs.read_file`、`fs.stat`、`json.Value.get`、`url.get`、`process.run` など）。`match` で両方の場合を扱ってください。

```rw
match fs.read_file("config.json"):
    case Some(text):
        print(text)
    case None:
        print(error_message(last_error()))
```

### パースエラーは `Result` と位置付きメッセージ

入力テキストを解釈する関数（`json.parse`、`template.compile`、`template.render`）は `Result[T, string]` を返します。`Err` のメッセージは「何が問題か」と「どこか」を `"<内容> at line <行>, column <列>"` の形で含みます。行と列は 1 始まりで、列は**バイト単位**で数えます（UTF-8 の多バイト文字は複数列になります）。

```text
expected ',' or '}' at line 1, column 14
unclosed section 'items' at line 2, column 3
```

### OS エラーは `last_error()`

ファイル・プロセスなど OS を経由する操作が失敗したときは、戻り値で失敗を示し（`None`、`false`、空リスト、`-1`）、原因は `last_error()` で取得します（仕様 51）。`last_error()` は現在のファイバーで最後に呼んだ I/O builtin の結果で、成功時は 0、失敗時は errno 値です。

| builtin | 戻り値 |
|---|---|
| `last_error() -> int` | 0 または errno |
| `error_name(e: int) -> string` | `"ENOENT"`、`"EEXIST"` などの記号名。0 なら `""` |
| `error_message(e: int) -> string` | 人間向けの説明（`strerror`）。0 なら `""` |

`last_error()` は次の I/O builtin を呼ぶと上書きされます。失敗を検出したら、他の I/O を挟まずにすぐ読んでください。

### レコードは参照

`class` で定義したレコード（`bufio.Reader`、`json.Value`、`log.Logger`、`process.Process` など）は参照として扱われます。関数に渡したりメソッドを呼んだりすると、同じオブジェクトが変更されます。`json.Value.set` が呼び出し元の値を書き換えるのも、`log.Logger` のフィールドを代入で変えられるのもこのためです。

### 公開範囲

rw には private の仕組みがないため、モジュール内のすべての関数・クラスが import できます。この文書では、利用者向けの API と内部用のヘルパーを区別して記載します。内部用のヘルパーは予告なく変わる可能性があります。

---

## bufio: バッファ付き読み込み

```rw
import bufio
```

`read(fd, max)` は「その時点で届いている分」を返すだけなので、行や区切り文字の境界は呼び出し側で探す必要があり、読みすぎたバイトも保持しておかなければなりません。`bufio.Reader` はこの処理を引き受けます。ソケット・パイプ・ファイルなど、どのハンドルにも使えます。

1 回の呼び出しで返さなかったバイトはリーダー内に残り、次の呼び出しで返されます。そのため、同じハンドルに対して各メソッドを自由に混ぜて使えます。ただし、リーダーを作った fd に対して `read` を直接呼ぶと、バッファ内のデータと順序が崩れるので避けてください。

### `reader`

```rw
def reader(fd: int) -> Reader
```

ハンドル `fd` を包むリーダーを返します。fd を閉じるのは呼び出し側の責任です。

### `class Reader`

| フィールド | 型 | 意味 |
|---|---|---|
| `fd` | `int` | 読み込み元のハンドル |
| `buf` | `string` | fd から読んだが、まだ返していないバイト |
| `eof` | `bool` | fd が EOF またはエラーを返したら `true` |
| `err` | `int` | そのときの `last_error()`。0 は正常な EOF |

エラーとタイムアウト（仕様 51, 53）も EOF と同じく読み込みを終わらせます。`r.eof` が `true` になり、`r.err` に原因（`ETIMEDOUT`、`ECONNRESET` など）が入ります。いったん `eof` になったリーダーは fd を再度読みません。

| メソッド | 戻り値 |
|---|---|
| `read_line(self) -> Option[string]` | 次の行。末尾の `"\n"` または `"\r\n"` は取り除かれます。改行で終わらない最終行もそのまま返します。EOF で `None` |
| `read_until(self, delim: string, limit: int = 0) -> string` | 最初の `delim` まで（`delim` を含む）。下記参照 |
| `read_exact(self, n: int) -> Bytes` | ちょうど `n` バイト。fd が先に終われば、それより短くなります |
| `read_some(self, max: int) -> Bytes` | バッファにあるデータ（最大 `max` バイト）。バッファが空なら fd を 1 回だけ読みます。EOF では空の `Bytes` |
| `buffered(self) -> int` | バッファにあってまだ返していないバイト数 |

#### `read_until` の詳細

- `delim` が見つかれば、先頭から `delim` の末尾までを返します。
- EOF に達したら、残っているデータを（`delim` なしで）すべて返します。何も残っていなければ `""` です。
- `limit > 0` の場合、`delim` が見つからないまま `limit` バイト以上たまった時点で、先頭 `limit` バイトを（`delim` なしで）返します。`delim` が先頭 `limit` バイトに収まらない位置にある場合も同じです。相手が改行を送らずにデータを送り続けても、バッファが際限なく大きくならないようにするためのものです。
- 戻り値が `delim` で終わっているかどうかで、「区切りが見つかった」「上限に達した、または EOF」を区別できます。

`read_line` は `read_until("\n")` を上限なしで呼びます。信頼できない相手から行を読むときは `read_until("\n", limit)` を使ってください。

#### 内部用ヘルパー

`fill(self) -> bool`（fd から最大 4096 バイトを読み足す）と `take(self, n: int) -> string`（バッファの先頭 `n` バイトを取り出す）は内部用です。

### 例: ヘッダと本文の読み分け

ヘッダ行を空行まで読み、`Content-Length` バイトの本文を読みます。HTTP リクエストの読み込みと同じ形です。

```rw
import bufio
import fs

def main() -> int:
    fs.write_file("request.txt", "POST /echo HTTP/1.1\r\nContent-Length: 5\r\n\r\nhelloEXTRA")
    fd = open("request.txt", "r")
    if fd < 0:
        print(error_message(last_error()))
        return 1
    r = bufio.reader(fd)
    length = 0
    while true:
        line = r.read_until("\n", 8192)
        if not str_ends_with(line, "\n"):
            print("head too long or truncated")
            close(fd)
            return 1
        line = str_strip(line)
        if line == "":
            break
        print("header: " + line)
        if str_starts_with(line, "Content-Length:"):
            match str_to_int(str_strip(str_slice(line, 15, len(line)))):
                case Some(n):
                    length = n
                case None:
                    length = 0
    body = r.read_exact(length)
    print("body: " + str_from_bytes(body))
    print(f"left in buffer: {r.buffered()}")
    close(fd)
    return 0
```

出力:

```text
header: POST /echo HTTP/1.1
header: Content-Length: 5
body: hello
left in buffer: 5
```

### 例: 行ごとに読む

```rw
import bufio

def main() -> int:
    fd = open("request.txt", "r")
    r = bufio.reader(fd)
    n = 0
    while true:
        match r.read_line():
            case Some(line):
                n += 1
                print(f"{n}: [{line}]")
            case None:
                break
    if r.err != 0:
        print("read error: " + error_name(r.err))
    close(fd)
    return 0
```

出力（前の例で作ったファイルを読んだ場合）:

```text
1: [POST /echo HTTP/1.1]
2: [Content-Length: 5]
3: []
4: [helloEXTRA]
```

空行は `Some("")` として返り、`None` と区別されます。TCP の行指向サーバーの例は `examples/tcp_chat.rw` にあります。

---

## fs: ファイルとディレクトリ

```rw
import fs
```

`fs_*` builtin と `open` / `read` / `write_all` / `close` を薄く包んだモジュールです。失敗時は `None`・`false`・空リストを返し、`last_error()` に原因を設定します（例外は下記「注意」参照）。ランタイムは各システムコールを aio プールで実行するので（仕様 50）、遅いファイルシステムで待っている間も他のファイバーは動き続けます。NUL バイトを含むパスは `EINVAL` になります。

### `class Stat`

| フィールド | 型 | 意味 |
|---|---|---|
| `kind` | `string` | `"file"`、`"dir"`、`"other"` のいずれか |
| `size` | `int` | バイト数 |
| `mtime_ms` | `int` | 最終更新時刻（Unix 時間、ミリ秒） |

### 関数

| 関数 | 戻り値 | 失敗時 |
|---|---|---|
| `stat(p: string) -> Option[Stat]` | `p` のメタデータ。シンボリックリンクは辿ります | `None` |
| `exists(p: string) -> bool` | `p` が存在するか | `false` |
| `is_dir(p: string) -> bool` | `p` がディレクトリか | `false` |
| `is_file(p: string) -> bool` | `p` が通常ファイルか | `false` |
| `read_dir(p: string) -> List[string]` | ディレクトリ内のエントリ名。バイト順でソート済み、`.` と `..` は含みません | 空リスト |
| `rename(src: string, dst: string) -> bool` | `src` を `dst` に名前変更・移動 | `false` |
| `remove(p: string) -> bool` | ファイルまたは**空の**ディレクトリを削除 | `false`（空でないディレクトリは `ENOTEMPTY`） |
| `mkdir(p: string) -> bool` | ディレクトリを 1 つ作成（モード 0755） | `false`（既存なら `EEXIST`） |
| `mkdir_all(p: string) -> bool` | `mkdir -p` 相当。足りない親ディレクトリも作ります。`p` がすでにディレクトリなら `true` | `false` |
| `read_file(p: string) -> Option[string]` | ファイル全体の内容 | `None` |
| `write_file(p: string, data: string) -> bool` | ファイルの内容を `data` で置き換えます（なければ作成） | `false` |

補足:

- `exists` / `is_dir` / `is_file` は失敗と「存在しない」を区別しません。理由が必要なら `stat` の後に `last_error()` を見てください。
- `read_dir` は失敗時に空リストを返すので、空のディレクトリと区別するには `last_error()` を確認します。
- `mkdir_all` はパスを `/` で区切って先頭から 1 段ずつ作成します。途中に通常ファイルがあれば `false` です。別のファイバーやプロセスが同時に同じディレクトリを作っても失敗しません。
- `read_file` は 64 KiB ずつ読んで連結します。`write_file` は `open(p, "w")` で切り詰めてから `write_all` で書きます。

`read_file` / `write_file` は、読み書きに失敗した後の `close` で `last_error()` が上書きされないよう、`set_last_error` で原因を戻してから返ります。そのため、`p` がディレクトリで読み込みが `EISDIR` になる場合も `last_error()` で原因がわかります。

### 例

```rw
import fs
import path

def main() -> int:
    root = "demo/site"
    if not fs.mkdir_all(path.join(root, "posts")):
        print("mkdir_all: " + error_name(last_error()))
        return 1
    fs.write_file(path.join(root, "posts/b.md"), "# B\n")
    fs.write_file(path.join(root, "posts/a.md"), "# A\n")
    for name in fs.read_dir(path.join(root, "posts")):
        p = path.join(root, "posts/" + name)
        match fs.stat(p):
            case Some(st):
                print(f"{name} {st.kind} {st.size} bytes")
            case None:
                print(name + ": " + error_name(last_error()))
    match fs.read_file(path.join(root, "posts/a.md")):
        case Some(text):
            print("a.md = " + str_strip(text))
        case None:
            print("read failed: " + error_name(last_error()))
    match fs.read_file(path.join(root, "missing.md")):
        case Some(text):
            print(text)
        case None:
            print("missing.md: " + error_name(last_error()))   # ENOENT
    print(fs.remove(path.join(root, "posts")))                # false: not empty
    print(error_name(last_error()))                            # ENOTEMPTY
    print(fs.rename(path.join(root, "posts/b.md"), path.join(root, "b.md")))
    print(fs.exists(path.join(root, "b.md")))
    # Clean up: remove works on files and empty directories only.
    fs.remove(path.join(root, "b.md"))
    fs.remove(path.join(root, "posts/a.md"))
    fs.remove(path.join(root, "posts"))
    fs.remove(root)
    fs.remove("demo")
    print(fs.exists("demo"))
    return 0
```

出力:

```text
a.md file 4 bytes
b.md file 4 bytes
a.md = # A
missing.md: ENOENT
false
ENOTEMPTY
true
true
false
```

---

## json: JSON の値・パース・シリアライズ

```rw
import json
```

JSON の値を表す `json.Value`、RFC 8259 準拠のパーサ、コンパクトなシリアライザを提供します。すべて rw で書かれており、`byte_at`（バイト単位の走査）、`chr`（`\uXXXX` を UTF-8 に変換）、`str_to_int` / `str_to_float`（数値の変換）の各 builtin を使っています。

### 定数

| 定数 | 値 | 意味 |
|---|---|---|
| `MAX_DEPTH: int` | `512` | 配列・オブジェクトの入れ子の上限。これを超える入力はスタックを溢れさせずにエラーになります |

### `class Value`

| フィールド | 型 | 意味 |
|---|---|---|
| `kind` | `string` | `"null"`、`"bool"`、`"int"`、`"float"`、`"string"`、`"array"`、`"object"` のいずれか |
| `b` | `bool` | `kind == "bool"` のときの値 |
| `i` | `int` | `kind == "int"` のときの値 |
| `f` | `float` | `kind == "float"` のときの値 |
| `s` | `string` | `kind == "string"` のときの値 |
| `items` | `List[Value]` | 配列の要素、またはオブジェクトのメンバーの値 |
| `keys` | `List[string]` | オブジェクトのキー（`items` と同じ順） |

`kind` に対応するフィールドだけが意味を持ちます。オブジェクトはキーを挿入順に保持し、入力中の重複キーも書かれたとおりに残します。`Value` は参照なので、`set` と `push` は値そのものを書き換えます。

#### 参照

| メソッド | 戻り値 |
|---|---|
| `get(self, key: string) -> Option[Value]` | オブジェクトのメンバー `key`。重複キーがあれば最初のもの。見つからない場合とオブジェクト以外では `None` |
| `at(self, n: int) -> Option[Value]` | 配列の `n` 番目の要素（0 始まり）。範囲外、負の値、配列以外では `None` |
| `size(self) -> int` | 配列の要素数またはオブジェクトのメンバー数。スカラーでは 0 |
| `is_null(self) -> bool` | `kind == "null"` か |

#### 型付きの取り出し

| メソッド | 戻り値 |
|---|---|
| `as_str(self) -> Option[string]` | 文字列なら `Some`、それ以外は `None` |
| `as_int(self) -> Option[int]` | `"int"` なら `Some`。`"float"` は（`3.0` でも）`None` |
| `as_float(self) -> Option[float]` | 数値なら何でも（`"int"` も `float` に変換して）`Some` |
| `as_bool(self) -> Option[bool]` | 真偽値なら `Some` |
| `as_str_or(self, fallback: string) -> string` | 文字列ならその値、それ以外は `fallback` |
| `as_int_or(self, fallback: int) -> int` | `"int"` ならその値、それ以外は `fallback` |

#### 組み立て

| メソッド | 戻り値 |
|---|---|
| `set(self, key: string, v: Value) -> Value` | オブジェクトのメンバー `key` を設定します。既存のキーがあれば（最初の 1 つを）置き換え、なければ末尾に追加します。`self` を返すので連鎖できます |
| `push(self, v: Value) -> Value` | 配列の末尾に追加します。`self` を返します |
| `dump(self) -> string` | コンパクトな JSON テキスト（`json.dump(self)` と同じ） |

`set` と `push` は `kind` を確認しません。配列に `set` するとキーが捨てられて要素が追加され、オブジェクトに `push` すると `dump` に現れないメンバーが増えます。`set` はオブジェクトに、`push` は配列にだけ使ってください。

### コンストラクタ

| 関数 | 戻り値 |
|---|---|
| `null() -> Value` | `null` |
| `from_bool(b: bool) -> Value` | 真偽値 |
| `from_int(i: int) -> Value` | 整数 |
| `from_float(f: float) -> Value` | 浮動小数点数 |
| `from_str(s: string) -> Value` | 文字列 |
| `array(items: List[Value]) -> Value` | 配列。空の配列は `json.array([])` |
| `object() -> Value` | 空のオブジェクト |

### パース

```rw
def parse(text: string) -> Result[Value, string]
```

前後の空白を含めて、JSON 文書 1 つをパースします。

- `\uXXXX` を含むすべてのエスケープに対応します。サロゲートペアは 1 つのコードポイントにまとめます。対になっていないサロゲートは U+FFFD になります。
- 64 ビットに収まる整数（小数点・指数なし）は `"int"`、それ以外の数値（`1.0`、`1e3`、int64 を超える整数）は `"float"` になります。
- 文字列中の生の制御文字、末尾のカンマ、先頭のゼロ（`01`）、引用符のないキー、文書の後ろの余分なデータはすべてエラーです。
- 入れ子が `MAX_DEPTH` を超えるとエラーです。

エラーメッセージは最初に見つかった問題だけを `"<内容> at line L, column C"` の形で返します。

| メッセージ | 原因の例 |
|---|---|
| `unexpected end of input` | `""`、`"["` |
| `unexpected character` | `[1, 2,]`（`]` の位置）、`@` |
| `invalid literal` | `tru`、`nul` |
| `invalid number` | `-`、`1.`、`1e` |
| `unterminated string` | `"abc` |
| `control character in string` | 文字列中の生のタブや改行 |
| `invalid escape` / `invalid \u escape` | `"\q"`、`"\u12G4"` |
| `expected a string key` | `{a:1}` |
| `expected ':'` | `{"a" 1}` |
| `expected ',' or ']'` / `expected ',' or '}'` | `[1 2]`、`{"a":1 "b":2}` |
| `unexpected trailing characters` | `[1,2] x`、`01` |
| `nesting too deep` | 513 段以上の入れ子 |

### シリアライズ

| 関数 | 戻り値 |
|---|---|
| `dump(v: Value) -> string` | コンパクトな JSON テキスト（空白なし） |
| `quote(s: string) -> string` | `s` を JSON の文字列リテラルにしたもの（前後の `"` を含む） |

- `"`、`\`、`\n`、`\r`、`\t` はエスケープし、それ以外の制御文字は `\u00XX` で書きます。0x80 以上のバイトはそのまま出力するので、UTF-8 のテキストは読めるまま残ります。
- NaN と無限大は JSON で表せないため `null` になります。
- 浮動小数点数は `str(float)` の書式で出力し、小数部も指数部もないときは `.0` を付けます（`from_float(2.0)` は `2.0`、`-0.0` は `-0.0`）。そのため `dump` して `parse` し直しても `"float"` のままです。数値を読むときは `as_float` を使うと、`"int"` と `"float"` のどちらでも受け取れます。

#### 内部用ヘルパー

`make(kind: string) -> Value`、`hex4(n: int) -> string`、`class Parser` は内部用です。

### 例: 設定ファイルの読み込みとレスポンスの組み立て

```rw
import json

class Config:
    name: string
    port: int
    debug: bool
    tags: List[string]

# Member `key`, or json null when it is missing.
def field(doc: json.Value, key: string) -> json.Value:
    match doc.get(key):
        case Some(v):
            return v
        case None:
            return json.null()

def load_config(text: string) -> Result[Config, string]:
    match json.parse(text):
        case Ok(doc):
            if doc.kind != "object":
                return Err("config: expected an object")
            cfg = Config(field(doc, "name").as_str_or("app"), field(doc, "port").as_int_or(8080), false, [])
            d = field(doc, "debug")
            if not d.is_null():
                match d.as_bool():
                    case Some(b):
                        cfg.debug = b
                    case None:
                        return Err("config: debug must be a boolean")
            tags = field(doc, "tags")
            for k in range(0, tags.size()):
                match tags.at(k):
                    case Some(t):
                        cfg.tags = list_push(cfg.tags, t.as_str_or(""))
                    case None:
                        continue
            return Ok(cfg)
        case Err(msg):
            return Err("config: " + msg)

def main() -> int:
    match load_config("{\"name\": \"api\", \"port\": 9000, \"tags\": [\"a\", \"b\"]}"):
        case Ok(c):
            print(f"{c.name}:{c.port} debug={c.debug} tags={str_join(c.tags, ",")}")
        case Err(e):
            print(e)
    match load_config("{\"debug\": \"yes\"}"):
        case Ok(c):
            print(c.name)
        case Err(e):
            print(e)
    match load_config("{\"name\": \"api\",\n \"port\": 9000,\n}"):
        case Ok(c):
            print(c.name)
        case Err(e):
            print(e)

    # Building a document: set / push modify in place and return the value.
    res = json.object()
    res.set("ok", json.from_bool(true)).set("count", json.from_int(2))
    items = json.array([])
    items.push(json.from_str("say \"hi\"")).push(json.null())
    res.set("items", items)
    res.set("ratio", json.from_float(0.25))
    print(res.dump())
    return 0
```

出力:

```text
api:9000 debug=false tags=a,b
config: debug must be a boolean
config: expected a string key at line 3, column 1
{"ok":true,"count":2,"items":["say \"hi\"",null],"ratio":0.25}
```

`examples/json_basic.rw` にも、エスケープやサロゲートペアを含む例があります。

---

## log: レベル付きロギング

```rw
import log
```

1 レコードを 1 行で出力するロガーです。行の形式は次のとおりです。

```text
[<UTC タイムスタンプ> ]<LEVEL>[ <prefix>:] <メッセージ>
2026-10-13T01:02:03.456Z INFO api: listening addr=127.0.0.1:8080
```

1 レコードは 1 つの文字列として組み立てられ、ロガーの `Mutex` を保持したまま 1 回の `write_all` で書かれます。そのため、複数のファイバーから同時に書いても行が混ざりません。出力は `print` の stdio バッファを通らず、直接ハンドルに書かれます。プロセスが kill されてもレコードは失われませんが、同じストリームに `print` とロガーを混ぜると行の順序が入れ替わることがあります（stdout がパイプのとき、`print` の出力はバッファに溜まってから書かれるため）。

### 定数

| 定数 | 値 |
|---|---|
| `DEBUG: int` | 10 |
| `INFO: int` | 20 |
| `WARN: int` | 30 |
| `ERROR: int` | 40 |

### `class Logger`

| フィールド | 型 | 意味 |
|---|---|---|
| `fd` | `int` | 出力先のハンドル |
| `level` | `int` | これ未満のレベルのレコードは捨てられます |
| `prefix` | `string` | 空でなければ `"<prefix>:"` として出力します |
| `timestamps` | `bool` | 各行の先頭に UTC の ISO 8601 時刻を付けるか |
| `mu` | `Mutex` | 書き込みを直列化するミューテックス |

`fd`、`level`、`prefix`、`timestamps` は直接代入して変更できます。

| メソッド | 戻り値 |
|---|---|
| `debug(self, msg: string) -> void` / `info` / `warn` / `error` | 対応するレベルで 1 レコード出力 |
| `log(self, level: int, msg: string) -> void` | 任意のレベルで出力。表示名は `level` 以下で最も近い定義済みレベルになります（35 なら `WARN`、99 なら `ERROR`、10 未満は `DEBUG`） |
| `enabled(self, level: int) -> bool` | `level` のレコードが出力されるか。重いメッセージを組み立てる前の判定に使います |
| `set_level(self, name: string) -> bool` | しきい値を名前で設定します。`"debug"`、`"info"`、`"warn"` / `"warning"`、`"error"`（大文字小文字は区別しません）。それ以外の名前では何も変えずに `false` |

### 関数

| 関数 | 戻り値 |
|---|---|
| `new(fd: int) -> Logger` | ハンドル `fd`（1 = stdout、2 = stderr、その他任意）に書くロガー。レベル INFO、プレフィックスなし、タイムスタンプあり |
| `to_file(p: string) -> Option[Logger]` | ファイル `p` に追記するロガー（なければ作成）。開けなければ `None` で、`last_error()` に原因が入ります |
| `kv(key: string, value: string) -> string` | `" key=value"`（先頭に空白を含む）。値が空、または空白・`"`・`=`・制御文字を含む場合は、値を `"` で囲み、`\` と `"` をエスケープします。ログ行を機械的に分割できるようにするためのものです |
| `iso8601(ms: int) -> string` | Unix 時間（ミリ秒）を `YYYY-MM-DDTHH:MM:SS.mmmZ`（UTC）に変換します。負の値（1970 年以前）も扱えます |

ロガーにはファイルを閉じるメソッドがありません。`to_file` で作ったロガーが不要になったら `close(lg.fd)` を呼んでください。

#### 内部用ヘルパー

`level_of(name: string) -> int`（名前から数値。不明なら -1）、`level_name(level: int) -> string`、`pad(n: int, width: int) -> string` は内部用です。

### 例

```rw
import log

def main() -> int:
    lg = log.new(1)              # 1 = stdout, 2 = stderr, or any handle
    lg.prefix = "api"
    lg.timestamps = false        # keep this example's output stable
    lg.debug("not shown: the default level is INFO")
    lg.info("listening" + log.kv("addr", "127.0.0.1:8080"))
    lg.set_level("debug")
    lg.debug("cache" + log.kv("hits", str(42)) + log.kv("path", "/var/cache/my app"))
    lg.warn("slow request" + log.kv("ms", str(1530)) + log.kv("user", ""))
    lg.error("upstream failed" + log.kv("err", "connection refused"))
    return 0
```

出力:

```text
INFO api: listening addr=127.0.0.1:8080
DEBUG api: cache hits=42 path="/var/cache/my app"
WARN api: slow request ms=1530 user=""
ERROR api: upstream failed err="connection refused"
```

`iso8601` の例:

| 呼び出し | 結果 |
|---|---|
| `log.iso8601(0)` | `1970-01-01T00:00:00.000Z` |
| `log.iso8601(1700000000123)` | `2023-11-14T22:13:20.123Z` |
| `log.iso8601(-1)` | `1969-12-31T23:59:59.999Z` |
| `log.iso8601(951782400000)` | `2000-02-29T00:00:00.000Z` |

---

## path: POSIX パス文字列

```rw
import path
```

POSIX パスを文字列として操作する関数群です。ファイルシステムには一切アクセスしません。`..` は字句的に解決され、シンボリックリンクは辿りません。

| 関数 | 戻り値 |
|---|---|
| `is_abs(p: string) -> bool` | `p` が `/` で始まるか |
| `join(a: string, b: string) -> string` | `a` と `b` を `/` 1 つでつないだもの。`b` が絶対パスなら `b`（Python の `os.path.join` と同じ）。`b` が空なら `a`、`a` が空なら `b` |
| `split(p: string) -> tuple[string, string]` | 最後の `/` で分けた `(ディレクトリ, 最後の要素)`。ディレクトリ側の末尾 `/` は、ルートの場合を除き付きません。`/` を含まなければ `("", p)` |
| `basename(p: string) -> string` | `split(p)` の後半 |
| `dirname(p: string) -> string` | `split(p)` の前半 |
| `ext(p: string) -> string` | 最後の要素の拡張子をドット付きで（`"x.tar.gz"` → `".gz"`）。なければ `""`。先頭のドット（`.bashrc`）は拡張子とみなしません |
| `normalize(p: string) -> string` | 等価な最短のパス。下記参照 |
| `join_under(root: string, rel: string) -> Option[string]` | `rel` を `root` の下で解決したパス。`root` の外に出る場合は `None`。下記参照 |

### `normalize`

- `.` と空の要素（`//`）を取り除きます。
- `..` は直前の要素と打ち消し合います。
- 相対パスでは、打ち消せない先頭の `..` を残します（`"../../a/.."` → `"../.."`）。
- 絶対パスでは `/` より上には行きません（`"/../x/.."` → `"/"`）。
- 結果が空になる相対パス（`""`、`"./"`）は `"."` になります。
- 末尾の `/` は残りません（`"a/b/"` → `"a/b"`）。

### `join_under`

信頼できないパス（URL のパスなど）をディレクトリに対応付けるための関数です。

- `rel` が絶対パスなら `None`。
- `normalize(rel)` が `..` または `../` で始まる（`root` の外に出る）なら `None`。
- `normalize(rel)` が `.`（`rel` が空の場合を含む）なら `Some(normalize(root))`。
- それ以外は `Some(join(normalize(root), normalize(rel)))`。

`rel` の途中にある `..` は `root` の内側に収まる限り許されます（`"css/../js/app.js"` は可）。判定は字句的なので、`root` 内のシンボリックリンクが外を指している場合は防げません。

### 例

```rw
import path

def show(o: Option[string]) -> string:
    match o:
        case Some(s):
            return s
        case None:
            return "None"

def main() -> int:
    print(path.join("/srv/www", "css/site.css"))     # /srv/www/css/site.css
    print(path.join("a/", "b"))                      # a/b
    print(path.join("a", "/abs"))                    # /abs
    d, b = path.split("/usr/lib/libx.so")
    print(d + " | " + b)                             # /usr/lib | libx.so
    print(path.dirname("/x") + " " + path.basename("noslash"))   # / noslash
    print(path.ext("x.tar.gz") + " [" + path.ext(".bashrc") + "]")  # .gz []
    print(path.normalize("a/./b/../c//d"))           # a/c/d
    print(path.normalize("/../x/.."))                # /
    print(path.normalize("../../a/.."))              # ../..
    print(show(path.join_under("/srv/www", "css/../js/app.js")))  # /srv/www/js/app.js
    print(show(path.join_under("/srv/www", "../etc/passwd")))     # None
    print(show(path.join_under("/srv/www", "/etc/passwd")))       # None
    return 0
```

各行の出力はコメントのとおりです。

---

## process: 子プロセス

```rw
import process
```

子プロセスを起動し、標準入出力をパイプでつなぎます。`argv[0]` は `PATH` から探されます。シェルは介在しないので、パイプやリダイレクトを使うときは `["sh", "-c", "..."]` と明示してください。子プロセスは親の環境変数と作業ディレクトリを引き継ぎます。

### `class Process`

| フィールド | 型 | 意味 |
|---|---|---|
| `pid` | `int` | プロセス ID |
| `stdin` | `int` | 子の標準入力につながる書き込み側のハンドル |
| `stdout` | `int` | 子の標準出力につながる読み込み側のハンドル |
| `stderr` | `int` | 子の標準エラーにつながる読み込み側のハンドル |

3 つのハンドルには `read` / `write` / `write_all` / `close`、または `bufio.reader` をそのまま使えます。

| メソッド | 戻り値 |
|---|---|
| `wait(self) -> int` | `stdin` がまだ開いていれば閉じて（子は EOF を受け取ります）、子の終了を待ちます。終了ステータス、シグナル N で終了した場合は `128 + N`、エラー時は -1。同じプロセスに 2 回目の `wait` を呼ぶと -1 です |
| `kill(self, sig: string = "SIGTERM") -> bool` | シグナルを名前で送ります（`SIGTERM`、`SIGKILL`、`SIGINT`、`SIGHUP`、`SIGQUIT`、`SIGUSR1`、`SIGUSR2`、`SIGSTOP`、`SIGCONT`）。成功で `true`。未知の名前は `false` で `last_error()` が `EINVAL` |

`wait` が閉じるのは `stdin` だけです。`start` を使う場合、`stdout` と `stderr` は呼び出し側で `close` してください（閉じないとハンドルが漏れます）。

### `class Output`

| フィールド | 型 | 意味 |
|---|---|---|
| `status` | `int` | `Process.wait` と同じ意味の終了ステータス |
| `stdout` | `string` | 標準出力のすべて |
| `stderr` | `string` | 標準エラーのすべて |

### 関数

| 関数 | 戻り値 |
|---|---|
| `run(argv: List[string], input: string = "") -> Option[Output]` | 子を起動し、`input` を標準入力に書き、標準出力と標準エラーを最後まで集めて、終了を待ちます。起動できなければ `None` |
| `start(argv: List[string]) -> Option[Process]` | 子を起動してすぐ返します。起動できなければ `None` |

起動に失敗した場合の `last_error()`:

| 状況 | エラー |
|---|---|
| コマンドが見つからない | `ENOENT` |
| `argv` が空、または引数に NUL バイトを含む | `EINVAL` |

`run` は標準入力への書き込みと標準エラーの読み込みをそれぞれ別のファイバーで行い、その間に標準出力を読みます。入力を読み終える前に大量に出力する子プロセスでも、親と子が互いを待ってデッドロックすることはありません。終了ステータスが 0 以外でも、起動できていれば `run` は `Some` を返します。成否は `out.status` で判断してください。

#### 内部用ヘルパー

`read_all(fd: int) -> string`（EOF まで読んで fd を閉じる）と `feed(fd: int, data: string) -> int`（`data` を書いて fd を閉じる）は `run` の内部用です。

### 例

```rw
import process
import bufio

def main() -> int:
    # Run to completion, feeding stdin.
    match process.run(["sort"], "pear\napple\nfig\n"):
        case Some(out):
            print(f"status={out.status}")
            print(out.stdout)
        case None:
            print("sort: " + error_name(last_error()))

    # No shell is involved: use sh -c explicitly for pipes and redirects.
    match process.run(["sh", "-c", "echo to-stderr >&2; exit 3"]):
        case Some(out):
            print(f"status={out.status} stderr={str_strip(out.stderr)}")
        case None:
            print("sh: " + error_name(last_error()))

    # A missing command is None with ENOENT.
    match process.run(["no-such-command"]):
        case Some(out):
            print(out.status)
        case None:
            print("failed: " + error_name(last_error()))

    # Streaming: read the child's stdout line by line as it is produced.
    match process.start(["sh", "-c", "for i in 1 2 3; do echo tick $i; done"]):
        case Some(p):
            r = bufio.reader(p.stdout)
            while true:
                match r.read_line():
                    case Some(line):
                        print("got: " + line)
                    case None:
                        break
            close(p.stdout)
            close(p.stderr)
            print(f"exit={p.wait()}")
        case None:
            print("start failed")

    # Stop a long-running child: 128 + 15 (SIGTERM).
    match process.start(["sleep", "30"]):
        case Some(p):
            p.kill()
            close(p.stdout)
            close(p.stderr)
            print(p.wait())
        case None:
            print("start failed")
    return 0
```

出力:

```text
status=0
apple
fig
pear

status=3 stderr=to-stderr
failed: ENOENT
got: tick 1
got: tick 2
got: tick 3
exit=0
143
```

---

## template: Mustache 風テンプレート

```rw
import template
import json
```

ロジックを持たないテンプレートを `json.Value` のデータで描画します。Mustache のサブセットです。データは `json.parse` で読むか、`json` のコンストラクタで組み立てます。

### タグ

| タグ | 意味 |
|---|---|
| `{{name}}` | 値をテキストにして HTML エスケープ（`&` `<` `>` `"` `'`）したもの |
| `{{{name}}}`、`{{& name}}` | 値をテキストにしたもの（エスケープなし） |
| `{{a.b.c}}` | ドット区切りの参照 |
| `{{.}}` | 現在の要素（セクションの中で使います） |
| `{{#name}}…{{/name}}` | セクション。値が配列なら要素ごとに 1 回、真の値なら 1 回（その値を新しいコンテキストにして）描画します |
| `{{^name}}…{{/name}}` | 反転セクション。値がないか偽のとき 1 回描画します |
| `{{! comment }}` | 何も出力しません |

タグ内の前後の空白は無視されます（`{{ name }}`、`{{# items }}` も可）。

### 値の解決・テキスト化・真偽

- **参照の解決**: 名前の最初の要素は、コンテキストスタックを内側から順に探します。セクションの中からでも外側の値を参照できます。残りの要素は、見つかった値から下へたどります。最初の要素が内側のコンテキストで見つかった場合、残りが見つからなくても外側へは戻りません（`{{a.b}}` で `a` に `b` がなければ、外側に `b` があっても `""`）。見つからない名前は `""` として描画され、偽として扱われます。
- **テキスト化**: 文字列はそのまま、`null` は `""`、数値と真偽値はテキスト（`1.5`、`true`）、配列とオブジェクトはコンパクトな JSON です（`{{name}}` ではその JSON も HTML エスケープされます）。
- **偽とみなす値**: 見つからない値、`null`、`false`、`""`、`[]`。数値の `0` と空のオブジェクト `{}` は**真**です。

対応していない機能: パーシャル（`{{> x}}`）、ラムダ、区切り文字の変更、Mustache 仕様にある「タグだけの行」の空白除去。

### 型と関数

| 宣言 | 説明 |
|---|---|
| `compile(src: string) -> Result[Template, string]` | テンプレートを一度だけ解析して木構造にします |
| `class Template` の `render(self, data: json.Value) -> string` | コンパイル済みテンプレートを描画します。失敗しません |
| `render(src: string, data: json.Value) -> Result[string, string]` | `compile` と描画を一度に行います |
| `escape_html(s: string) -> string` | `&` `<` `>` `"` `'` を `&amp;` `&lt;` `&gt;` `&quot;` `&#39;` に置き換えます |

多数のリクエストで使うテンプレートは、`compile` を一度だけ呼んで `Template` を使い回してください。

`compile` のエラーは、問題のタグの開始位置（`{{` の位置）を示します。

| メッセージ | 原因 |
|---|---|
| `unclosed section 'NAME'` | `{{#NAME}}` / `{{^NAME}}` に対応する `{{/NAME}}` がない（位置は開始タグ） |
| `unclosed tag` | `{{` に対応する `}}`（三重波括弧なら `}}}`）がない |
| `unexpected closing tag 'NAME'` | 開いていないセクションの `{{/NAME}}` |
| `'NAME' closes section 'OTHER'` | 閉じタグの名前が開いているセクションと一致しない |
| `empty tag` | `{{}}` |

`{{{}}}` と `{{&}}` は名前が空でもエラーにならず、`""` として描画されます。

#### 内部用ヘルパー

`class Node`、`class Parser`、`to_text`、`truthy`、`lookup`、`render_nodes` は内部用です。

### 例

```rw
import json
import template

def main() -> int:
    src = "<h1>{{title}}</h1>\n{{#items}}<li>{{name}}: {{price}} {{currency}}</li>\n{{/items}}{{^items}}<p>no items</p>\n{{/items}}{{! comments render as nothing }}{{{footer}}}"
    match template.compile(src):
        case Ok(t):
            match json.parse("{\"title\": \"Tom & Jerry\", \"currency\": \"JPY\", \"items\": [{\"name\": \"<tea>\", \"price\": 300}, {\"name\": \"cake\", \"price\": 450, \"currency\": \"USD\"}], \"footer\": \"<hr>\"}"):
                case Ok(data):
                    print(t.render(data))
                case Err(msg):
                    print("data: " + msg)
            # The same compiled template, rendered again with other data.
            empty = json.object()
            empty.set("title", json.from_str("Empty")).set("items", json.array([]))
            print(t.render(empty))
        case Err(msg):
            print("template: " + msg)

    # Compile errors carry the position of the offending tag.
    match template.compile("<ul>\n  {{#items}}<li>{{.}}</li>\n</ul>"):
        case Ok(t):
            print("compiled")
        case Err(msg):
            print(msg)
    return 0
```

出力:

```text
<h1>Tom &amp; Jerry</h1>
<li>&lt;tea&gt;: 300 JPY</li>
<li>cake: 450 USD</li>
<hr>
<h1>Empty</h1>
<p>no items</p>

unclosed section 'items' at line 2, column 3
```

1 つ目の `<li>` では、`currency` が要素にないため外側のコンテキストの `"JPY"` が使われます。2 つ目の要素は自分の `currency` を持つので `"USD"` です。2 回目の描画では `footer` がないため `{{{footer}}}` は `""` になり、`print` の改行だけが空行として残ります。

---

## url: パーセントエンコーディングとクエリ文字列

```rw
import url
```

パーセントエンコーディングの変換と、`application/x-www-form-urlencoded` 形式（クエリ文字列・フォーム本文）の解析を行います。デコードには、0x80 以上の 1 バイトを作るための `str_from_byte` builtin を使っています（`chr` はコードポイントを UTF-8 にするので、単独のバイトを作れません）。

### `class Param`

| フィールド | 型 | 意味 |
|---|---|---|
| `key` | `string` | デコード済みのキー |
| `value` | `string` | デコード済みの値 |

### 関数

| 関数 | 戻り値 |
|---|---|
| `unescape(s: string, plus: bool = true) -> string` | `%XX` をデコードしたもの。`plus` が `true`（既定、クエリ・フォームの慣例）なら `+` も空白になります。パスのセグメントには `false` を渡してください。後ろに 16 進数字が 2 つ続かない `%` はそのまま残します |
| `escape(s: string) -> string` | RFC 3986 の非予約文字（`A-Z a-z 0-9 - . _ ~`）以外をすべて `%XX`（大文字の 16 進）にしたもの。空白は `%20` になります |
| `parse_query(q: string) -> List[Param]` | `&` で分割した `key=value` の組を、デコードして出現順に返します。`=` のない要素は値が `""`、空の要素（`a=1&&b=2` の間）は飛ばします。値の中の 2 つ目以降の `=` は値の一部です。`q` に先頭の `?` を含めないでください |
| `get(params: List[Param], key: string) -> Option[string]` | `key` の最初の値。なければ `None` |

`%XX` は 1 バイトずつデコードされるので、多バイトの UTF-8（`%C3%A9` → `é`）もそのまま復元されます。デコード結果が正しい UTF-8 かどうかは検査しません。同じキーが複数回現れる場合（`tag=a&tag=b`）、すべての値が欲しければ `parse_query` の結果を走査してください。

#### 内部用ヘルパー

`hex_value(c: int) -> int`（16 進数字 1 文字の値。数字でなければ -1）は内部用です。

### 例

```rw
import url

def main() -> int:
    q = url.parse_query("q=caf%C3%A9+au+lait&tag=a&tag=b&&debug")
    for p in q:
        print("[" + p.key + "]=[" + p.value + "]")
    match url.get(q, "tag"):
        case Some(v):
            print("first tag: " + v)              # a
        case None:
            print("no tag")
    match url.get(q, "page"):
        case Some(v):
            print(v)
        case None:
            print("no page")                      # missing key
    print(url.unescape("a%20b+c"))                # a b c
    print(url.unescape("a%20b+c", false))         # a b+c  (path segments)
    print(url.unescape("100%"))                   # 100%   (kept as is)
    print(url.escape("a b&c=d/é~"))               # a%20b%26c%3Dd%2F%C3%A9~
    print("/search?q=" + url.escape("rw & go"))   # /search?q=rw%20%26%20go
    return 0
```

出力:

```text
[q]=[café au lait]
[tag]=[a]
[tag]=[b]
[debug]=[]
first tag: a
no page
a b c
a b+c
100%
a%20b%26c%3Dd%2F%C3%A9~
/search?q=rw%20%26%20go
```

---

## ライブラリの例: Web フレームワーク

`examples/web.rw` は、標準ライブラリの上に rw で書かれた小さな HTTP/1.1 Web フレームワークです。標準ライブラリそのものではなく `examples/` に置かれており、使うときはアプリケーションの隣にコピーします（`import web` はエントリファイルのディレクトリから解決されるため）。

標準ライブラリの使い方の実例としても読めます。

| モジュール | `web.rw` での用途 |
|---|---|
| `bufio` | リクエストヘッダを `read_until("\n", limit)` で 1 行ずつ読み（64 KiB の上限付き）、本文を `read_exact` で読む |
| `url` | クエリ文字列・パスパラメータ・フォーム本文のデコード |
| `path` / `fs` | 静的ファイル配信（`join_under` でディレクトリの外への脱出を防ぐ） |
| `log` | アクセスログ |

`examples/web_app.rw` は、さらに `json` と `template` を使った完全なアプリケーションです。API の詳細は [`docs/web-framework.md`](../web-framework.md) を参照してください。

---

## 標準ライブラリにモジュールを追加する

1. **`stdlib/<name>.rw` に置きます。** 既存のモジュールや builtin と名前が衝突しないこと、また利用者が自作モジュールに付けそうな名前でないことを確認してください（ローカルのモジュールが優先されるため）。
2. **`main` を定義してはいけません。** import されるモジュールが `main` を持つとコンパイルエラーになります。
3. **先頭にコメントで概要と短い使用例を書きます。** 既存のモジュールと同じく、仕様番号を書き、`import` から典型的な呼び出しまでを示してください。公開する関数とクラスには、戻り値と失敗時の挙動を 1〜2 行で説明するコメントを付けます。
4. **この文書の約束事に従います。** 「無いかもしれない」は `Option`、入力の解釈エラーは位置付きメッセージの `Result[T, string]`、OS のエラーは `last_error()` に任せて `None` / `false` を返します。
5. **例とテストを追加します。**
   - `examples/<name>_basic.rw` と、その期待出力 `examples/<name>_basic.rw.expected` を作り、`tests/test_e2e.py` の `test_synchronous_example` のリストに名前を追加します。出力は環境によらず同じになるようにしてください（時刻・乱数・PID を出力しない）。
   - ネットワークや並行性、エラー経路など期待出力ファイルで表しにくい挙動は、`tests/test_e2e_<name>.py` で検証します。rw だけで書けるテストなら `rwc test` の `test_*.rw`（仕様 68）も使えます。
6. **仕様書を書きます。** `docs/specs/NN-<name>.md` に、動機・API の表・エラー時の挙動・制限・テストの一覧を書きます。新しい builtin が必要なら、その仕様も同じ文書に含めます。
7. **この文書を更新します。** モジュール一覧の表とモジュールの節を、`docs/ja/stdlib.md` と `docs/stdlib.md` の両方に追加してください。
