#!/usr/bin/env python3
"""
Auto-update helper for distributions.json.

The script reads mirror/source metadata from sources_config.json (by default),
discovers the latest artifacts per distribution, and rewrites distributions.json.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Tuple
from urllib.parse import urljoin

import requests

DEFAULT_HEADERS = {
    # 清华等镜像站会封禁浏览器 UA(反爬/反自动下载), 故用通用默认 UA 反而更稳。
    # 实测: Chrome UA -> 403, 默认 curl/requests UA -> 200
    "User-Agent": "iso-hub/1.0 (Linux distribution ISO auto-updater)",
}


class SourceBuilderError(RuntimeError):
    """Raised when a source definition cannot be processed."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate distributions.json from mirror metadata."
    )
    parser.add_argument(
        "--config",
        default="sources_config.json",
        help="Path to source configuration file (default: sources_config.json).",
    )
    parser.add_argument(
        "--output",
        default="distributions.json",
        help="Where to write the generated JSON (default: distributions.json).",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output with indentation.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the generated JSON without writing to disk.",
    )
    return parser.parse_args()


def load_config(path: Path) -> List[Dict]:
    with path.open("r", encoding="utf-8") as fp:
        data = json.load(fp)
    sources = data.get("sources")
    if not isinstance(sources, list):
        raise ValueError("sources_config.json must contain a top-level 'sources' list.")
    return sources


def fetch_text(url: str) -> str:
    response = requests.get(url, headers=DEFAULT_HEADERS, timeout=30)
    response.raise_for_status()
    return response.text


def natural_key(value: str) -> Tuple:
    parts = re.split(r"(\d+)", value)
    key: List = []
    for part in parts:
        if part.isdigit():
            key.append(int(part))
        else:
            key.append(part)
    return tuple(key)


def extract_ordered_unique(
    items: Iterable[str], descending: bool = True
) -> List[str]:
    unique = sorted(set(items), key=natural_key, reverse=descending)
    return unique


def format_template(template: str, context: Dict[str, str]) -> str:
    try:
        return template.format(**context)
    except KeyError as exc:  # pragma: no cover - surfacing config issues loudly
        raise SourceBuilderError(f"Missing placeholder {exc} in context {context}")


def _attach_mirrors(
    source: Dict,
    entry: Dict,
    ctx: Dict[str, str],
    download_template: Optional[str],
    checksum_template: Optional[str],
    sub_key: str = "sub_listing_url",
) -> Dict:
    """为 entry 生成多源候选 download_urls / checksum_urls（含默认主源）。

    mirrors: 可选镜像对象列表；每个镜像可覆盖 listing_url、download_template、
    checksum_template、sub_listing_template。缺省字段继承 source 默认值。
    flat_listing 类无 download_template 时，用 urljoin(mirror 的 listing/sub, 文件名)。
    """
    mirrors = source.get("mirrors") or []
    if not mirrors:
        return entry
    base = source.get("listing_url", "")
    dl_urls = [entry.get("download_url")]
    cs_urls = [entry["checksum_url"]] if entry.get("checksum_url") else []
    for m in mirrors:
        mctx = {**ctx, "listing_url": m.get("listing_url", base)}
        if sub_key and m.get("sub_listing_template"):
            mctx[sub_key] = format_template(m["sub_listing_template"], mctx)
        # 下载候选
        m_dl_tmpl = m.get("download_template") or download_template
        if m_dl_tmpl:
            m_dl = format_template(m_dl_tmpl, mctx)
        else:
            base_url = mctx.get(sub_key) or mctx["listing_url"]
            fname = entry["download_url"].rstrip("/").rsplit("/", 1)[-1]
            m_dl = urljoin(base_url, fname)
        if m_dl and m_dl not in dl_urls:
            dl_urls.append(m_dl)
        # 校验和候选
        m_cs_tmpl = m.get("checksum_template") or checksum_template
        if m_cs_tmpl:
            try:
                m_cs = format_template(m_cs_tmpl, mctx)
            except SourceBuilderError:
                m_cs = None
            if m_cs and m_cs not in cs_urls:
                cs_urls.append(m_cs)
    entry["download_urls"] = dl_urls
    if cs_urls:
        entry["checksum_urls"] = cs_urls
    return entry


def get_primary_match(match: re.Match) -> Tuple[str, Dict[str, str]]:
    groups = {k: v for k, v in match.groupdict().items() if v is not None}
    if "value" in groups:
        primary = groups["value"]
    elif match.groups():
        primary = match.group(1)
    else:
        primary = match.group(0)
    return primary, groups


