"""Unit tests for the HTML Sanitizer and Content Preprocessor Engine.

Verifies:
1. Stripping of non-content HTML tags (<script>, <style>, <nav>, <header>, <footer>, etc.).
2. Replacement of <li> tags with bullet points ('• ').
3. Structured line breaks for block tags (<br>, <p>, <h1>-<h6>).
4. HTML entity unescaping (&amp;, &quot;, &#39;, &nbsp;, etc., plus Greenhouse escaped markup).
5. CRITICAL evidence preservation: exact verbatim retention of compensation and requirements sentences.
6. Handling of plain text (Hacker News comments with math/operators) vs HTML (Greenhouse/Lever).
7. Prioritization of key sections and stripping of generic legal boilerplate.
8. Graceful handling of empty, whitespace, and malformed inputs.
9. Accurate token reduction metrics.
"""

import html
import sqlite3
from pathlib import Path
import pytest

from pipeline.extractor.preprocessor import JobContentPreprocessor


@pytest.fixture
def preprocessor() -> JobContentPreprocessor:
    """Fixture providing a fresh instance of JobContentPreprocessor."""
    return JobContentPreprocessor()


# -------------------------------------------------------------------------
# Test Suite 1: Non-Content Tag Stripping
# -------------------------------------------------------------------------

def test_clean_html_strips_scripts_and_styles(preprocessor: JobContentPreprocessor):
    """Verify <script>, <style>, <noscript>, and <svg> tags and their contents are purged."""
    raw_html = """
    <html>
      <head>
        <style>body { background-color: #000; color: #fff; } .hidden { display: none; }</style>
        <script type="text/javascript">
          window.dataLayer = window.dataLayer || [];
          function trackUser() { alert('tracking'); }
        </script>
      </head>
      <body>
        <h1>Senior Data Engineer</h1>
        <svg viewBox="0 0 100 100"><circle cx="50" cy="50" r="40"/></svg>
        <noscript><p>Please enable JavaScript to view this application.</p></noscript>
        <p>We are seeking a talented Data Engineer.</p>
      </body>
    </html>
    """
    clean = preprocessor.clean_html(raw_html)

    assert "Senior Data Engineer" in clean
    assert "We are seeking a talented Data Engineer." in clean
    assert "background-color" not in clean
    assert "dataLayer" not in clean
    assert "trackUser" not in clean
    assert "alert" not in clean
    assert "viewBox" not in clean
    assert "Please enable JavaScript" not in clean


def test_clean_html_strips_nav_header_footer_and_forms(preprocessor: JobContentPreprocessor):
    """Verify <nav>, <header>, <footer>, <button>, and <form> tags are removed."""
    raw_html = """
    <header>
      <nav>
        <a href="/jobs">Careers</a>
        <a href="/about">About Us</a>
      </nav>
    </header>
    <main>
      <h1>Machine Learning Engineer</h1>
      <p>Join our Core ML team to build large-scale generative models.</p>
      <form action="/apply" method="POST">
        <input type="text" name="applicant_name" placeholder="Full Name" />
        <button type="submit">Apply Now</button>
      </form>
    </main>
    <footer>
      <p>&copy; 2026 TechCorp Inc. All rights reserved. <a href="/privacy">Privacy</a></p>
    </footer>
    """
    clean = preprocessor.clean_html(raw_html)

    assert "Machine Learning Engineer" in clean
    assert "Join our Core ML team" in clean
    assert "Careers" not in clean
    assert "About Us" not in clean
    assert "applicant_name" not in clean
    assert "Apply Now" not in clean
    assert "All rights reserved" not in clean


# -------------------------------------------------------------------------
# Test Suite 2: Bullet Points and Structured Line Breaks
# -------------------------------------------------------------------------

def test_clean_html_replaces_li_with_bullet_points(preprocessor: JobContentPreprocessor):
    """Verify <li> tags are replaced with clean '• ' bullet points."""
    raw_html = """
    <h2>Key Qualifications:</h2>
    <ul>
      <li>5+ years of software development experience in Python or Go.</li>
      <li>Strong expertise with PostgreSQL, Redis, and message queues.</li>
      <li>Experience building and operating distributed cloud microservices.</li>
    </ul>
    """
    clean = preprocessor.clean_html(raw_html)

    assert "• 5+ years of software development experience in Python or Go." in clean
    assert "• Strong expertise with PostgreSQL, Redis, and message queues." in clean
    assert "• Experience building and operating distributed cloud microservices." in clean


