"""Tests for Hacker News HTML scraper."""

from unittest.mock import MagicMock
import pytest
from pipeline.scrapers.hackernews import HackerNewsScraper


MOCK_SUBMITTED_HTML = """
<html>
<body>
<table>
  <tr class="athing" id="101">
    <td class="title"><span class="titleline"><a href="item?id=101">Ask HN: Who wants to be hired? (November 2026)</a></span></td>
  </tr>
  <tr class="athing" id="102">
    <td class="title"><span class="titleline"><a href="item?id=102">Ask HN: Who is hiring? (November 2026)</a></span></td>
  </tr>
  <tr class="athing" id="103">
    <td class="title"><span class="titleline"><a href="item?id=103">Ask HN: Freelancer? (November 2026)</a></span></td>
  </tr>
</table>
</body>
</html>
"""

MOCK_THREAD_PAGE_1 = """
<html>
<body>
<table>
  <!-- Submission main row -->
  <tr class="athing" id="102">
    <td class="title"><span class="titleline"><a href="#">Ask HN: Who is hiring?</a></span></td>
  </tr>
  <!-- Top-level comment 1 -->
  <tr class="athing comtr" id="201">
    <td><table><tr>
      <td class="ind"><img src="s.gif" height="1" width="0"></td>
      <td class="default">
        <div class="comment">
          <span class="commtext">Stripe | Seattle & SF | Full-time | Staff Data Engineer\nWe are looking for senior talent.</span>
          <div class="reply"><p><font size="1"><a href="reply?id=201">reply</a></font></p></div>
        </div>
      </td>
    </tr></table></td>
  </tr>
  <!-- Child reply comment to 201 (indent width 40, must be skipped) -->
  <tr class="athing comtr" id="202">
    <td><table><tr>
      <td class="ind"><img src="s.gif" height="1" width="40"></td>
      <td class="default">
        <div class="comment">
          <span class="commtext">Is this open to remote candidates?</span>
        </div>
      </td>
    </tr></table></td>
  </tr>
  <!-- Top-level comment 2 without pipe -->
  <tr class="athing comtr" id="203">
    <td><table><tr>
      <td class="ind"><img src="s.gif" height="1" width="0"></td>
      <td class="default">
        <div class="comment">
          <div class="commtext">We are hiring ML Engineers at Stealth Startup in NYC. Apply directly via email.</div>
        </div>
      </td>
    </tr></table></td>
  </tr>
  <!-- Deleted comment (should be skipped) -->
  <tr class="athing comtr" id="204">
    <td><table><tr>
      <td class="ind"><img src="s.gif" height="1" width="0"></td>
      <td class="default">
        <div class="comment">
          <span class="commtext">[deleted]</span>
        </div>
      </td>
    </tr></table></td>
  </tr>
  <tr class="morespace" style="height:10px"></tr>
  <tr><td colspan="2"></td><td class="title"><a class="morelink" href="item?id=102&p=2" rel="next">More</a></td></tr>
</table>
</body>
</html>
"""

MOCK_THREAD_PAGE_2 = """
<html>
<body>
<table>
  <!-- Top-level comment 3 on page 2 -->
  <tr class="athing comtr" id="301">
    <td><table><tr>
      <td class="ind"><img src="s.gif" height="1" width="0"></td>
      <td class="default">
        <div class="comment">
          <span class="commtext">**Databricks** | Mountain View, CA | Senior Platform Engineer\nJoin our lakehouse engine team.</span>
        </div>
      </td>
    </tr></table></td>
  </tr>
  <!-- No more link on page 2 -->
</table>
</body>
</html>
"""


def test_find_latest_hiring_thread():
    client_mock = MagicMock()
    response_mock = MagicMock()
    response_mock.status_code = 200
    response_mock.text = MOCK_SUBMITTED_HTML
    client_mock.get.return_value = response_mock

    scraper = HackerNewsScraper(client=client_mock)
    thread = scraper.find_latest_hiring_thread("https://news.ycombinator.com/submitted?id=whoishiring")

    assert thread is not None
    assert thread["id"] == "102"
    assert thread["title"] == "Ask HN: Who is hiring? (November 2026)"
    assert thread["url"] == "https://news.ycombinator.com/item?id=102"


def test_fetch_thread_comments_pagination():
    client_mock = MagicMock()

    resp_page_1 = MagicMock()
    resp_page_1.status_code = 200
    resp_page_1.text = MOCK_THREAD_PAGE_1

    resp_page_2 = MagicMock()
    resp_page_2.status_code = 200
    resp_page_2.text = MOCK_THREAD_PAGE_2

    def side_effect(url, **kwargs):
        if "p=2" in url:
            return resp_page_2
        return resp_page_1

    client_mock.get.side_effect = side_effect

    scraper = HackerNewsScraper(client=client_mock, max_pages=5)
    comments = scraper.fetch_thread_comments(thread_id="102")

    # Page 1 has 2 valid top-level comments (201, 203) - 202 is child, 204 is deleted
    # Page 2 has 1 valid top-level comment (301)
    # Total should be 3
    assert len(comments) == 3

    comment_ids = [c["comment_id"] for c in comments]
    assert comment_ids == ["201", "203", "301"]
    assert comments[0]["thread_id"] == "102"


def test_normalize_comment():
    # Pipe separated comment
    c1 = HackerNewsScraper.normalize_comment(
        comment_id="201",
        text="Stripe | Seattle & SF | Full-time | Staff Data Engineer\nWe are looking for senior talent.",
        raw_html="<span>Stripe...</span>",
        thread_id="102",
    )
    assert c1["source"] == "hackernews"
    assert c1["source_id"] == "201"
    assert c1["company"] == "Stripe"
    assert c1["title"] == "Stripe | Seattle & SF | Full-time | Staff Data Engineer"
    assert c1["location_raw"] == ""
    assert c1["url"] == "https://news.ycombinator.com/item?id=201"
    assert "We are looking for senior talent." in c1["description_raw"]
    assert c1["raw_payload"]["thread_id"] == "102"
    assert c1["raw_payload"]["comment_id"] == "201"

    # Non-pipe separated comment
    c2 = HackerNewsScraper.normalize_comment(
        comment_id="203",
        text="We are hiring ML Engineers at Stealth Startup in NYC. Apply directly via email.",
        raw_html="<div>We are...</div>",
        thread_id="102",
    )
    assert c2["company"] == "HackerNews_Post"
    assert c2["title"] == "We are hiring ML Engineers at Stealth Startup in NYC. Apply directly via email."

    # Markdown stripped company
    c3 = HackerNewsScraper.normalize_comment(
        comment_id="301",
        text="**Databricks** | Mountain View, CA | Senior Platform Engineer",
        raw_html="<span>**Databricks**...</span>",
        thread_id="102",
    )
    assert c3["company"] == "Databricks"


def test_full_scrape():
    client_mock = MagicMock()
    resp_sub = MagicMock()
    resp_sub.status_code = 200
    resp_sub.text = MOCK_SUBMITTED_HTML

    resp_item = MagicMock()
    resp_item.status_code = 200
    resp_item.text = MOCK_THREAD_PAGE_1

    def side_effect(url, **kwargs):
        if "submitted" in url:
            return resp_sub
        return resp_item

    client_mock.get.side_effect = side_effect

    scraper = HackerNewsScraper(client=client_mock, max_pages=1)
    results = scraper.scrape()

    assert len(results) == 2
    assert results[0]["company"] == "Stripe"
    assert results[0]["source_id"] == "201"
    assert results[1]["company"] == "HackerNews_Post"
    assert results[1]["source_id"] == "203"