def build_from_dated_directory(source: Dict) -> List[Dict]:
    listing_url: str = source["listing_url"]
    version_regex: str = source["version_regex"]
    html = fetch_text(listing_url)
    pattern = re.compile(version_regex)
    matches = pattern.finditer(html)
    values: List[str] = []
    for match in matches:
        primary, _ = get_primary_match(match)
        values.append(primary.strip("/"))
    versions = extract_ordered_unique(values)
    max_entries = source.get("max_entries")
    if isinstance(max_entries, int) and max_entries > 0:
        versions = versions[:max_entries]
    entries: List[Dict] = []
    overrides = source.get("overrides", [])
    override_patterns = [
        (re.compile(rule["pattern"]), rule)
        for rule in overrides
        if "pattern" in rule
    ]
    for version in versions:
        context = {
            "version": version,
            "listing_url": listing_url,
        }
        download_template = source["download_template"]
        checksum_template = source.get("checksum_template")
        extra_context: Dict[str, str] = {}
        for pattern, rule in override_patterns:
            if pattern.search(version):
                download_template = rule.get("download_template", download_template)
                checksum_template = rule.get("checksum_template", checksum_template)
                extra_context.update(rule.get("extra_context", {}))
                break
        full_context = {**context, **extra_context}
        download_url = format_template(download_template, full_context)
        checksum_url = (
            format_template(checksum_template, full_context) if checksum_template else ""
        )
        entry = {
            "distribution": source["distribution"],
            "type": source["type"],
            "download_url": download_url,
            "checksum_url": checksum_url,
            "checksum": source.get("checksum", ""),
        }
        _attach_mirrors(source, entry, full_context, download_template, checksum_template)
        entries.append(entry)
    return entries


def build_from_flat_listing(source: Dict) -> List[Dict]:
    listing_url: str = source["listing_url"]
    artifact_regex: str = source["artifact_regex"]
    html = fetch_text(listing_url)
    pattern = re.compile(artifact_regex)
    matches = []
    for match in pattern.finditer(html):
        primary, groups = get_primary_match(match)
        matches.append((primary, groups))
    if not matches:
        raise SourceBuilderError(f"No artifacts matched regex for {listing_url}")
    unique_matches = extract_ordered_unique([item[0] for item in matches])
    grouped: List[Tuple[str, Dict[str, str]]] = []
    for value in unique_matches:
        group_dict = next((g for primary, g in matches if primary == value), {})
        grouped.append((value, group_dict))
    max_entries = source.get("max_entries")
    if isinstance(max_entries, int) and max_entries > 0:
        grouped = grouped[:max_entries]
    entries: List[Dict] = []
    for value, groups in grouped:
        context = {"match": value, "listing_url": listing_url, **groups}
        download_template = source.get("download_template")
        if download_template:
            download_url = format_template(download_template, context)
        else:
            download_url = urljoin(listing_url, value)
        checksum_template = source.get("checksum_template")
        checksum_url = (
            format_template(checksum_template, context) if checksum_template else ""
        )
        entry = {
            "distribution": source["distribution"],
            "type": source["type"],
            "download_url": download_url,
            "checksum_url": checksum_url,
            "checksum": source.get("checksum", ""),
        }
        _attach_mirrors(source, entry, context, download_template, checksum_template)
        entries.append(entry)
    return entries


def build_from_static(source: Dict) -> List[Dict]:
    versions = source.get("versions", [])
    if not versions:
        raise SourceBuilderError("Static source requires a non-empty 'versions' list.")
    entries: List[Dict] = []
    for version in versions:
        context = {"version": version, "listing_url": source.get("listing_url", "")}
        download_template = source.get("download_template")
        if not download_template:
            raise SourceBuilderError("Static source must define download_template.")
        download_url = format_template(download_template, context)
        checksum_template = source.get("checksum_template", "")
        checksum_url = (
            format_template(checksum_template, context) if checksum_template else ""
        )
        entry = {
            "distribution": source["distribution"],
            "type": source["type"],
            "download_url": download_url,
            "checksum_url": checksum_url,
            "checksum": source.get("checksum", ""),
        }
        _attach_mirrors(source, entry, context, download_template, checksum_template)
        entries.append(entry)
    return entries


