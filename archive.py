#!/usr/bin/env python3
import asyncio
import gzip
import hashlib
import json
import os
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag, unquote

from playwright.async_api import async_playwright


BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
ARCHIVE_DIR = BASE_DIR / "archives"


def load_config():
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(f"設定ファイルがありません: {CONFIG_FILE}")

    with CONFIG_FILE.open("r", encoding="utf-8") as f:
        config = json.load(f)

    url = str(config.get("url", "")).strip()
    if not url:
        raise ValueError("url を設定してください。")

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("url には http:// または https:// を指定してください。")

    mode = str(config.get("mode", "folder")).lower().strip()
    if mode not in ("folder", "index"):
        raise ValueError('mode は "folder" または "index" にしてください。')

    interval_minutes = int(config.get("interval_minutes", 360))
    max_pages = int(config.get("max_pages", 100))

    if interval_minutes < 1:
        raise ValueError("interval_minutes は1以上にしてください。")
    if max_pages < 1:
        raise ValueError("max_pages は1以上にしてください。")

    scope_path = str(config.get("scope_path", "")).strip() or parsed.path or "/"
    if not scope_path.startswith("/"):
        scope_path = "/" + scope_path

    if scope_path != "/" and not scope_path.endswith("/"):
        scope_path += "/"

    return {
        "url": url,
        "mode": mode,
        "scope_path": scope_path,
        "schedule_enabled": bool(config.get("schedule_enabled", False)),
        "interval_minutes": interval_minutes,
        "max_pages": max_pages,
        "wait_after_load_seconds": float(config.get("wait_after_load_seconds", 3)),
        "save_screenshot": bool(config.get("save_screenshot", True)),
        "web_archive_formats": [
            str(x).lower().strip()
            for x in config.get("web_archive_formats", ["mhtml", "warc"])
            if str(x).lower().strip() in {"mhtml", "warc"}
        ],
        "ignore_https_errors": bool(config.get("ignore_https_errors", False)),
        "capture_external_resources": bool(config.get("capture_external_resources", True)),
        "rewrite_links": bool(config.get("rewrite_links", True)),
        "follow_index_links": bool(config.get("follow_index_links", False)),
    }


