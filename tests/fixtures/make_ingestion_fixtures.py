"""Builds tests/fixtures/ingestion/ and ingestion_baseline.json.

Every document here is written for this repo, so the fixtures can be shared
freely. Each one keeps the feature its tests exercise:

- citation_guide.pdf: every page's text is inside an image, with only a
  short footer as real text, so every page is flagged and OCR has to
  recover the content (page 8, choosing a citation style, exists only in
  its image).
- ai_assistant_slides.pptx: 15 slides, pictures on slides 9 to 12.
- research_methods.pdf: mostly plain text; page 8 is a short caption with a
  chart, page 10 is a chart alone, so both are flagged.
- dense_article.pdf: 15 pages of dense prose, none flagged.

Run from the repo root (reportlab is only needed here, not by the tests):

    uv run --with reportlab==4.2.5 python tests/fixtures/make_ingestion_fixtures.py
"""

import io
import json
import sys
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from pptx import Presentation
from pptx.util import Inches, Pt
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "ingestion"
sys.path.insert(0, str(ROOT.parent.parent / "plugin" / "server"))

WIDTH, HEIGHT = letter


def text_image(title: str, body: str, width: int = 1500) -> Image.Image:
    """Black text on white, large enough for Tesseract at 200 dpi."""
    title_font = ImageFont.load_default(size=56)
    body_font = ImageFont.load_default(size=36)
    lines = [line for para in body.split("\n") for line in (textwrap.wrap(para, 70) or [""])]
    height = 140 + 52 * len(lines) + 60
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    draw.text((60, 40), title, fill="black", font=title_font)
    for i, line in enumerate(lines):
        draw.text((60, 140 + 52 * i), line, fill="black", font=body_font)
    return img


def chart_image(values: list[int], width: int = 900, height: int = 500) -> Image.Image:
    """A bar chart with no text, so OCR adds nothing from it."""
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    bar = width // (2 * len(values) + 1)
    for i, v in enumerate(values):
        x = bar * (2 * i + 1)
        draw.rectangle([x, height - 40 - v * 4, x + bar, height - 40], fill=(60, 110, 180))
    draw.line([30, height - 40, width - 30, height - 40], fill="black", width=3)
    return img


def png(img: Image.Image) -> ImageReader:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return ImageReader(buf)


def draw_paragraphs(c: canvas.Canvas, paragraphs: list[str], top: float, size: int = 10, wrap: int = 98) -> float:
    c.setFont("Helvetica", size)
    y = top
    for para in paragraphs:
        for line in textwrap.wrap(para, wrap):
            c.drawString(54, y, line)
            y -= size * 1.35
        y -= size * 0.8
    return y


# --- citation_guide.pdf ---------------------------------------------------------

CITATION_PAGES = [
    ("Citations and Bibliographies with Zotero",
     "A short library workshop guide.\nWhat Zotero is, how to install it, and how to keep your sources organized."),
    ("Contents",
     "1. Installation\n2. The browser connector\n3. Collections\n4. Adding sources\n5. Notes and tags\n"
     "6. Choosing a citation style\n7. Citing while you write\n8. Building the bibliography"),
    ("Installation",
     "Download the Zotero desktop app from zotero.org and run the installer for your system. "
     "Open the app once so it can create your library folder before you add anything."),
    ("Installation: the connector",
     "Download the Zotero connector for your browser from zotero.org and add it to the browser. "
     "The connector saves a page or a paper to your library with one click on its toolbar button."),
    ("Creating a collection",
     "A collection is a folder for one project. To create a collection, click the New Collection button "
     "above the left pane, give it a name such as Thesis, and drag sources into it. "
     "A source can sit in several collections at once."),
    ("Adding sources",
     "Paste a DOI or ISBN with the magic wand button, drop a PDF onto the library, or save a page with "
     "the connector. Zotero fills in the author, title and year for you; check them before citing."),
    ("Notes and tags",
     "Add a note to any source to keep your own summary next to it. Tags such as methods or to read "
     "let you filter the library later."),
    ("Choosing a citation style",
     "Use the style your instructor or journal asks for. If nobody specifies one, APA 7th edition is a "
     "safe choice for the social sciences and IEEE for engineering. Change the default under Settings, "
     "Cite, and every citation updates to the new style."),
    ("Citing while you write",
     "Install the word processor plugin from Settings, Cite. In your document, click Add Citation, "
     "search for the source, and press Enter."),
    ("Building the bibliography",
     "Click Add Bibliography where the reference list should go. It updates itself whenever you add "
     "or remove a citation."),
    ("Questions", "Ask at the library help desk or book a research consultation."),
]