def build_from_versioned_flat_listing(source: Dict) -> List[Dict]:
    base_listing: str = source["listing_url"]
    version_regex: str = source["version_regex"]
    sub_listing_template: str = source["sub_listing_template"]
    html = fetch_text(base_listing)
    pattern = re.compile(version_regex)
    matches = pattern.finditer(html)
    versions: List[str] = []
    for match in matches:
        primary, _ = get_primary_match(match)
        versions.append(primary.strip("/"))
    versions = extract_ordered_unique(versions)
    max_entries = source.get("max_entries")
    if isinstance(max_entries, int) and max_entries > 0:
        versions = versions[:max_entries]

    artifact_pattern = re.compile(source["artifact_regex"])
    max_artifacts = source.get("max_artifacts", 1)
    entries: List[Dict] = []
    per_version_failures: List[str] = []
    for version in versions:
        context = {"version": version, "listing_url": base_listing}
        sub_listing_url = format_template(sub_listing_template, context)
        try:
            artifact_html = fetch_text(sub_listing_url)
            artifact_matches = []
            for match in artifact_pattern.finditer(artifact_html):
                primary, groups = get_primary_match(match)
                artifact_matches.append((primary, groups))
            if not artifact_matches:
                raise SourceBuilderError(
                    f"No artifacts matched regex for {sub_listing_url}"
                )
            artifact_matches = artifact_matches[:max_artifacts]
            for primary, groups in artifact_matches:
                entry_context = {
                    "version": version,
                    "listing_url": base_listing,
                    "sub_listing_url": sub_listing_url,
                    "match": primary,
                    **groups,
                }
                download_template = source.get("download_template")
                if download_template:
                    download_url = format_template(download_template, entry_context)
                else:
                    download_url = urljoin(sub_listing_url, primary)
                checksum_template = source.get("checksum_template")
                checksum_url = (
                    format_template(checksum_template, entry_context)
                    if checksum_template
                    else ""
                )
                entry = {
                    "distribution": source["distribution"],
                    "type": source["type"],
                    "download_url": download_url,
                    "checksum_url": checksum_url,
                    "checksum": source.get("checksum", ""),
                }
                _attach_mirrors(
                    source, entry, entry_context, download_template, checksum_template
                )
                entries.append(entry)
        except Exception as exc:
            per_version_failures.append(f"{version}: {exc}")
            print(
                f"[WARN] Skipping {source['distribution']} {version}: {exc}",
                file=sys.stderr,
            )
            continue
    if not entries:
        raise SourceBuilderError(
            "No artifacts generated; failures: " + "; ".join(per_version_failures)
        )
    return entries


STRATEGY_BUILDERS = {
    "dated_directory": build_from_dated_directory,
    "flat_listing": build_from_flat_listing,
    "static": build_from_static,
    "versioned_flat_listing": build_from_versioned_flat_listing,
}


def build_entries(source: Dict) -> List[Dict]:
    strategy = source.get("strategy")
    if strategy not in STRATEGY_BUILDERS:
        raise SourceBuilderError(f"Unsupported strategy '{strategy}'.")
    builder = STRATEGY_BUILDERS[strategy]
    return builder(source)


def main() -> None:
    args = parse_args()
    config_path = Path(args.config)
    output_path = Path(args.output)
    try:
        sources = load_config(config_path)
    except Exception as exc:
        raise SystemExit(f"Failed to load config: {exc}") from exc

    all_entries: List[Dict] = []
    failures: List[str] = []
    for source in sources:
        name = source.get("distribution", "unknown")
        try:
            all_entries.extend(build_entries(source))
        except Exception as exc:
            failures.append(f"{name}: {exc}")
            print(f"[WARN] Skipping {name}: {exc}", file=sys.stderr)

    if not all_entries:
        raise SystemExit("No distribution entries were generated.")

    all_entries.sort(key=lambda item: (item["distribution"].lower(), item["download_url"]))
    payload = {"distributions": all_entries}
    json_kwargs = {"ensure_ascii": False}
    if args.pretty:
        json_kwargs["indent"] = 2
    output_data = json.dumps(payload, **json_kwargs)

    if args.dry_run:
        print(output_data)
    else:
        output_path.write_text(output_data + ("\n" if args.pretty else ""), encoding="utf-8")
        print(f"Wrote {len(all_entries)} entries to {output_path}")

    if failures:
        print(
            f"Completed with {len(failures)} warning(s). "
            "Review the log above for skipped distributions.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
