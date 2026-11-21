"""citation_links.canvas_url: the Canvas page a search result cites."""

import pytest

import citation_links

ORIGIN = "https://canvas.example.edu"


@pytest.mark.parametrize(
    "item_type, item_id, url",
    [
        ("file", "file:14690029", f"{ORIGIN}/courses/56350/files/14690029"),
        ("page", "page:week-1-overview", f"{ORIGIN}/courses/56350/pages/week-1-overview"),
        ("assignment", "assignment:1020612", f"{ORIGIN}/courses/56350/assignments/1020612"),
        ("syllabus", "syllabus:56350", f"{ORIGIN}/courses/56350/assignments/syllabus"),
        (None, "file:7", f"{ORIGIN}/courses/56350/files/7"),  # an unknown type is a file
    ],
)
def test_canvas_url(item_type, item_id, url):
    assert citation_links.canvas_url(ORIGIN, 56350, item_type, item_id) == url


def test_no_id_means_no_link():
    assert citation_links.canvas_url(ORIGIN, 56350, "file", "") is None

