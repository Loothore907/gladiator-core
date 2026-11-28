"""The home page (website/, published at gladiatorbench.com by .github/workflows/pages.yml).

It is one static page and a not-found page. Checked here: every address on them leads somewhere that exists, they
run no script and load nothing from anywhere else (the fonts and pictures are in website/), a link within the page
names a part of it that is there, and the command the home page prints is one CI plays."""

import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "website"
# The only places these pages send anyone: the project's own two pages and its own two repositories.
OWN = (
    "https://gladiatorbench.com/",
    "https://ledger.gladiatorbench.com/",
    "https://github.com/Loothore907/gladiator-core",
    "https://github.com/Loothore907/gladiator-ledger",
)
IN_THIS_REPO = "https://github.com/Loothore907/gladiator-core/"


class Read(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.loaded, self.tags, self.code, self.meta, self._in = [], [], [], [], {}, []
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append(tag)
        self._in.append(tag)
        if attrs.get("id"):
            self.ids.add(attrs["id"])
        if tag == "a":
            self.links.append(attrs.get("href") or "")
        if tag == "link" and attrs.get("rel") != "canonical":
            self.loaded.append(attrs.get("href") or "")
        if tag in ("img", "script", "iframe", "source", "video", "audio", "object", "embed"):
            self.loaded.append(attrs.get("src") or attrs.get("data") or "")
        if tag == "meta" and attrs.get("name"):
            self.meta[attrs["name"]] = attrs.get("content") or ""

    def handle_endtag(self, tag):
        while self._in and self._in.pop() != tag:
            pass

    def handle_data(self, data):
        if self._in and self._in[-1] == "code":
            self.code.append(data.strip())


def read(name: str) -> Read:
    page = Read()
    page.feed((SITE / name).read_text(encoding="utf-8"))
    return page


class HomePageTests(unittest.TestCase):
    def test_there_is_a_home_page_and_a_not_found_page(self):
        self.assertEqual(sorted(p.name for p in SITE.glob("*.html")), ["404.html", "index.html"])

    def test_every_link_goes_to_the_projects_own_pages_and_to_something_that_exists(self):
        for name in ("index.html", "404.html"):
            page = read(name)
            links = page.links
            self.assertTrue(links, name)
            for link in links:
                # a link within the page (the bar along the top) names a part of the page that is there
                if link.startswith("#"):
                    self.assertIn(link[1:], page.ids, f"{name}: {link}")
                    continue
                self.assertTrue(any(link == own or link.startswith(own) for own in OWN), f"{name}: {link}")
                # a link into this repository names a file or folder that is in it
                if link.startswith(IN_THIS_REPO):
                    kind, branch, path = link[len(IN_THIS_REPO) :].split("/", 2)
                    self.assertIn(kind, ("blob", "tree"), link)
                    self.assertEqual(branch, "main", link)
                    there = ROOT / path
                    self.assertTrue(there.is_file() if kind == "blob" else there.is_dir(), f"{name}: {link}")

    def test_it_sends_visitors_to_ledger_and_to_the_engine(self):
        links = read("index.html").links
        self.assertIn("https://ledger.gladiatorbench.com/", links)
        self.assertIn("https://github.com/Loothore907/gladiator-core", links)

    def test_nothing_is_run_and_nothing_is_loaded_from_anywhere_else(self):
        for name in ("index.html", "404.html"):
            page = read(name)
            self.assertNotIn("script", page.tags, name)
            for address in page.loaded:
                if address.startswith("data:"):  # the icon, written into the page itself
                    continue
                self.assertNotIn("//", address, f"{name} loads {address}")
                self.assertTrue((SITE / address).is_file(), f"{name} loads {address}, which is not in website/")

    def test_the_style_sheet_loads_only_files_that_are_here(self):
        sheet = (SITE / "styles.css").read_text(encoding="utf-8")
        loaded = re.findall(r"url\(\s*[\"']?([^\"')]+)", sheet)
        self.assertTrue(loaded)  # the fonts
        for address in loaded:
            self.assertNotIn("//", address, f"styles.css loads {address}")
            self.assertTrue((SITE / address).is_file(), f"styles.css loads {address}, which is not in website/")
        self.assertNotIn("@import", sheet)

    def test_the_pictures_are_sized_for_the_web(self):
        # Each picture is a compressed copy, never the full-size original: a visitor on a phone downloads them all.
        pictures = sorted((SITE / "img").glob("*"))
        self.assertTrue(pictures)
        for picture in pictures:
            self.assertEqual(picture.suffix, ".jpg", picture.name)
            self.assertLess(picture.stat().st_size, 250_000, picture.name)

    def test_the_home_page_says_what_it_is(self):
        page = read("index.html")
        self.assertIn("title", page.tags)
        self.assertGreater(len(page.meta.get("description", "")), 60)

    def test_the_command_it_prints_is_one_ci_plays_on_every_push(self):
        commands = [c for c in read("index.html").code if c.startswith("uv run gladiator match")]
        self.assertEqual(len(commands), 1)
        played = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn(commands[0], played)


if __name__ == "__main__":
    unittest.main()