def test_clean_html_structured_line_breaks(preprocessor: JobContentPreprocessor):
    """Verify <br>, <p>, and <h1>-<h6> tags introduce structured line breaks."""
    raw_html = """
    <h1>Backend Architect</h1>
    <p>Line one of the description.<br/>Line two after a break tag.<br>Line three after another break.</p>
    <h2>Responsibilities</h2>
    <p>Own the architectural roadmap.</p>
    """
    clean = preprocessor.clean_html(raw_html)

    assert "Backend Architect" in clean
    assert "Line one of the description." in clean
    assert "Line two after a break tag." in clean
    assert "Line three after another break." in clean
    assert "Responsibilities" in clean
    assert "Own the architectural roadmap." in clean

    # Ensure elements don't get joined without line breaks
    assert "Backend ArchitectLine" not in clean
    assert "break.Responsibilities" not in clean


# -------------------------------------------------------------------------
# Test Suite 3: Entity Unescaping
# -------------------------------------------------------------------------

def test_entity_unescaping(preprocessor: JobContentPreprocessor):
    """Verify decoding of standard HTML entities and Greenhouse-style escaped markup."""
    raw_html = (
        "<p>Stripe &amp; Plaid are hiring &quot;Staff Engineers&quot;&#39;s favorite role! "
        "Skills: C&plus;&plus;, R&amp;D, and 100% remote&nbsp;work.</p>"
    )
    clean = preprocessor.clean_html(raw_html)

    assert "Stripe & Plaid are hiring \"Staff Engineers\"'s favorite role!" in clean
    assert "R&D" in clean
    assert "remote work" in clean


def test_greenhouse_escaped_html_payload(preprocessor: JobContentPreprocessor):
    """Verify Greenhouse ATS double-escaped or entity-encoded HTML strings are sanitized correctly."""
    escaped_payload = (
        "&lt;h2&gt;Who we are&lt;/h2&gt;\n"
        "&lt;p&gt;Stripe is a financial infrastructure platform for businesses.&lt;/p&gt;\n"
        "&lt;h3&gt;Minimum requirements&lt;/h3&gt;\n"
        "&lt;ul&gt;\n"
        "&lt;li&gt;5+ years of software engineering experience.&lt;/li&gt;\n"
        "&lt;li&gt;Strong knowledge of distributed systems.&lt;/li&gt;\n"
        "&lt;/ul&gt;"
    )
    clean = preprocessor.clean_html(escaped_payload)

    assert "Who we are" in clean
    assert "Stripe is a financial infrastructure platform for businesses." in clean
    assert "Minimum requirements" in clean
    assert "• 5+ years of software engineering experience." in clean
    assert "• Strong knowledge of distributed systems." in clean
    assert "&lt;" not in clean
    assert "&gt;" not in clean


# -------------------------------------------------------------------------
# Test Suite 4: CRITICAL Evidence Preservation Requirement
# -------------------------------------------------------------------------

def test_verbatim_compensation_preservation(preprocessor: JobContentPreprocessor):
    """CRITICAL: Verify compensation sentences are preserved VERBATIM for evidence extraction."""
    expected_compensation_sentence = (
        "The expected base salary range for this role is $185,000 - $235,000 per year, "
        "plus equity grant and full comprehensive healthcare benefits."
    )
    raw_html = f"""
    <div>
      <h2>Compensation &amp; Benefits</h2>
      <p>{expected_compensation_sentence}</p>
    </div>
    """
    result = preprocessor.preprocess(raw_html, is_html=True)
    clean_text = result["clean_text"]

    assert expected_compensation_sentence in clean_text, (
        "Evidence extraction failed: compensation sentence was altered or not preserved verbatim!"
    )