def make_citation_guide(path: Path) -> None:
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setTitle("Citations and Bibliographies with Zotero")
    for i, (title, body) in enumerate(CITATION_PAGES, 1):
        img = text_image(title, body)
        w = WIDTH - 72
        h = w * img.height / img.width
        c.drawImage(png(img), 36, HEIGHT - 72 - h, width=w, height=h)
        c.setFont("Helvetica", 9)
        c.drawString(36, 30, f"Library workshop, page {i}")
        c.showPage()
    c.save()


# --- research_methods.pdf -------------------------------------------------------

RESEARCH_SECTIONS = [
    ("Research Methods for Software Projects",
     ["This handout walks through how a course project turns a question into evidence. It covers framing "
      "the question, finding prior work, choosing a method, and reporting what was found.",
      "Each section ends with a checklist you can use before a project milestone."]),
    ("Framing the question",
     ["A good research question is narrow enough to answer in a semester and specific enough that two "
      "people would agree on whether it was answered. Replace words like better or faster with a measure "
      "you can collect, such as task completion time or the number of defects found.",
      "Write the question down before reading widely, then revise it once you know what has been done."]),
    ("Finding prior work",
     ["Start with two or three recent survey papers and follow their references backward. Then search the "
      "major venues of the field for the last five years. Keep every paper you read in a reference manager "
      "from the first day, with a one-line note on why it matters.",
      "Search with several phrasings of the same idea: authors in different communities name the same "
      "concept differently. Record the exact search strings and the date, so the search can be repeated.",
      "Stop when new searches keep returning papers you have already seen. That saturation, not a fixed "
      "number of papers, is the signal that the review is complete enough for a course project.",
      "Read the abstract, the figures and the conclusion first. Read the full paper only when it is close "
      "to your question or when you plan to reuse its method."]),
    ("Choosing a method",
     ["Controlled experiments answer whether one condition causes a difference in a measure. Surveys and "
      "interviews answer what people think and why. Case studies follow one system or team in depth. "
      "Mining software repositories answers questions about how code and projects change over time.",
      "Pick the method from the question, not from what is easiest to run. A question about causes needs a "
      "comparison; a question about experience needs people's own words.",
      "Whatever the method, decide before collecting data what result would count against your hypothesis. "
      "Writing that down protects the analysis from being bent to fit the data afterward.",
      "Plan the sample size early. A pilot with a handful of participants shows whether the tasks and "
      "instructions work before the real study uses up your participant pool."]),
    ("Threats to validity",
     ["Internal validity asks whether something other than your intervention could explain the result. "
      "External validity asks whether the result would hold for other people, systems or settings.",
      "Construct validity asks whether your measure captures the concept you care about. Lines of code are a "
      "poor stand-in for effort; defects found per hour is closer to testing effectiveness."]),
    ("Ethics and consent",
     ["Studies with people need approval from the review board before any data is collected. Participants "
      "must know what they are agreeing to, and that they can stop at any time without penalty.",
      "Store recordings and personal data on approved storage only, and remove names before sharing "
      "transcripts with the rest of the team."]),
    ("Reporting results",
     ["Report what you planned, what you did, and where the two differed. Show the raw distribution before "
      "any average, and give effect sizes alongside significance tests.",
      "A result that did not support the hypothesis is still a result. Say what it rules out."]),
    ("Figure 1",
     ["Figure 1. Participants per study condition in last year's course projects. "
      "Most projects used fewer than twenty participants per condition."]),
    ("Checklist",
     ["Before the proposal milestone: the question names a measure, the related work section cites at least "
      "the main survey papers, and the method section says what result would count against the hypothesis."]),
    (None, []),
]


