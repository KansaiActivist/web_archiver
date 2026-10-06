# Web Site Archiver

指定したURLを起点に、WebサイトをローカルアーカイブするPythonスクリプトです。

HTMLだけではなく、ブラウザが実際に取得したCSS、JavaScript、画像、フォント、SVGなどのリソースを保存し、HTML/CSSの参照をローカル相対パスへ書き換えます。

## 取得モード

`config.json` の `mode` で選択します。

### 1. folder

指定ディレクトリ以下を巡回します。

```json
{
    "url": "https://example.com/wiki/",
    "mode": "folder",
    "scope_path": "/wiki/"
}
```

この場合、

```text
/wiki/
/wiki/page1
/wiki/page2
/wiki/category/page3
```

など、`/wiki/` 以下のページを巡回します。

`max_pages` が巡回上限です。

### 2. index

指定したURLの1ページだけを取得します。

```json
{
    "url": "https://example.com/wiki/index.html",
    "mode": "index"
}
```

このモードでは指定された `index.html` をブラウザで開き、そのページが実際に読み込んだリソースを保存します。

例えば、

```html
<link rel="stylesheet" href="/assets/style.css">
<script src="/assets/app.js"></script>
<img src="/images/logo.png">
```

なら、CSS、JS、画像も保存します。

CSSから参照される、

```css
background-image: url("/images/background.png");
```

のようなファイルも、ブラウザが取得した場合は保存します。

### indexモードでリンク先ページも取得する

通常は `index.html` だけです。

必要なら、

```json
"follow_index_links": true
```

にできます。

この場合、index.htmlからリンクされているURLも取得候補になります。ただし、完全なページ巡回をしたい場合は `folder` モードを推奨します。

## 外部CDN

```json
"capture_external_resources": true
```

なら外部CDNなども保存対象です。

例えば、

```text
https://cdn.example.com/style.css
https://fonts.example.com/font.woff2
```

なども取得します。

falseにすると対象サイトと同じホストのリソースだけを保存します。

## オフライン化

取得したHTML/CSSに含まれる、

```text
https://example.com/assets/style.css
/assets/image.png
```

などの参照を保存先への相対パスに書き換えます。

そのため、取得後にインターネット接続を切っても、保存されたリソースの範囲で元サイトに近い状態で閲覧できます。

## 定期取得

```json
"schedule_enabled": true,
"interval_minutes": 360
```

なら6時間ごとに取得します。

```json
"schedule_enabled": false
```

なら1回取得して終了します。

## インストール

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
```

## 実行

```bash
python archive.py
```

## 注意

ログイン状態、Cookie、WebSocket、API、サーバー側処理などは静的ファイルだけでは完全に再現できない場合があります。

また、JavaScriptがAPIから取得するデータについては、取得時のリソースを保存できても、オフラインで同じAPI処理が動作するとは限りません。

robots.txt、利用規約、著作権、アクセス制限などを確認したうえで使用してください。

## 出力ファイル

1回の取得ごとに `archives/YYYY-MM-DD_HH-MM-SS/` が作成されます。

- `.png` : 実際にブラウザで表示されたページのスクリーンショット
- `.mhtml` : Webアーカイブファイル。ページとブラウザが読み込んだリソースを1ファイルにまとめた形式で、対応ブラウザで開けます
- `.html` : ローカル保存したHTML
- `.css` / `.js` / フォント / JSON / XML / SVG / 動画 / 音声 / その他取得したリソース
- `metadata.json` : 取得情報

`config.json` の `save_web_archive` を `false` にすると `.mhtml` の生成だけ無効化できます。

### MHTMLについて

`.mhtml` はブラウザのWebアーカイブ形式です。取得時点でページが実際に読み込んだリソースをまとめるため、保存した `.mhtml` をブラウザで開くことで、取得時の表示状態を再現しやすくなります。JavaScriptによる外部通信やログイン状態など、サイト側の動的処理まで完全にオフライン化するものではありません。


## Webアーカイブ形式
`config.json` の `web_archive_formats` で選択できます。

```json
"web_archive_formats": ["mhtml", "warc"]
```

指定可能な形式:
- `mhtml`: ChromiumのWebアーカイブ。ブラウザで開いて表示を確認できます。
- `warc`: WARC 1.0形式をgzip圧縮した `web_archive.warc.gz`。Webアーカイブ保存・解析ツール向けです。

片方だけなら `["mhtml"]` または `["warc"]`、両方なら `["mhtml", "warc"]` とします。

PNG、HTML、CSS、JS、画像、フォントなどの取得リソース保存はこれまで通り行われます。