def test_verbatim_requirements_preservation(preprocessor: JobContentPreprocessor):
    """CRITICAL: Verify requirements bullet sentences are preserved VERBATIM for evidence extraction."""
    expected_req_1 = "7+ years of experience building resilient microservices using Python, Go, or Java."
    expected_req_2 = "Demonstrated expertise with Kafka, Apache Flink, or real-time event streaming architectures."

    raw_html = f"""
    <div class="job-requirements">
      <h3>Qualifications</h3>
      <ul>
        <li>{expected_req_1}</li>
        <li>{expected_req_2}</li>
      </ul>
    </div>
    """
    result = preprocessor.preprocess(raw_html, is_html=True)
    clean_text = result["clean_text"]

    assert expected_req_1 in clean_text, "Evidence extraction failed: requirement 1 was altered!"
    assert expected_req_2 in clean_text, "Evidence extraction failed: requirement 2 was altered!"


# -------------------------------------------------------------------------
# Test Suite 5: Plain Text (Hacker News) vs HTML (Greenhouse/Lever)
# -------------------------------------------------------------------------

def test_plain_text_hackernews_comment(preprocessor: JobContentPreprocessor):
    """Verify plain text processing handles mathematical operators and Hacker News formats."""
    hn_comment = (
        "Fly.io | Full-Stack & Systems Engineers | REMOTE (US & Canada) | Full-time | $160k - $210k + Equity\n\n"
        "We are looking for engineers with experience in Rust, Go, or Elixir.\n"
        "Requirements:\n"
        "- 3+ years experience with Linux systems programming\n"
        "- Salary expectation: > $160,000 and < $220,000 base pay\n"
        "- Tech stack: Go 1.22+, Rust, WireGuard, SQLite\n\n"
        "Apply by emailing jobs@fly.io with your resume."
    )

    result = preprocessor.preprocess(hn_comment, is_html=False)
    clean = result["clean_text"]

    # Verify math operators are NOT mangled as HTML tags
    assert "> $160,000 and < $220,000 base pay" in clean
    assert "Fly.io | Full-Stack & Systems Engineers" in clean
    assert "jobs@fly.io" in clean


def test_lever_ats_html_structure(preprocessor: JobContentPreprocessor):
    """Verify Lever ATS style HTML with div wrappers and custom classes."""
    lever_html = """
    <div class="section page-centered">
      <div class="section-wrapper">
        <div class="posting-headline">
          <h2>Senior Infrastructure Engineer</h2>
          <div class="posting-categories">
            <span class="location">San Francisco, CA</span>
            <span class="workplaceTypes">Hybrid</span>
            <span class="commitment">Full-time</span>
          </div>
        </div>
        <div class="section page-centered last-section-apply">
          <div><strong>About the Role:</strong></div>
          <div>We are seeking a Senior Infrastructure Engineer to lead our Kubernetes operations.</div>
          <div>&nbsp;</div>
          <div><strong>Requirements:</strong></div>
          <ul>
            <li>5+ years of experience managing production Kubernetes clusters.</li>
            <li>Proficiency in Terraform, AWS, and CI/CD pipelines.</li>
          </ul>
          <div>&nbsp;</div>
          <div><strong>Compensation:</strong></div>
          <div>Base pay range: $170,000 - $220,000 per year.</div>
        </div>
      </div>
    </div>
    """
    result = preprocessor.preprocess(lever_html, is_html=True)
    clean = result["clean_text"]

    assert "Senior Infrastructure Engineer" in clean
    assert "San Francisco, CA" in clean
    assert "About the Role:" in clean
    assert "5+ years of experience managing production Kubernetes clusters." in clean
    assert "Base pay range: $170,000 - $220,000 per year." in clean


# -------------------------------------------------------------------------
# Test Suite 6: Key Section Extraction and Boilerplate Removal
# -------------------------------------------------------------------------