def make_research_methods(path: Path) -> None:
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setTitle("Research Methods for Software Projects")
    for i, (title, paragraphs) in enumerate(RESEARCH_SECTIONS, 1):
        if title:
            c.setFont("Helvetica-Bold", 16)
            c.drawString(54, HEIGHT - 72, title)
            draw_paragraphs(c, paragraphs, HEIGHT - 104, size=11, wrap=88)
        if i in (8, 10):
            img = chart_image([12, 18, 9, 25, 15, 7])
            c.drawImage(png(img), 90, 200, width=WIDTH - 180, height=(WIDTH - 180) * img.height / img.width)
        c.showPage()
    c.save()


# --- dense_article.pdf ----------------------------------------------------------

ARTICLE = [
    "Spaced practice, the habit of returning to material several times with gaps in between, is one of the "
    "most reliable findings about how people learn. Students who spread the same amount of study over several "
    "sessions remember more weeks later than students who study it all at once, even when the single session "
    "felt more productive at the time.",
    "The effect has been measured with vocabulary lists, mathematics problems, programming exercises and "
    "motor skills. It holds for children and adults, in laboratories and in classrooms, and it grows as the "
    "delay before the final test grows. The size of the ideal gap depends on how long the material must be "
    "remembered: a test next week calls for gaps of a day or two, a final exam months away for gaps of weeks.",
    "Retrieval practice adds to the benefit. Trying to recall an answer before looking at it strengthens the "
    "memory more than reading the answer again, and the effort of recall is part of why it works. Practice "
    "tests, flash cards and explaining a concept from memory are all forms of retrieval practice.",
    "Interleaving, mixing different kinds of problems within one session, helps when students must later "
    "choose which method to use. Blocked practice, many problems of one kind in a row, feels smoother but "
    "hides the step of recognizing the problem type, which is exactly the step an exam requires.",
    "Students tend to judge their learning by how fluent the material feels during study. Fluency is a poor "
    "guide: rereading a chapter makes it feel familiar without making it easier to recall. Strategies that "
    "feel harder in the moment, such as spacing, testing and interleaving, often produce better long-term "
    "results, a pattern sometimes described as desirable difficulty.",
    "Course design can build these strategies in. Weekly low-stakes quizzes provide spaced retrieval practice "
    "without adding much grading work. Cumulative assignments that revisit earlier topics space the material "
    "automatically. Problem sets that mix topics from several weeks interleave practice.",
    "There are limits. Spacing does little for material that was never understood in the first place, and "
    "retrieval practice works best when students get feedback on their answers. Very long gaps can mean the "
    "material is forgotten entirely before it is reviewed, so the first review should come soon after the "
    "first study session.",
    "For a semester course, a practical schedule is to review new material the day after it is introduced, "
    "again at the end of the week, and again a few weeks later, each time by attempting problems before "
    "looking at worked solutions. Keeping a short list of questions missed on earlier reviews focuses later "
    "sessions on what is still weak.",
]


def make_dense_article(path: Path) -> None:
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setTitle("Spacing and Retrieval in Course Design")
    for i in range(15):
        c.setFont("Helvetica-Bold", 13)
        c.drawString(54, HEIGHT - 60, f"Spacing and Retrieval in Course Design, part {i + 1}")
        start = i % len(ARTICLE)
        paragraphs = [ARTICLE[(start + k) % len(ARTICLE)] for k in range(4 + i % 3)]
        draw_paragraphs(c, paragraphs, HEIGHT - 88, size=9, wrap=118)
        c.showPage()
    c.save()


# --- ai_assistant_slides.pptx ---------------------------------------------------

