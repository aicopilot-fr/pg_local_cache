#!/usr/bin/env python3
"""Static contracts for the GitHub Pages documentation source."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import json
import re
import struct
import unittest


ROOT = Path(__file__).resolve().parents[1]
PUBLISHED_DOCUMENTS = (
    ROOT / "docs" / "INSTALL_EXISTING.md",
    ROOT / "docs" / "BENCHMARKS.md",
    ROOT / "docs" / "MONITORING.md",
    ROOT / "docs" / "TECHNICAL.md",
)
MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


class _IndexParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.h1_count = 0
        self.image_sources: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        if attributes.get("id"):
            self.ids.add(attributes["id"] or "")
        if tag == "h1":
            self.h1_count += 1
        if tag == "img" and attributes.get("src"):
            self.image_sources.append(attributes["src"] or "")


def front_matter(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "---":
        raise AssertionError(f"{path}: missing YAML front matter")
    try:
        end = lines.index("---", 1)
    except ValueError as error:
        raise AssertionError(f"{path}: unterminated YAML front matter") from error
    values: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition(":")
        if not separator:
            raise AssertionError(f"{path}: unsupported front-matter line {line!r}")
        values[key.strip()] = value.strip()
    return values


class PagesSourceContracts(unittest.TestCase):
    def test_homepage_is_semantic_static_html(self) -> None:
        source = (ROOT / "index.html").read_text(encoding="utf-8")
        parser = _IndexParser()
        parser.feed(source)
        self.assertEqual(parser.h1_count, 1)
        self.assertIn("main-content", parser.ids)
        self.assertIn("quick-start", parser.ids)
        self.assertIn("sql-api", parser.ids)
        self.assertEqual(parser.image_sources, [])
        self.assertNotIn("lorem ipsum", source.lower())
        self.assertNotIn("10x", source.lower())
        self.assertIn("local_cache.mget", source)
        self.assertIn("SELECT * FROM public.items WHERE id = $1::bigint", source)
        self.assertIn("≥1.50x", source)
        self.assertIn("c16/k32 throughput", source)

    def test_benchmark_pages_publish_the_sql_kv_release_contract(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        benchmarks = (ROOT / "docs" / "BENCHMARKS.md").read_text(
            encoding="utf-8"
        )
        for source in (readme, benchmarks):
            self.assertIn("local_cache.mget", source)
            self.assertIn("1.50", source)
            self.assertIn("3,000-byte", source)
            self.assertIn("stock PostgreSQL", source)
        self.assertIn("Historical transparent-SQL results", benchmarks)
        self.assertNotIn("30729192604", readme)

    def test_docs_prefer_ordinary_sql_for_native_tuples(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        technical = (ROOT / "docs" / "TECHNICAL.md").read_text(
            encoding="utf-8"
        )
        install = (ROOT / "docs" / "INSTALL_EXISTING.md").read_text(
            encoding="utf-8"
        )
        for source in (readme, technical, install):
            self.assertIn("SELECT * FROM public.items WHERE id =", source)
            self.assertIn("SELECT value", source)
            self.assertNotIn("NULL::public.items", source)
        self.assertIn("No result-type witness", technical)

    def test_every_published_document_has_metadata(self) -> None:
        documents = {
            "INSTALL_EXISTING.md": "/docs/INSTALL_EXISTING.html",
            "BENCHMARKS.md": "/docs/BENCHMARKS.html",
            "MONITORING.md": "/docs/MONITORING.html",
            "TECHNICAL.md": "/docs/TECHNICAL.html",
        }
        for name, permalink in documents.items():
            with self.subTest(name=name):
                metadata = front_matter(ROOT / "docs" / name)
                self.assertEqual(metadata.get("layout"), "doc")
                self.assertEqual(metadata.get("permalink"), permalink)
                self.assertTrue(metadata.get("title"))
                self.assertGreater(len(metadata.get("description", "")), 50)

    def test_published_documents_do_not_link_to_unbuilt_markdown(self) -> None:
        github_source_prefix = (
            "https://github.com/aicopilot-fr/pg_local_cache/blob/main/"
        )
        for path in PUBLISHED_DOCUMENTS:
            source = path.read_text(encoding="utf-8")
            for match in MARKDOWN_LINK.finditer(source):
                target = match.group(1).strip()
                target_path = target.split("#", 1)[0]
                if not target_path.lower().endswith(".md"):
                    continue
                with self.subTest(document=path.name, target=target):
                    self.assertTrue(
                        target.startswith(github_source_prefix),
                        f"{path}: relative .md link is not built by Pages: {target}",
                    )

    def test_homepage_terminal_uses_one_explicit_transaction(self) -> None:
        source = (ROOT / "index.html").read_text(encoding="utf-8")
        begin = source.index('app=&gt; <span class="sql">BEGIN;</span>')
        update = source.index(
            'app=*&gt; <span class="sql">UPDATE</span>', begin
        )
        commit = source.index(
            'app=*&gt; <span class="sql">COMMIT;</span>', update
        )
        self.assertLess(begin, update)
        self.assertLess(update, commit)
        self.assertIn("SET value = 'updated' WHERE id = 42;", source)
        self.assertNotIn("SET price", source)
        self.assertNotIn("Buffers: shared hit=0 read=0", source)
        self.assertIn("fenced before commit became visible", source)
        self.assertIn("#security-boundary", source)
        self.assertNotIn(
            "'/docs/TECHNICAL.html' | relative_url }}#security\"", source
        )

    def test_default_layout_has_social_metadata(self) -> None:
        layout = (ROOT / "_layouts" / "default.html").read_text(
            encoding="utf-8"
        )
        for marker in (
            'rel="canonical"',
            'property="og:title"',
            'property="og:description"',
            'property="og:image"',
            'content="1200"',
            'content="630"',
            'name="twitter:card" content="summary_large_image"',
            'name="twitter:image"',
            '"@type": "SoftwareSourceCode"',
            'href="#main-content"',
        ):
            self.assertIn(marker, layout)

        social_card = ROOT / "assets" / "social-card.png"
        self.assertTrue(social_card.is_file())
        self.assertGreater(social_card.stat().st_size, 10_000)
        header = social_card.read_bytes()[:24]
        self.assertEqual(header[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack(">II", header[16:24]), (1200, 630))

        structured = re.search(
            r'<script type="application/ld\+json">\s*(.*?)\s*</script>',
            layout,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(structured)
        payload = (structured.group(1) if structured else "").replace(
            "{{ site.description | jsonify }}", '"description"'
        )
        self.assertEqual(json.loads(payload)["@type"], "SoftwareSourceCode")

    def test_pages_workflow_uses_pinned_official_actions(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "pages.yml").read_text(
            encoding="utf-8"
        )
        expected = {
            "actions/checkout": "11bd71901bbe5b1630ceea73d27597364c9af683",
            "actions/configure-pages": "983d7736d9b0ae728b81ab479565c72886d7745b",
            "actions/jekyll-build-pages": "44a6e6beabd48582f863aeeb6cb2151cc1716697",
            "actions/upload-pages-artifact": "56afc609e74202658d3ffba0e8f6dda462b719fa",
            "actions/deploy-pages": "d6db90164ac5ed86f2b6aed7e0febac5b3c0c03e",
        }
        for action, sha in expected.items():
            self.assertIn(f"uses: {action}@{sha}", workflow)
        self.assertNotRegex(workflow, r"uses:\s+[^\n]+@(v\d+|main)\s*$")
        self.assertIn("pages: write", workflow)
        self.assertIn("id-token: write", workflow)

    def test_pull_requests_build_pages_without_deploying(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "pages.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("  pull_request:\n", workflow)
        self.assertIn("if: github.event_name == 'pull_request'", workflow)
        self.assertIn("Build Jekyll site without deployment", workflow)
        self.assertIn("if: github.event_name != 'pull_request'", workflow)
        self.assertIn("group: pg_local_cache-pages-${{ github.ref }}", workflow)
        validation = workflow[
            workflow.index("  validate:") : workflow.index("  deploy:")
        ]
        self.assertIn("actions/jekyll-build-pages@", validation)
        self.assertNotIn("actions/configure-pages@", validation)
        self.assertNotIn("actions/upload-pages-artifact@", validation)
        self.assertNotIn("actions/deploy-pages@", validation)
        self.assertEqual(workflow.count("actions/deploy-pages@"), 1)

    def test_crawler_files_use_the_configured_origin(self) -> None:
        config = (ROOT / "_config.yml").read_text(encoding="utf-8")
        robots = (ROOT / "robots.txt").read_text(encoding="utf-8")
        sitemap = (ROOT / "sitemap.xml").read_text(encoding="utf-8")
        self.assertIn("url: https://aicopilot-fr.github.io", config)
        self.assertIn("baseurl: /pg_local_cache", config)
        self.assertIn("Sitemap:", robots)
        for page in (
            "/docs/INSTALL_EXISTING.html",
            "/docs/BENCHMARKS.html",
            "/docs/MONITORING.html",
            "/docs/TECHNICAL.html",
        ):
            self.assertIn(page, sitemap)

    def test_styles_cover_accessibility_and_responsive_layouts(self) -> None:
        css = (ROOT / "assets" / "site.css").read_text(encoding="utf-8")
        self.assertIn(".skip-link:focus", css)
        self.assertIn("@media (max-width: 720px)", css)
        self.assertIn("@media (prefers-reduced-motion: reduce)", css)
        self.assertIn("@media (prefers-color-scheme: dark)", css)

    def test_navigation_and_document_toc_are_progressive(self) -> None:
        default_layout = (ROOT / "_layouts" / "default.html").read_text(
            encoding="utf-8"
        )
        doc_layout = (ROOT / "_layouts" / "doc.html").read_text(
            encoding="utf-8"
        )
        css = (ROOT / "assets" / "site.css").read_text(encoding="utf-8")
        javascript = (ROOT / "assets" / "site.js").read_text(encoding="utf-8")
        self.assertIn('class="no-js"', default_layout)
        self.assertIn("classList.replace('no-js', 'js')", default_layout)
        self.assertIn(".js .site-nav", css)
        self.assertIn("data-doc-toc", doc_layout)
        self.assertIn('aria-current="page"', doc_layout)
        self.assertIn("data-doc-toc-list", javascript)
        self.assertIn("document.querySelectorAll('.doc-content table')", javascript)
        self.assertIn("Scrollable data table", javascript)
        self.assertIn("region.setAttribute('tabindex', '0')", javascript)


if __name__ == "__main__":
    unittest.main()
