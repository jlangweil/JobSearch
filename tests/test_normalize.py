from __future__ import annotations

from jobhunt.sources.normalize import (
    content_hash,
    derive_employment_type,
    derive_remote_type,
    parse_comp,
    slugify,
    strip_html,
)


def test_strip_html_handles_greenhouse_double_escaping():
    escaped = "&lt;p&gt;Hello &amp;amp; welcome&lt;/p&gt;&lt;ul&gt;&lt;li&gt;APIs&lt;/li&gt;&lt;/ul&gt;"
    text = strip_html(escaped)
    assert "Hello & welcome" in text
    assert "• APIs" in text
    assert "<" not in text


def test_strip_html_drops_scripts_and_collapses_whitespace():
    text = strip_html("<div>  One\n\n\n  two <script>alert(1)</script></div><p>three</p>")
    assert text == "One\n\ntwo\n\nthree" or text == "One\ntwo\n\nthree"
    assert "alert" not in text


def test_parse_comp_ranges():
    assert parse_comp("Pay: $170,000 - $210,000 per year") == (170000, 210000, "posted")
    assert parse_comp("Base $180K–$220K") == (180000, 220000, "posted")
    assert parse_comp("$85/hr - $95/hr") == (85 * 2080, 95 * 2080, "inferred")
    assert parse_comp("$90 per hour") == (90 * 2080, 90 * 2080, "inferred")
    assert parse_comp("no pay info, 401k match") == (None, None, "none")
    assert parse_comp("", 150000, 190000) == (150000, 190000, "posted")


def test_remote_and_employment_derivation():
    assert derive_remote_type("Lead Architect - Virtual", "Newark, NJ", "") == "remote"
    assert derive_remote_type("Architect", "Hybrid - Parsippany", "") == "hybrid"
    assert derive_remote_type("Architect", "Newark, NJ", "This role is hybrid, 3 days in office") == "hybrid"
    assert derive_remote_type("Architect", "SF", "", hint="On-site") == "onsite"
    assert derive_remote_type("Architect", "SF", "") == "unknown"
    assert derive_employment_type("Architect", "", hint="Full time") == "fte"
    assert derive_employment_type("Contract Integration Architect", "") == "contract"
    assert derive_employment_type("Architect", "This is a contract-to-hire role") == "c2h"


def test_hash_ignores_whitespace_and_case():
    assert content_hash("A  B", "Co", "x\n y") == content_hash("a b", "co", "X Y")


def test_slugify():
    assert slugify("Johnson & Johnson") == "johnson-and-johnson"
    assert slugify("CVS Health, Inc.") == "cvs-health"
    assert slugify("Zürich Re") == "zurich-re"