def test_boilerplate_removal_when_exceeding_token_limit(preprocessor: JobContentPreprocessor):
    """Verify legal boilerplate (EEO, privacy, accommodations) is pruned when budget is exceeded."""
    eeo_boilerplate = (
        "Equal Opportunity Employer Statement:\n"
        "We are an equal opportunity employer and value diversity at our company. "
        "We do not discriminate on the basis of race, religion, color, national origin, "
        "gender, sexual orientation, age, marital status, veteran status, or disability status. "
        "All qualified applicants will receive consideration for employment without regard to race, "
        "color, religion, sex, sexual orientation, gender identity, or national origin. "
        "E-Verify: Federal law requires all employers to verify the identity and employment eligibility "
        "of all persons hired to work in the United States. San Francisco Fair Chance Ordinance Notice."
    )

    core_content = (
        "Role Overview:\n"
        "We are looking for a Lead AI Engineer to architect our enterprise LLM solutions.\n\n"
        "Requirements:\n"
        "• 6+ years of experience with Python, PyTorch, and deep learning architectures.\n"
        "• Hands-on experience fine-tuning large language models.\n\n"
        "Compensation:\n"
        "Annual base salary range is $210,000 - $275,000 plus substantial equity."
    )

    combined_text = f"{core_content}\n\n{eeo_boilerplate}"

    # Force a token limit that fits core_content but NOT both core_content and eeo_boilerplate
    core_tokens = preprocessor.estimate_tokens(core_content)
    total_tokens = preprocessor.estimate_tokens(combined_text)
    assert total_tokens > core_tokens

    # Set budget to fit core content only
    extracted = preprocessor.extract_key_sections(combined_text, max_tokens=core_tokens + 20)

    assert "Annual base salary range is $210,000 - $275,000" in extracted
    assert "6+ years of experience with Python, PyTorch" in extracted
    assert "We do not discriminate" not in extracted
    assert "San Francisco Fair Chance Ordinance" not in extracted
    assert "E-Verify" not in extracted


def test_force_strip_boilerplate(preprocessor: JobContentPreprocessor):
    """Verify force_strip_boilerplate removes EEO clauses even when within max_tokens budget."""
    text = (
        "Staff Site Reliability Engineer\n\n"
        "Compensation:\n"
        "$190,000 - $240,000.\n\n"
        "Equal Opportunity Employer:\n"
        "We do not discriminate on the basis of race, color, religion."
    )

    clean = preprocessor.extract_key_sections(text, max_tokens=2000, force_strip_boilerplate=True)
    assert "Staff Site Reliability Engineer" in clean
    assert "$190,000 - $240,000" in clean
    assert "We do not discriminate" not in clean


def test_preserves_content_when_within_token_limit(preprocessor: JobContentPreprocessor):
    """Verify that when content fits comfortably inside max_tokens, all sections are preserved."""
    text = (
        "Company: Acme Corp\n\n"
        "Role Overview:\n"
        "We are hiring a Senior Software Engineer.\n\n"
        "Requirements:\n"
        "• 4+ years of Python experience.\n\n"
        "Compensation:\n"
        "Salary: $140,000 - $180,000.\n\n"
        "Location:\n"
        "Remote (US)."
    )

    extracted = preprocessor.extract_key_sections(text, max_tokens=1500)
    assert "Company: Acme Corp" in extracted
    assert "We are hiring a Senior Software Engineer." in extracted
    assert "4+ years of Python experience." in extracted
    assert "Salary: $140,000 - $180,000." in extracted
    assert "Remote (US)." in extracted


# -------------------------------------------------------------------------
# Test Suite 7: Graceful Handling of Empty, None, and Malformed Inputs
# -------------------------------------------------------------------------

def test_empty_and_none_inputs(preprocessor: JobContentPreprocessor):
    """Verify None, empty string, and whitespace-only inputs are handled gracefully."""
    assert preprocessor.clean_html("") == ""
    assert preprocessor.clean_html("   \n\t  ") == ""
    assert preprocessor.clean_html(None) == ""

    assert preprocessor.clean_text("") == ""
    assert preprocessor.clean_text(None) == ""

    assert preprocessor.extract_key_sections("") == ""
    assert preprocessor.extract_key_sections(None) == ""

    res_empty = preprocessor.preprocess("")
    assert res_empty["clean_text"] == ""
    assert res_empty["original_length"] == 0
    assert res_empty["cleaned_length"] == 0
    assert res_empty["token_reduction_ratio"] == 1.0

    res_none = preprocessor.preprocess(None)
    assert res_none["clean_text"] == ""
    assert res_none["original_length"] == 0
    assert res_none["cleaned_length"] == 0


def test_malformed_html_inputs(preprocessor: JobContentPreprocessor):
    """Verify severely broken or unclosed HTML tags do not cause crashes."""
    malformed_html = (
        "<div><p>Broken paragraph without closing tag"
        "<h2>Title without closing tag"
        "<ul><li>Item 1<li>Item 2 without closing li"
        "<p>Compensation: $150,000 - $180,000 per year"
    )
    clean = preprocessor.clean_html(malformed_html)

    assert "Broken paragraph without closing tag" in clean
    assert "Title without closing tag" in clean
    assert "Item 1" in clean
    assert "Item 2 without closing li" in clean
    assert "Compensation: $150,000 - $180,000 per year" in clean


