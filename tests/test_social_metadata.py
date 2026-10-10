"""Regression checks for metadata visible to crawlers in the initial HTML."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def find_static_dir() -> Path:
    # The production-image job mounts tests at /tmp/codelens-tests, while the
    # application assets stay inside the image at /app/app/static.
    candidates = (
        ROOT / "app" / "static",
        Path.cwd() / "app" / "static",
        Path("/app/app/static"),
    )
    return next(path for path in candidates if path.is_dir())


STATIC_DIR = find_static_dir()
HTML_PATH = STATIC_DIR / "index.html"
IMAGE_PATH = STATIC_DIR / "codelens-social-preview.png"
DESCRIPTION = (
    "CodeLens is an AI-powered GitHub pull request review assistant that uses "
    "Retrieval-Augmented Generation (RAG) and historical review comments to "
    "generate context-aware, actionable code review feedback."
)
TITLE = "CodeLens | AI-Powered GitHub PR Reviews"
PRODUCTION_URL = "https://13-51-158-45.sslip.io/"
IMAGE_URL = f"{PRODUCTION_URL}static/codelens-social-preview.png"


class HeadMetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_head = False
        self.metadata: dict[tuple[str, str], list[str]] = {}
        self.canonical_urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "head":
            self.in_head = True
        if not self.in_head:
            return
        if tag == "meta":
            key = "property" if "property" in attributes else "name"
            if key in attributes and attributes.get("content"):
                self.metadata.setdefault((key, attributes[key]), []).append(attributes["content"])
        if tag == "link" and attributes.get("rel") == "canonical":
            href = attributes.get("href")
            if href:
                self.canonical_urls.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag == "head":
            self.in_head = False


class FooterParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_footer_copy = False
        self.copy_text: list[str] = []
        self.author_link: dict[str, str | None] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "span" and "footer-copy" in (attributes.get("class") or "").split():
            self.in_footer_copy = True
        elif tag == "a" and self.in_footer_copy and attributes.get("href") == "https://github.com/sprahasingh":
            self.author_link = attributes

    def handle_data(self, data: str) -> None:
        if self.in_footer_copy:
            self.copy_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "span" and self.in_footer_copy:
            self.in_footer_copy = False

def test_initial_html_has_fixed_social_metadata() -> None:
    html = HTML_PATH.read_text(encoding="utf-8")
    parser = HeadMetadataParser()
    parser.feed(html)
    metadata = parser.metadata

    assert f"<title>{TITLE}</title>" in html
    assert metadata[("name", "author")] == ["Spraha Singh"]
    assert metadata[("name", "description")] == [DESCRIPTION]
    assert metadata[("property", "og:title")] == [TITLE]
    assert metadata[("property", "og:description")] == [DESCRIPTION]
    assert metadata[("property", "og:type")] == ["website"]
    assert metadata[("property", "og:url")] == [PRODUCTION_URL]
    assert metadata[("property", "og:image")] == [IMAGE_URL]
    assert metadata[("property", "og:image:width")] == ["1200"]
    assert metadata[("property", "og:image:height")] == ["630"]
    assert metadata[("property", "og:image:alt")] == ["CodeLens — Code review that learns from history"]
    assert metadata[("name", "twitter:card")] == ["summary_large_image"]
    assert metadata[("name", "twitter:title")] == [TITLE]
    assert metadata[("name", "twitter:description")] == [DESCRIPTION]
    assert metadata[("name", "twitter:image")] == [IMAGE_URL]
    assert parser.canonical_urls == [PRODUCTION_URL]
    assert "similar past patterns matched" not in html.split("</head>", 1)[0]


def test_homepage_response_includes_metadata_and_footer_at_initial_load() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    response = TestClient(app).get("/")
    assert response.status_code == 200

    metadata_parser = HeadMetadataParser()
    metadata_parser.feed(response.text)
    assert metadata_parser.metadata[("name", "author")] == ["Spraha Singh"]
    assert metadata_parser.metadata[("property", "og:type")] == ["website"]
    assert metadata_parser.metadata[("property", "og:title")] == [TITLE]
    assert metadata_parser.metadata[("property", "og:description")] == [DESCRIPTION]
    assert metadata_parser.metadata[("property", "og:image")] == [IMAGE_URL]
    assert metadata_parser.canonical_urls == [PRODUCTION_URL]

    footer_parser = FooterParser()
    footer_parser.feed(response.text)
    assert "".join(footer_parser.copy_text) == "© 2026 CodeLens. Built by Spraha Singh."
    assert footer_parser.author_link is not None
    assert footer_parser.author_link.get("target") == "_blank"
    assert set((footer_parser.author_link.get("rel") or "").split()) == {"noopener", "noreferrer"}
    assert "CodeLens · Built with Voyage AI, pgvector, Groq" not in response.text


def test_social_preview_is_a_1200_by_630_png() -> None:
    image = IMAGE_PATH.read_bytes()
    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    assert int.from_bytes(image[16:20], "big") == 1200
    assert int.from_bytes(image[20:24], "big") == 630