def safe_name(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = value.strip(" .")
    return value[:180] or "index"


def local_path_for_url(url):
    parsed = urlparse(url)
    path = unquote(parsed.path or "/")

    if path.endswith("/"):
        path += "index.html"
    elif not Path(path).suffix:
        path += "/index.html"

    parts = [safe_name(x) for x in path.lstrip("/").split("/") if x]
    if not parts:
        parts = ["index.html"]

    filename = parts[-1]
    directory = Path(*parts[:-1]) if len(parts) > 1 else Path()

    if parsed.query:
        query = safe_name(parsed.query.replace("&", "_").replace("=", "-"))
        stem = Path(filename).stem
        suffix = Path(filename).suffix
        filename = f"{stem}__{query}{suffix}"

    return Path(parsed.netloc) / directory / filename


def normalize_url(url):
    return urldefrag(url)[0]


def allowed_folder_url(url, base_url, scope_path):
    parsed = urlparse(url)
    base = urlparse(base_url)

    if parsed.scheme not in ("http", "https"):
        return False

    if parsed.netloc.lower() != base.netloc.lower():
        return False

    path = parsed.path or "/"

    if scope_path == "/":
        return True

    return path == scope_path.rstrip("/") or path.startswith(scope_path)


def allowed_index_page(url, index_url):
    parsed = urlparse(url)
    base = urlparse(index_url)

    return (
        parsed.scheme in ("http", "https")
        and parsed.netloc.lower() == base.netloc.lower()
        and normalize_url(url) == normalize_url(index_url)
    )


def allowed_resource(url, base_url, capture_external):
    parsed = urlparse(url)
    base = urlparse(base_url)

    if parsed.scheme not in ("http", "https"):
        return False

    if capture_external:
        return True

    return parsed.netloc.lower() == base.netloc.lower()


def extract_links(html, current_url):
    result = set()

    patterns = [
        r"""(?:href|src|poster|action)=["']([^"']+)["']""",
        r"""srcset=["']([^"']+)["']""",
    ]

    for pattern in patterns:
        for value in re.findall(pattern, html, re.IGNORECASE):
            for item in value.split(","):
                item = item.strip().split(" ")[0]
                if not item or item.startswith(("data:", "blob:", "javascript:", "mailto:", "tel:", "#")):
                    continue
                result.add(normalize_url(urljoin(current_url, item)))

    for value in re.findall(r"""url\(\s*['"]?([^'")]+)""", html, re.IGNORECASE):
        if value.startswith(("data:", "blob:", "#")):
            continue
        result.add(normalize_url(urljoin(current_url, value)))

    return result


def is_text_resource(content_type, path):
    value = content_type.lower()
    if any(x in value for x in ("text/", "javascript", "json", "xml", "svg", "css")):
        return True

    return Path(path).suffix.lower() in {
        ".css", ".js", ".mjs", ".json", ".xml", ".svg", ".html", ".htm", ".map"
    }


def rewrite_css(text, source_url, source_path, path_map):
    pattern = r"""url\(\s*(['"]?)([^'")]+)\1\s*\)"""

    def replace(match):
        raw = match.group(2).strip()

        if raw.startswith(("data:", "blob:", "#")):
            return match.group(0)

        target_url = normalize_url(urljoin(source_url, raw))
        target = path_map.get(target_url)

        if not target:
            return match.group(0)

        relative = os.path.relpath(target, source_path.parent).replace("\\", "/")
        return f"url({match.group(1)}{relative}{match.group(1)})"

    return re.sub(pattern, replace, text, flags=re.IGNORECASE)


def rewrite_html(text, source_url, source_path, path_map):
    attributes = ("href", "src", "poster", "action")

    for attribute in attributes:
        pattern = rf"""({attribute}\s*=\s*)(["'])([^"']+)(\2)"""

        def replace(match):
            raw = match.group(3)

            if raw.startswith(("data:", "blob:", "javascript:", "mailto:", "tel:", "#")):
                return match.group(0)

            target_url = normalize_url(urljoin(source_url, raw))
            target = path_map.get(target_url)

            if not target:
                return match.group(0)

            relative = os.path.relpath(target, source_path.parent).replace("\\", "/")
            return f"{match.group(1)}{match.group(2)}{relative}{match.group(4)}"

        text = re.sub(pattern, replace, text, flags=re.IGNORECASE)

    return text


def write_warc(path, resources, resource_headers, resource_status, start_url, created_at):
    def write_record(stream, record_type, target_uri, payload, headers=None, status=None):
        payload = payload or b""
        record_id = f"<urn:uuid:{uuid.uuid4()}>"
        date = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
        date = date[:-2] + ":" + date[-2:]
        lines = [
            "WARC/1.0",
            f"WARC-Type: {record_type}",
            f"WARC-Date: {date}",
            f"WARC-Record-ID: {record_id}",
            f"WARC-Target-URI: {target_uri}",
            "WARC-Content-Type: application/http; msgtype=response" if record_type == "response" else "WARC-Content-Type: application/warc-fields",
        ]
        if record_type == "response":
            status = int(status or 200)
            content_type = (headers or {}).get("content-type", "application/octet-stream")
            http_headers = [f"HTTP/1.1 {status} OK"]
            for key, value in (headers or {}).items():
                if key.lower() in {"content-length", "transfer-encoding", "connection"}:
                    continue
                http_headers.append(f"{key}: {value}")
            http_payload = ("\r\n".join(http_headers) + "\r\n\r\n").encode("utf-8") + payload
            payload = http_payload
        lines.append(f"Content-Length: {len(payload)}")
        header_bytes = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")
        stream.write(header_bytes)
        stream.write(payload)
        stream.write(b"\r\n\r\n")

    with gzip.open(path, "wb") as stream:
        info = (
            f"software: Python web_archiver\n"
            f"format: WARC File Format 1.0\n"
            f"created: {created_at}\n"
            f"source: {start_url}\n"
        ).encode("utf-8")
        write_record(stream, "warcinfo", start_url, info)
        for url, body in resources.items():
            write_record(
                stream,
                "response",
                url,
                body,
                resource_headers.get(url, {}),
                resource_status.get(url, 200),
            )


async def archive_once(config):
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    root = ARCHIVE_DIR / timestamp
    root.mkdir(parents=True, exist_ok=True)

    start_url = normalize_url(config["url"])
    pending = [start_url]
    visited_pages = set()
    page_html = {}
    web_archives = {}
    resources = {}
    resource_types = {}
    resource_headers = {}
    resource_status = {}

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()

        context = await browser.new_context(
            ignore_https_errors=config["ignore_https_errors"],
            viewport={"width": 1440, "height": 900},
            device_scale_factor=1,
        )

        async def response_handler(response):
            url = normalize_url(response.url)

            if not allowed_resource(
                url,
                config["url"],
                config["capture_external_resources"],
            ):
                return

            try:
                if response.status >= 400:
                    return

                body = await response.body()
                if not body:
                    return

                resources[url] = body
                resource_types[url] = response.headers.get("content-type", "")
                resource_headers[url] = dict(response.headers)
                resource_status[url] = response.status
            except Exception:
                pass

        context.on("response", response_handler)

        try:
            while pending and len(visited_pages) < config["max_pages"]:
                current = pending.pop(0)

                if current in visited_pages:
                    continue

                if config["mode"] == "folder":
                    allowed = allowed_folder_url(
                        current,
                        config["url"],
                        config["scope_path"],
                    )
                else:
                    allowed = allowed_index_page(current, start_url)

                if not allowed:
                    continue

                page = await context.new_page()
                visited_pages.add(current)

                try:
                    print(f"[ページ] {current}")

                    await page.goto(
                        current,
                        wait_until="networkidle",
                        timeout=60000,
                    )

                    if config["wait_after_load_seconds"] > 0:
                        await page.wait_for_timeout(
                            int(config["wait_after_load_seconds"] * 1000)
                        )

                    html = await page.content()
                    page_html[current] = html

                    if "mhtml" in config["web_archive_formats"]:
                        try:
                            cdp = await context.new_cdp_session(page)
                            snapshot = await cdp.send("Page.captureSnapshot", {"format": "mhtml"})
                            web_archives[current] = snapshot.get("data", "")
                            await cdp.detach()
                        except Exception as e:
                            print(f"[Webアーカイブ失敗] {current}: {e}")

                    if config["mode"] == "folder":
                        for link in extract_links(html, current):
                            if (
                                allowed_folder_url(
                                    link,
                                    config["url"],
                                    config["scope_path"],
                                )
                                and link not in visited_pages
                                and link not in pending
                            ):
                                pending.append(link)

                    elif config["follow_index_links"]:
                        for link in extract_links(html, current):
                            if link not in visited_pages and link not in pending:
                                if allowed_resource(
                                    link,
                                    config["url"],
                                    config["capture_external_resources"],
                                ):
                                    pending.append(link)

                    if config["save_screenshot"]:
                        screenshot_path = root / local_path_for_url(current)
                        screenshot_path = screenshot_path.with_suffix(".png")
                        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
                        await page.screenshot(
                            path=str(screenshot_path),
                            full_page=True,
                        )

                except Exception as e:
                    print(f"[失敗] {current}: {e}")

                finally:
                    await page.close()

        finally:
            await context.close()
            await browser.close()

    path_map = {}

    for url in page_html:
        path_map[url] = root / local_path_for_url(url)

    for url in resources:
        path_map[url] = root / local_path_for_url(url)

    for url, body in resources.items():
        output = path_map[url]
        output.parent.mkdir(parents=True, exist_ok=True)

        if is_text_resource(resource_types.get(url, ""), output):
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError:
                text = body.decode("utf-8", errors="replace")

            if output.suffix.lower() == ".css" or "css" in resource_types.get(url, "").lower():
                text = rewrite_css(text, url, output, path_map)
            elif output.suffix.lower() in {".html", ".htm"}:
                text = rewrite_html(text, url, output, path_map)

            output.write_text(text, encoding="utf-8")
        else:
            output.write_bytes(body)

    if "mhtml" in config["web_archive_formats"]:
        for url, mhtml in web_archives.items():
            output = root / local_path_for_url(url)
            output = output.with_suffix(".mhtml")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(mhtml, encoding="utf-8")

    if "warc" in config["web_archive_formats"]:
        warc_path = root / "web_archive.warc.gz"
        write_warc(
            warc_path,
            resources,
            resource_headers,
            resource_status,
            config["url"],
            datetime.now().astimezone().isoformat(),
        )

    for url, html in page_html.items():
        output = path_map[url]
        output.parent.mkdir(parents=True, exist_ok=True)

        if config["rewrite_links"]:
            html = rewrite_html(html, url, output, path_map)

        output.write_text(html, encoding="utf-8")

    metadata = {
        "url": config["url"],
        "mode": config["mode"],
        "scope_path": config["scope_path"],
        "created_at": datetime.now().astimezone().isoformat(),
        "pages": len(visited_pages),
        "resources": len(resources),
        "web_archive_formats": config["web_archive_formats"],
        "web_archives": len(web_archives),
    }

    (root / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print()
    print(f"[完了] {root}")
    print(f"[取得ページ] {len(visited_pages)}")
    print(f"[取得リソース] {len(resources)}")
    print()


async def main():
    try:
        config = load_config()
    except Exception as e:
        print(f"[設定エラー] {e}")
        sys.exit(1)

    print(f"[URL] {config['url']}")
    print(f"[モード] {config['mode']}")

    if config["mode"] == "folder":
        print(f"[対象ディレクトリ] {config['scope_path']}")
    else:
        print("[対象] 指定したindex.htmlと、そのページから読み込まれるリソース")

    if not config["schedule_enabled"]:
        print("[定期取得] OFF")
        await archive_once(config)
        return

    print("[定期取得] ON")
    print(f"[間隔] {config['interval_minutes']} 分")
    print()

    while True:
        started = datetime.now()

        try:
            await archive_once(config)
        except Exception as e:
            print(f"[エラー] {e}")

        elapsed = (datetime.now() - started).total_seconds()
        wait_seconds = max(
            1,
            config["interval_minutes"] * 60 - elapsed,
        )

        next_time = datetime.fromtimestamp(
            datetime.now().timestamp() + wait_seconds
        ).astimezone()

        print(f"[次回取得] {next_time.strftime('%Y-%m-%d %H:%M:%S')}")
        await asyncio.sleep(wait_seconds)


if __name__ == "__main__":
    asyncio.run(main())