# -------------------------------------------------------------------------
# Test Suite 8: Preprocess Metrics and Token Reduction Ratio
# -------------------------------------------------------------------------

def test_preprocess_token_reduction_ratio(preprocessor: JobContentPreprocessor):
    """Verify preprocessing metrics accurately calculate original_length and reduction ratio."""
    verbose_html = """
    <html>
      <head>
        <style>
          .job-title { font-size: 24px; font-weight: bold; margin-bottom: 12px; }
          .apply-btn { background: #0070f3; color: white; padding: 10px 20px; }
          .sidebar { width: 300px; display: flex; flex-direction: column; }
        </style>
        <script>
          window.__INITIAL_STATE__ = { "user": null, "trackingId": "xyz123", "experiments": [] };
          console.log("Telemetry started for job page");
        </script>
      </head>
      <body>
        <header><nav><a href="/">Home</a><a href="/jobs">Jobs</a></nav></header>
        <div class="job-container">
          <h1 class="job-title">Staff Cloud Security Engineer</h1>
          <p>We are hiring a Staff Cloud Security Engineer to safeguard our global payment systems.</p>
          <h2>Requirements:</h2>
          <ul>
            <li>6+ years of experience in AWS or GCP cloud security architectures.</li>
            <li>In-depth knowledge of IAM, Kubernetes security, and zero-trust networks.</li>
          </ul>
          <h2>Compensation &amp; Location:</h2>
          <p>Base salary: $200,000 - $260,000 per year. Work arrangement: US Remote.</p>
        </div>
        <footer>
          <p>Copyright 2026. All rights reserved.</p>
        </footer>
      </body>
    </html>
    """
    res = preprocessor.preprocess(verbose_html, is_html=True)

    assert "clean_text" in res
    assert "original_length" in res
    assert "cleaned_length" in res
    assert "token_reduction_ratio" in res

    assert res["original_length"] == len(verbose_html)
    assert res["cleaned_length"] == len(res["clean_text"])
    expected_ratio = round(1.0 - (res["cleaned_length"] / max(1, res["original_length"])), 4)
    assert res["token_reduction_ratio"] == expected_ratio

    # Significant token savings from eliminating scripts, styles, and HTML tags
    assert res["token_reduction_ratio"] >= 0.40


def test_clean_and_call_interface(preprocessor: JobContentPreprocessor):
    """Verify preprocessor.clean() and preprocessor() callable interface."""
    html_content = "<p>Data Engineer</p>"
    plain_content = "Software Engineer\n$150k"

    assert preprocessor.clean(html_content) == "Data Engineer"
    assert preprocessor(html_content) == "Data Engineer"
    assert preprocessor.clean(plain_content) == "Software Engineer\n$150k"
    assert preprocessor(plain_content) == "Software Engineer\n$150k"


# -------------------------------------------------------------------------
# Test Suite 9: Integration with Real Database Postings (if available)
# -------------------------------------------------------------------------

def test_real_database_cached_jobs_integration(preprocessor: JobContentPreprocessor):
    """Verify preprocessor handles actual scraped payloads stored in pipeline.db."""
    db_path = Path("D:/Project_Files/AGY_Assignments/Twig/Code/data/pipeline.db")
    if not db_path.exists():
        pytest.skip("pipeline.db not found on test machine")

    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()

    cur.execute("SELECT source, payload FROM raw_cache WHERE source in ('greenhouse', 'lever') LIMIT 4")
    rows = cur.fetchall()
    conn.close()

    if not rows:
        pytest.skip("No greenhouse/lever rows found in raw_cache")

    for source, payload in rows:
        import json
        try:
            data = json.loads(payload)
            raw_text = data.get("content") or data.get("description") or payload
        except Exception:
            raw_text = payload

        result = preprocessor.preprocess(raw_text, is_html=True)
        assert result["cleaned_length"] > 0
        assert len(result["clean_text"]) > 0
        assert result["token_reduction_ratio"] >= 0.0