SLIDES = [
    ("Building a Course Research Assistant", "Workshop slides"),
    ("Agenda", "Why an assistant\nRetrieval basics\nIndexing course files\nAnswering with citations"),
    ("Why an assistant?", "Students ask the same questions every term."),
    ("What it should do",
     "Answer questions from the course's own files.\nCite the page or slide every answer comes from.\n"
     "Say when the material does not cover a question instead of guessing.\nNever do graded work for the student."),
    ("Retrieval basics",
     "Split each document into chunks.\nEmbed every chunk as a vector.\nAt question time, embed the question and "
     "find the nearest chunks."),
    ("Keyword search still matters",
     "Vector search finds related ideas.\nKeyword search finds exact names and course codes.\n"
     "Combining both with rank fusion gets the best of each."),
    ("Chunking", "One chunk per slide or page."),
    ("Chunk size",
     "Keep chunks under the embedding model's token limit.\nCount tokens with the model's own tokenizer.\n"
     "Overlap long chunks slightly so a sentence is never cut off from its context."),
    ("What is a vector database?",
     "A vector database stores embeddings next to their text and metadata.\n"
     "It answers nearest-neighbor queries quickly, even over millions of chunks.\n"
     "LanceDB keeps the whole index in local files, so nothing leaves the student's computer."),
    ("Indexing pipeline",
     "Download the course files.\nExtract text from PDF, slides and pages.\nRun OCR where a page is mostly images.\n"
     "Embed and store every chunk."),
    ("Handling scanned pages",
     "A page with little text and an image is probably a scan or a screenshot.\nOCR reads the text inside the "
     "image.\nOnly lines that add new words are kept, so OCR mistakes do not replace good text."),
    ("Answering with citations",
     "Pass the retrieved chunks with their source labels.\nAsk for a citation after every claim.\n"
     "Link each citation to the page in the course site."),
    ("Evaluating answers",
     "Write questions with known answers from the syllabus.\nCheck that the right chunk is retrieved.\n"
     "Check that the answer cites it."),
    ("Common failures",
     "Dates written in relative terms do not match the syllabus.\nTables lose their labels in extraction.\n"
     "Very short chunks score poorly in vector search."),
    ("Next steps",
     "Index one course end to end.\nCollect real student questions for a week.\nMeasure retrieval before tuning "
     "anything.\nAdd a reranker only if the measurements show it helps.\nShare the results with the course staff."),
]


def make_slides(path: Path) -> None:
    prs = Presentation()
    layout = prs.slide_layouts[1]
    for i, (title, body) in enumerate(SLIDES, 1):
        slide = prs.slides.add_slide(layout)
        slide.shapes.title.text = title
        slide.placeholders[1].text = body
        if 9 <= i <= 12:
            buf = io.BytesIO()
            chart_image([8, 14, 11, 20], width=600, height=340).save(buf, format="PNG")
            buf.seek(0)
            slide.shapes.add_picture(buf, Inches(6.2), Inches(5.0), width=Inches(3.4))
            slide.placeholders[1].text_frame.paragraphs[0].runs[0].font.size = Pt(20)
    prs.core_properties.author = "canvas-study tests"
    prs.core_properties.title = "Building a Course Research Assistant"
    prs.save(str(path))


# --- baseline ---------------------------------------------------------------------


def baseline() -> dict:
    import ingestion

    out = {}
    pages = ingestion.extract_pdf(OUT / "citation_guide.pdf")
    out["citation_guide.pdf"] = {
        "pages": len(pages),
        "char_counts": [p["char_count"] for p in pages],
        "flagged": [p["needs_fallback"] for p in pages],
        "ocr_total_chars": sum(len(ingestion.ocr_pdf_page(OUT / "citation_guide.pdf", p["page"])) for p in pages),
    }
    slides = ingestion.extract_pptx(OUT / "ai_assistant_slides.pptx")
    out["ai_assistant_slides.pptx"] = {
        "slides": len(slides),
        "char_counts": [s["char_count"] for s in slides],
        "has_image": [s["has_image"] for s in slides],
    }
    for name in ("research_methods.pdf", "dense_article.pdf"):
        pages = ingestion.extract_pdf(OUT / name)
        out[name] = {
            "pages": len(pages),
            "char_counts": [p["char_count"] for p in pages],
            "flagged": [p["needs_fallback"] for p in pages],
        }
    return out


def main() -> None:
    OUT.mkdir(exist_ok=True)
    make_citation_guide(OUT / "citation_guide.pdf")
    make_research_methods(OUT / "research_methods.pdf")
    make_dense_article(OUT / "dense_article.pdf")
    make_slides(OUT / "ai_assistant_slides.pptx")
    (ROOT / "ingestion_baseline.json").write_text(json.dumps(baseline(), indent=2) + "\n")


if __name__ == "__main__":
    main()
