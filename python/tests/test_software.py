import pytest

from PiFinder.ui.software import (
    update_needed,
    _parse_version,
    _strip_markdown,
    _UNLOCK_SEQUENCE,
)


@pytest.mark.unit
class TestParseVersion:
    def test_simple_version(self):
        assert _parse_version("2.4.0") == (2, 4, 0, 1, "")

    def test_prerelease_version(self):
        result = _parse_version("2.5.0-beta.1")
        assert result == (2, 5, 0, 0, "beta.1")

    def test_prerelease_sorts_below_release(self):
        assert _parse_version("2.5.0-beta.1") < _parse_version("2.5.0")

    def test_whitespace_stripped(self):
        assert _parse_version("  2.4.0\n") == (2, 4, 0, 1, "")


@pytest.mark.unit
class TestUpdateNeeded:
    def test_newer_version_available(self):
        assert update_needed("2.3.0", "2.4.0") is True

    def test_same_version(self):
        assert update_needed("2.4.0", "2.4.0") is False

    def test_older_version(self):
        assert update_needed("2.5.0", "2.4.0") is False

    def test_major_version_bump(self):
        assert update_needed("1.9.9", "2.0.0") is True

    def test_patch_bump(self):
        assert update_needed("2.4.0", "2.4.1") is True

    def test_prerelease_to_release(self):
        assert update_needed("2.5.0-beta.1", "2.5.0") is True

    def test_release_to_prerelease_same(self):
        # Going from a release to a pre-release of same version is not an update
        assert update_needed("2.5.0", "2.5.0-beta.1") is False

    def test_prerelease_higher_minor(self):
        # 2.5.0-beta.1 is still greater than 2.4.0
        assert update_needed("2.4.0", "2.5.0-beta.1") is True

    def test_garbage_input_returns_false(self):
        assert update_needed("garbage", "2.4.0") is False

    def test_empty_string_returns_false(self):
        assert update_needed("", "") is False

    def test_partial_version_returns_false(self):
        assert update_needed("2.4", "2.5.0") is False

    def test_unknown_returns_false(self):
        assert update_needed("2.4.0", "Unknown") is False


@pytest.mark.unit
class TestUnlockSequence:
    def test_sequence_length(self):
        assert len(_UNLOCK_SEQUENCE) == 7

    def test_sequence_content(self):
        assert _UNLOCK_SEQUENCE == ["square"] * 7


@pytest.mark.unit
class TestStripMarkdown:
    def test_removes_headings(self):
        assert _strip_markdown("# Hello") == "Hello"
        assert _strip_markdown("## Sub") == "Sub"

    def test_removes_bold(self):
        assert _strip_markdown("**bold**") == "bold"

    def test_removes_italic(self):
        assert _strip_markdown("*italic*") == "italic"

    def test_removes_links(self):
        assert _strip_markdown("[text](http://example.com)") == "text"

    def test_removes_backticks(self):
        assert _strip_markdown("`code`") == "code"

    def test_preserves_plain_text(self):
        assert _strip_markdown("Hello world") == "Hello world"

    def test_multiline(self):
        md = "# Title\n\nSome **bold** text.\n- item"
        result = _strip_markdown(md)
        assert "Title" in result
        assert "bold" in result
        assert "**" not in result
