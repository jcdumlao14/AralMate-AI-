import html
import json
import os
import re
import uuid
from typing import Any

import fitz  # PyMuPDF
import streamlit as st
from dotenv import load_dotenv
from openai import OpenAI
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

# -----------------------------------------------------------------------------
# AralMate AI — document-grounded study companion
# Groq is accessed through its OpenAI-compatible Chat Completions endpoint.
# -----------------------------------------------------------------------------

load_dotenv(override=False)

st.set_page_config(
    page_title="AralMate AI | Study Workspace",
    page_icon="📚",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ------------------------------- BRAND DESIGN --------------------------------
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Manrope:wght@500;600;700;800&display=swap');
    html, body, [class*="css"] { font-family: 'DM Sans', sans-serif; }
    .stApp { background: #F7F8F3; }
    [data-testid="stSidebar"] { background: #173F35; }
    [data-testid="stSidebar"] * { color: #F5F5E9 !important; }
    .hero {
        background: linear-gradient(120deg, #173F35, #286B54);
        color: white; padding: 30px; border-radius: 22px; margin-bottom: 22px;
    }
    .hero h1 { font-family: 'Manrope', sans-serif; font-size: 34px; margin: 0 0 8px 0; }
    .hero p { color: #E0EBDD; font-size: 16px; margin: 0; }
    .metric-card, .feature-card, .study-card {
        background: white; border: 1px solid #E8EBE3; border-radius: 18px; padding: 20px;
    }
    .metric-label { color: #758178; font-size: 12px; letter-spacing: .08em; font-weight: 700; }
    .metric-value { color: #173F35; font-family: 'Manrope', sans-serif; font-size: 29px; font-weight: 800; }
    .section-title { color: #173F35; font-family: 'Manrope', sans-serif; font-weight: 800; }
    .muted { color: #758178; }
    .study-card { min-height: 170px; }
    div.stButton > button { border-radius: 12px; border: 1px solid #DCE5D9; font-weight: 600; }
    div.stButton > button[kind="primary"] { background: #286B54; color: white; border: none; }
    .stTextInput input, .stTextArea textarea { border-radius: 12px; }
    [data-testid="stChatMessage"] { border-radius: 16px; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ------------------------------ CONFIGURATION --------------------------------

def get_config_value(name: str, default: str | None = None) -> str | None:
    """Read configuration from environment variables or Streamlit Secrets."""
    value = os.getenv(name)

    if value:
        return value

    try:
        value = st.secrets.get(name)
    except Exception:
        value = None

    return str(value) if value else default


GROQ_BASE_URL = "https://api.groq.com/openai/v1"

GROQ_API_KEY = (
    get_config_value("GROQ_API_KEY")
    or get_config_value("OPENAI_API_KEY")
)

GROQ_MODEL = (
    get_config_value("GROQ_MODEL")
    or get_config_value("OPENAI_MODEL")
    or "openai/gpt-oss-20b"
)

CUSTOM_BASE_URL = (
    get_config_value("GROQ_BASE_URL")
    or get_config_value("OPENAI_BASE_URL")
)

API_BASE_URL = CUSTOM_BASE_URL or GROQ_BASE_URL
def get_client() -> OpenAI | None:
    """Return an OpenAI SDK client configured for Groq, if a key exists."""
    if not GROQ_API_KEY:
        return None
    return OpenAI(api_key=GROQ_API_KEY, base_url=API_BASE_URL, timeout=60.0)


def call_model(prompt: str, system_prompt: str | None = None, max_tokens: int = 1800) -> str:
    """Call the configured Groq chat model and return its text response."""
    client = get_client()
    if client is None:
        raise RuntimeError("Groq API key is not configured. Set GROQ_API_KEY in your .env file or Streamlit Secrets.")

    response = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {
                "role": "system",
                "content": system_prompt
                or "You are AralMate AI, a careful and encouraging study tutor. Be accurate and clear.",
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
        max_tokens=max_tokens,
    )
    content = response.choices[0].message.content

    if not content or not content.strip():
        raise RuntimeError("The AI provider returned an empty response.")

    return content.strip()


# ------------------------------ SESSION STATE --------------------------------
def init_state() -> None:
    defaults = {
        "pages": [],
        "filename": "",
        "chunks": [],
        "messages": [],
        "quiz": [],
        "quiz_id": 0,
        "quiz_submitted": False,
        "quiz_results": [],
        "score": None,
        "quiz_history": [],
        "flashcards": [],
        "flashcard_index": 0,
        "flashcard_revealed": False,
        "document_id": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


init_state()


# ----------------------------- DOCUMENT LOGIC --------------------------------
def extract_pdf(uploaded_file: Any) -> list[dict[str, Any]]:
    """Extract text from each page while retaining original page numbers."""
    raw = uploaded_file.getvalue()
    document = fitz.open(stream=raw, filetype="pdf")
    extracted_pages: list[dict[str, Any]] = []
    try:
        for page_number, page in enumerate(document, start=1):
            page_text = page.get_text("text").strip()
            if page_text:
                extracted_pages.append({"page": page_number, "text": page_text})
    finally:
        document.close()
    return extracted_pages


def split_into_sentences(text: str) -> list[str]:
    """A lightweight sentence splitter that keeps punctuation with each sentence."""
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return []
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9(\"'])", normalized) if part.strip()]


def make_chunks(pages: list[dict[str, Any]], max_chars: int = 1100, overlap_sentences: int = 1) -> list[dict[str, Any]]:
    """Build sentence-aware chunks per page, preserving page references."""
    chunks: list[dict[str, Any]] = []
    for page in pages:
        sentences = split_into_sentences(page["text"])
        if not sentences:
            continue
        current: list[str] = []
        current_length = 0
        for sentence in sentences:
            extra_length = len(sentence) + (1 if current else 0)
            if current and current_length + extra_length > max_chars:
                chunk_text = " ".join(current).strip()
                chunks.append({"page": page["page"], "text": chunk_text})
                overlap = current[-overlap_sentences:] if overlap_sentences else []
                current = list(overlap)
                current_length = len(" ".join(current))
            current.append(sentence)
            current_length = len(" ".join(current))
        if current:
            chunks.append({"page": page["page"], "text": " ".join(current).strip()})
    return chunks


def retrieve(question: str, chunks: list[dict[str, Any]], top_k: int = 5) -> list[dict[str, Any]]:
    """Retrieve the most relevant chunks with TF-IDF cosine similarity."""
    if not chunks or not question.strip():
        return []
    texts = [item["text"] for item in chunks]
    try:
        vectorizer = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), sublinear_tf=True)
        matrix = vectorizer.fit_transform(texts + [question])
        scores = cosine_similarity(matrix[-1], matrix[:-1]).ravel()
    except ValueError:
        return []
    ranked = scores.argsort()[::-1]
    results = []
    for index in ranked:
        if scores[index] <= 0:
            continue
        item = dict(chunks[int(index)])
        item["score"] = float(scores[index])
        results.append(item)
        if len(results) >= top_k:
            break
    return results


def document_context(chunks: list[dict[str, Any]], limit: int = 14, max_chars: int = 12000) -> str:
    """Prepare a bounded source context for generation tasks."""
    selected = chunks[:limit]
    pieces: list[str] = []
    total = 0
    for item in selected:
        piece = f"[Page {item['page']}] {item['text']}"
        if total + len(piece) > max_chars:
            remaining = max_chars - total
            if remaining > 150:
                pieces.append(piece[:remaining])
            break
        pieces.append(piece)
        total += len(piece)
    return "\n\n".join(pieces)


# ------------------------------- JSON HELPERS --------------------------------
def parse_json_response(text: str) -> Any:
    """Parse JSON from plain output or a fenced JSON code block."""
    cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip(), flags=re.IGNORECASE)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for start, char in enumerate(cleaned):
            if char in "[{":
                try:
                    value, _ = decoder.raw_decode(cleaned[start:])
                    return value
                except json.JSONDecodeError:
                    continue
    raise ValueError("The AI response was not valid JSON. Please generate again.")


def validate_quiz(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        data = data.get("questions", [])

    if not isinstance(data, list) or len(data) < 3:
        raise ValueError(
            "The AI did not return enough quiz questions. Please generate again."
        )

    validated = []

    for item in data[:5]:
        if not isinstance(item, dict):
            continue

        question = str(item.get("question", "")).strip()
        options = item.get("options", [])
        answer = str(item.get("answer", "")).strip()
        explanation = str(item.get("explanation", "")).strip()

        if not question or not isinstance(options, list) or len(options) != 4:
            continue

        options = [str(option).strip() for option in options]
        normalized = [option.casefold() for option in options]

        if (
            any(not option for option in options)
            or len(set(normalized)) != 4
        ):
            continue

        if answer not in options:
            continue

        validated.append({
            "question": question,
            "options": options,
            "answer": answer,
            "explanation": (
                explanation
                or "Review the cited study material to understand this answer."
            ),
            "topic": (
                str(item.get("topic", "General understanding")).strip()
                or "General understanding"
            ),
        })

    if len(validated) < 3:
        raise ValueError(
            "The generated quiz did not pass validation. Please try again."
        )

    return validated


def validate_flashcards(data: Any) -> list[dict[str, str]]:
    if isinstance(data, dict):
        data = data.get("flashcards", [])

    if not isinstance(data, list):
        raise ValueError("The AI did not return a flashcard list.")

    cards = []
    seen_fronts = set()

    for item in data:
        if not isinstance(item, dict):
            continue

        front = str(item.get("front", "")).strip()
        back = str(item.get("back", "")).strip()

        if not front or not back:
            continue

        normalized_front = " ".join(front.casefold().split())

        if normalized_front in seen_fronts:
            continue

        seen_fronts.add(normalized_front)
        cards.append({"front": front, "back": back})

        if len(cards) == 10:
            break

    if not cards:
        raise ValueError(
            "No valid flashcards were returned. Please try again."
        )

    return cards


# --------------------------------- SIDEBAR ------------------------------------
with st.sidebar:
    st.markdown("## 📚 AralMate AI")
    st.caption("YOUR PERSONAL STUDY COMPANION")
    st.divider()
    page = st.radio(
        "WORKSPACE",
        [
            "🏠 Dashboard",
            "📂 My Library",
            "🤖 AI Tutor",
            "📝 Practice Studio",
            "🗂️ Flashcards",
            "🧭 Knowledge Gap Map",
        ],
        label_visibility="visible",
    )
    st.divider()
    if st.session_state.filename:
        st.caption("CURRENT STUDY MATERIAL")
        st.write(f"📘 {st.session_state.filename}")
    else:
        st.caption("NO DOCUMENT UPLOADED YET")
    st.divider()
    if GROQ_API_KEY:
        st.success("AI provider configured")
        st.caption(f"Model: {GROQ_MODEL}")
    else:
        st.warning("AI provider not configured")
        st.caption("PDF retrieval works now. Add a Groq key to enable generation.")
    st.caption("LEARN • PRACTICE • IMPROVE")


# -------------------------------- DASHBOARD -----------------------------------
if page == "🏠 Dashboard":
    st.markdown(
        "<div class='hero'><h1>Learn smarter, one step at a time. 🌱</h1>"
        "<p>Your study materials, AI tutor, quizzes, and flashcards — all in one learning workspace.</p></div>",
        unsafe_allow_html=True,
    )
    col1, col2, col3, col4 = st.columns(4)
    metrics = [
        ("STUDY MATERIAL", "1" if st.session_state.pages else "0", "Uploaded documents"),
        ("PAGES PROCESSED", str(len(st.session_state.pages)), "Readable PDF pages"),
        ("SEARCHABLE SECTIONS", str(len(st.session_state.chunks)), "Document passages"),
        ("LATEST QUIZ SCORE", str(st.session_state.score) if st.session_state.score is not None else "—", "Based on your last quiz"),
    ]
    for column, (label, value, caption) in zip([col1, col2, col3, col4], metrics):
        with column:
            st.markdown(
                f"<div class='metric-card'><div class='metric-label'>{label}</div>"
                f"<div class='metric-value'>{value}</div><div class='muted'>{caption}</div></div>",
                unsafe_allow_html=True,
            )
    st.write("")
    st.markdown("### Your learning workspace")
    cards = st.columns(2)
    with cards[0]:
        st.markdown("<div class='study-card'><h3>📂 My Library</h3><p>Upload a PDF and turn your learning material into a searchable study space.</p></div>", unsafe_allow_html=True)
        st.markdown("<div class='study-card'><h3>🤖 AI Tutor</h3><p>Ask questions and receive explanations grounded in your uploaded material.</p></div>", unsafe_allow_html=True)
        st.markdown("<div class='study-card'><h3>🧭 Knowledge Gap Map</h3><p>See topics that need more attention based on your quiz attempts.</p></div>", unsafe_allow_html=True)
    with cards[1]:
        st.markdown("<div class='study-card'><h3>📝 Practice Studio</h3><p>Generate questions, check your understanding, and learn from explanations.</p></div>", unsafe_allow_html=True)
        st.markdown("<div class='study-card'><h3>🗂️ Flashcards</h3><p>Review important concepts with interactive question-and-answer cards.</p></div>", unsafe_allow_html=True)
    if not st.session_state.pages:
        st.info("Start by opening **My Library** and uploading a text-based PDF.")
    if not GROQ_API_KEY:
        st.warning("Groq is not connected yet. You can still upload and search PDFs; AI-generated features will activate after you add your API key.")


# ---------------------------------- LIBRARY -----------------------------------
elif page == "📂 My Library":
    st.markdown("<h2 class='section-title'>My Learning Library</h2>", unsafe_allow_html=True)
    st.write("Upload a PDF to create your personal study space.")
    upload = st.file_uploader("Choose a study PDF", type=["pdf"], help="Text-based PDFs work best. Scanned PDFs need OCR, which is not included in this MVP.")
    if upload and st.button("Process my document", type="primary"):
        with st.spinner("Reading and organizing your PDF..."):
            try:
                pages = extract_pdf(upload)
                chunks = make_chunks(pages)
                if not chunks:
                    st.error("No readable text was found. This may be a scanned PDF. Try a text-based PDF.")
                else:
                    st.session_state.pages = pages
                    st.session_state.chunks = chunks
                    st.session_state.filename = upload.name
                    st.session_state.document_id = str(uuid.uuid4())
                    st.session_state.messages = []
                    st.session_state.quiz = []
                    st.session_state.quiz_submitted = False
                    st.session_state.quiz_results = []
                    st.session_state.score = None
                    st.session_state.flashcards = []
                    st.session_state.flashcard_index = 0
                    st.session_state.flashcard_revealed = False
                    st.success("Your document is ready!")
            except Exception as exc:
                st.error(f"Could not process this PDF: {exc}")
    if st.session_state.pages:
        st.markdown("### Your uploaded material")
        st.write(f"📘 **{st.session_state.filename}**")
        st.write(f"{len(st.session_state.pages)} readable pages • {len(st.session_state.chunks)} searchable sections")
        with st.expander("Preview extracted text"):
            for item in st.session_state.pages[:3]:
                st.markdown(f"**Page {item['page']}**")
                st.write(item["text"][:1800])
        if st.button("Remove current study material"):
            for key, value in {
                "pages": [], "filename": "", "chunks": [], "messages": [],
                "quiz": [], "quiz_submitted": False, "quiz_results": [], "score": None,
                "flashcards": [], "flashcard_index": 0,
                "flashcard_revealed": False, "document_id": None,
            }.items():
                st.session_state[key] = value
            st.rerun()


# --------------------------------- AI TUTOR ----------------------------------
elif page == "🤖 AI Tutor":
    st.markdown("<h2 class='section-title'>Your AI Study Tutor</h2>", unsafe_allow_html=True)
    st.caption("Ask questions about your uploaded study material. Answers are grounded in retrieved passages.")
    if not st.session_state.chunks:
        st.warning("Upload a PDF in My Library first.")
    else:
        if not GROQ_API_KEY:
            st.info("Your document is ready for searching. Add a Groq API key to enable AI-generated explanations; retrieved source passages will still be available.")
        for message in st.session_state.messages:
            with st.chat_message(message["role"]):
                st.markdown(message["content"])
        question = st.chat_input("What would you like to understand?")
        if question:
            st.session_state.messages.append({"role": "user", "content": question})
            with st.chat_message("user"):
                st.markdown(question)
            sources = retrieve(question, st.session_state.chunks, top_k=5)
            with st.chat_message("assistant"):
                if not sources:
                    answer = "I couldn't find relevant supporting text in your uploaded document. Try rephrasing your question using terms from the material."
                else:
                    source_context = "\n\n".join(f"[Page {source['page']}] {source['text']}" for source in sources)
                    prompt = (
                        "Answer the student's question using ONLY the supplied study material. "
                        "Explain the idea in clear, student-friendly language. If the material does not support an answer, say so plainly. "
                        "Cite page numbers inline, for example (Page 2). Do not invent facts or citations.\n\n"
                        f"STUDY MATERIAL:\n{source_context}\n\nSTUDENT QUESTION: {question}"
                    )
                    if GROQ_API_KEY:
                        try:
                            answer = call_model(
                                prompt,
                                system_prompt=(
            "You are AralMate AI, a careful document-grounded study tutor. "
            "Use the supplied study passages as the only source for claims "
            "about the uploaded document. Verify each factual statement "
            "against the actual passage before answering. Pay particular "
            "attention to names, numbers, dates, definitions, sequences, "
            "cause and effect, directions, and left-versus-right distinctions. "
            "Never reverse relationships or infer missing details as facts. "
            "If the passages do not contain enough evidence, clearly say so "
            "and explain what information is available. Correct false "
            "premises when the supplied material contradicts them. "
            "Cite only page numbers explicitly present in the supplied "
            "passages, placing citations next to supported claims. "
            "Never invent quotations, page numbers, or source evidence."
        ),
                                max_tokens=1200,
                            )
                        except Exception as exc:
                            answer = "I found relevant passages, but the AI provider request failed. Check your Groq key, model name, and account limits.\n\n" + "\n\n".join(f"**Page {s['page']}**\n{s['text']}" for s in sources)
                            st.error(f"Groq request failed: {exc}")
                    else:
                        answer = "**Relevant passages from your document**\n\n" + "\n\n".join(f"**Page {s['page']}**\n{s['text']}" for s in sources)
                st.markdown(answer)
                st.session_state.messages.append({"role": "assistant", "content": answer})


# ------------------------------ PRACTICE STUDIO -------------------------------
elif page == "📝 Practice Studio":
    st.markdown("<h2 class='section-title'>Practice Studio</h2>", unsafe_allow_html=True)
    st.write("Generate a short multiple-choice quiz from your uploaded study material.")
    if not st.session_state.chunks:
        st.warning("Upload a PDF in My Library first.")
    elif not GROQ_API_KEY:
        st.info("Connect your Groq API key to generate quizzes. PDF upload and retrieval are available without it.")
    else:
        if st.button("✨ Generate 5-question quiz", type="primary"):
            with st.spinner("Creating questions from your study material..."):
                try:
                    context = document_context(st.session_state.chunks, limit=16, max_chars=12000)
                    prompt = (
                        "Create exactly 5 high-quality multiple-choice questions based ONLY on the supplied study material. "
                        "Return a valid JSON array containing exactly 5 question objects. "
                        "Each object must contain: question (string), options (array of exactly 4 distinct strings), "
                        "answer (string that exactly matches one option), explanation (string), and topic (short string).\n\n"

                        "CRITICAL QUESTION QUALITY RULES:\n"
                        "1. Every question must have EXACTLY ONE correct answer.\n"
                        "2. The three distractors must be clearly and unambiguously incorrect.\n"
                        "3. Never include two options that are both correct, mathematically equivalent, "
                        "synonyms, or different ways of expressing the same answer.\n"
                        "4. For mathematics, carefully verify every option. For example, do not ask "
                        "which pair contains like terms if two different listed pairs both contain like terms.\n"
                        "5. Avoid vague wording, trick questions, subjective questions, and information "
                        "that is not supported by the supplied study material.\n"
                        "6. Make each question self-contained and specific. Include necessary context "
                        "such as the variable, unit, condition, or requested concept.\n"
                        "7. Do not use 'all of the above' or 'none of the above'.\n"
                        "8. Verify that the answer is supported by the source and that the explanation "
                        "clearly explains why it is correct.\n"
                        "9. Before returning the JSON, independently review all four options for each "
                        "question. If more than one option could reasonably be correct, rewrite the question.\n"
                        "10. Keep questions appropriate for a student studying the supplied material.\n\n"

                        "Return JSON only. Do not include markdown fences or commentary.\n\n"
                        f"STUDY MATERIAL:\n{context}"
                    )
                    generated = call_model(
                        prompt,
                        system_prompt=(
                            "You are a careful educational assessment designer and subject-matter reviewer. "
                            "Use only the supplied source material. Accuracy is more important than complexity. "
                            "Check every question and every answer option for ambiguity, overlapping meanings, "
                            "and multiple correct answers. For mathematical questions, independently verify "
                            "the correctness of every option. Return valid JSON only."
                        ),
                        max_tokens=3000,
                    )
                    st.session_state.quiz = validate_quiz(parse_json_response(generated))
                    st.session_state.quiz_id += 1
                    st.session_state.quiz_submitted = False
                    st.session_state.quiz_results = []
                    st.session_state.score = None
                    st.success("Your quiz is ready. Take your time!")
                except Exception as exc:
                    st.error(f"Quiz generation failed: {exc}")
        if st.session_state.quiz:
            st.markdown("### Your practice quiz")
            if not st.session_state.quiz_submitted:
                with st.form(f"quiz_form_{st.session_state.quiz_id}"):
                    selected_answers = []
                    for index, item in enumerate(st.session_state.quiz):
                        st.markdown(f"**{index + 1}. {item['question']}**")
                        selected = st.radio(
                            "Choose one answer",
                            item["options"],
                            index=None,
                            key=f"quiz_{st.session_state.quiz_id}_{index}",
                        )
                        selected_answers.append(selected)
                        st.write("")
                    submitted = st.form_submit_button("Submit my answers", type="primary")
                if submitted:
                    results = []
                    for index, (selected, item) in enumerate(zip(selected_answers, st.session_state.quiz)):
                        is_correct = selected == item["answer"]
                        results.append({
                            "question": item["question"],
                            "topic": item["topic"],
                            "selected": selected,
                            "answer": item["answer"],
                            "correct": is_correct,
                            "explanation": item["explanation"],
                        })
                    correct_count = sum(1 for result in results if result["correct"])
                    st.session_state.quiz_results = results
                    st.session_state.score = f"{correct_count}/{len(results)}"
                    st.session_state.quiz_submitted = True
                    st.session_state.quiz_history.append({
                        "score": correct_count,
                        "total": len(results),
                        "results": results,
                        "document_id": st.session_state.document_id,
                        "filename": st.session_state.filename,
                    })
                    st.rerun()
            else:
                results = st.session_state.quiz_results
                correct_count = sum(1 for result in results if result["correct"])
                st.success(f"You scored {correct_count} out of {len(results)} ({round(correct_count / len(results) * 100)}%).")
                for index, result in enumerate(results, start=1):
                    st.markdown(f"**{index}. {result['question']}**")
                    if result["correct"]:
                        st.success("Correct!")
                    else:
                        st.error(f"Not quite. Your answer: {result['selected'] or 'No answer selected'}")
                        st.write(f"**Correct answer:** {result['answer']}")
                    st.write(result["explanation"])
                    st.caption(f"Topic: {result['topic']}")
                    st.divider()
                if st.button("Generate another quiz"):
                    st.session_state.quiz = []
                    st.session_state.quiz_submitted = False
                    st.session_state.quiz_results = []
                    st.rerun()


# -------------------------------- FLASHCARDS ---------------------------------
elif page == "🗂️ Flashcards":
    st.markdown("<h2 class='section-title'>Flashcard Studio</h2>", unsafe_allow_html=True)
    st.write("Create quick review cards from your study material.")
    if not st.session_state.chunks:
        st.warning("Upload a PDF in My Library first.")
    elif not GROQ_API_KEY:
        st.info("Connect your Groq API key to generate flashcards.")
    else:
        if st.button("✨ Generate flashcards", type="primary"):
            with st.spinner("Creating your flashcards..."):
                try:
                    context = document_context(st.session_state.chunks, limit=14, max_chars=10000)
                    prompt = (
                        "Create 8 concise, useful study flashcards based ONLY on the material below. "
                        "Return a JSON array of objects with exactly two string fields: front and back. "
                        "The front should be a clear question or key term; the back should give a concise, accurate answer. "
                        "Do not include markdown fences or commentary.\n\n"
                        f"STUDY MATERIAL:\n{context}"
                    )
                    generated = call_model(prompt, system_prompt="You create accurate, concise study flashcards using only the supplied material.", max_tokens=1800)
                    st.session_state.flashcards = validate_flashcards(parse_json_response(generated))
                    st.session_state.flashcard_index = 0
                    st.session_state.flashcard_revealed = False
                    st.success(f"Created {len(st.session_state.flashcards)} flashcards.")
                except Exception as exc:
                    st.error(f"Flashcard generation failed: {exc}")
        if st.session_state.flashcards:
            total = len(st.session_state.flashcards)
            index = min(st.session_state.flashcard_index, total - 1)
            card = st.session_state.flashcards[index]
            st.progress((index + 1) / total, text=f"Card {index + 1} of {total}")
            st.markdown(
                f"<div class='study-card' style='min-height:220px;text-align:center;display:flex;flex-direction:column;justify-content:center;'>"
                f"<div class='metric-label'>{'ANSWER' if st.session_state.flashcard_revealed else 'QUESTION'}</div>"
                f"<h2 style='color:#173F35;'>{html.escape(card['back'] if st.session_state.flashcard_revealed else card['front'])}</h2>"
                f"</div>",
                unsafe_allow_html=True,
            )
            st.write("")
            nav1, nav2, nav3 = st.columns([1, 2, 1])
            with nav1:
                if st.button("← Previous", disabled=index == 0, use_container_width=True):
                    st.session_state.flashcard_index -= 1
                    st.session_state.flashcard_revealed = False
                    st.rerun()
            with nav2:
                label = "Hide answer" if st.session_state.flashcard_revealed else "Reveal answer"
                if st.button(label, type="primary", use_container_width=True):
                    st.session_state.flashcard_revealed = not st.session_state.flashcard_revealed
                    st.rerun()
            with nav3:
                if st.button("Next →", disabled=index >= total - 1, use_container_width=True):
                    st.session_state.flashcard_index += 1
                    st.session_state.flashcard_revealed = False
                    st.rerun()


# ----------------------------- KNOWLEDGE GAP MAP -----------------------------
elif page == "🧭 Knowledge Gap Map":
    st.markdown(
        "<h2 class='section-title'>Your Knowledge Gap Map</h2>",
        unsafe_allow_html=True,
    )
    st.write(
        "Review topics that need more attention, based on your submitted quiz answers."
    )

    if not st.session_state.quiz_history:
        st.info(
            "Your Knowledge Gap Map will appear after you complete at least "
            "one quiz. It uses your actual answers rather than estimated mastery scores."
        )
    else:
        history = st.session_state.quiz_history

        document_options = {"All study materials": "__all__"}
        seen_document_ids = set()

        for attempt in history:
            document_id = attempt.get("document_id") or "__legacy__"

            # Show each study material only once, even if it has multiple quiz attempts.
            if document_id in seen_document_ids:
                continue

            seen_document_ids.add(document_id)
            filename = attempt.get("filename") or "Previous / unknown material"

            if document_id == "__legacy__":
                label = filename
            else:
                label = f"{filename} · {str(document_id)[:8]}"

            document_options[label] = document_id

        selected_label = st.selectbox(
            "Filter by study material",
            options=list(document_options.keys()),
        )

        selected_document_id = document_options[selected_label]

        if selected_document_id == "__all__":
            filtered_attempts = history
        else:
            filtered_attempts = [
                attempt for attempt in history
                if (attempt.get("document_id") or "__legacy__")
                == selected_document_id
            ]

        topic_stats: dict[str, dict[str, int]] = {}

        for attempt in filtered_attempts:
            for result in attempt.get("results", []):
                topic = (
                    str(result.get("topic") or "General understanding").strip()
                    or "General understanding"
                )

                topic_stats.setdefault(
                    topic,
                    {"correct": 0, "incorrect": 0},
                )

                is_correct = bool(result.get("correct", False))
                key = "correct" if is_correct else "incorrect"
                topic_stats[topic][key] += 1

        st.metric("Quiz attempts", len(filtered_attempts))

        if not topic_stats:
            st.info("No recorded question results for this study material yet.")
        else:
            weak_topics = sorted(
                topic_stats.items(),
                key=lambda pair: (
                    pair[1]["incorrect"],
                    -pair[1]["correct"],
                ),
                reverse=True,
            )

            for topic, counts in weak_topics:
                total = counts["correct"] + counts["incorrect"]
                accuracy = (
                    round(counts["correct"] / total * 100)
                    if total else 0
                )

                with st.container(border=True):
                    left, right = st.columns([3, 1])

                    with left:
                        st.markdown(f"**{topic}**")
                        st.caption(
                            f"{counts['incorrect']} incorrect • "
                            f"{counts['correct']} correct • "
                            f"{total} responses"
                        )
                        st.progress(
                            accuracy / 100,
                            text=f"Accuracy: {accuracy}%",
                        )

                    with right:
                        if counts["incorrect"]:
                            st.warning("Review recommended")
                        else:
                            st.success("Looking good")

            if any(
                counts["incorrect"] for counts in topic_stats.values()
            ):
                st.info(
                    "Suggested next step: revisit the topics with the most "
                    "incorrect answers, then generate another practice quiz "
                    "to check your understanding."
                )
            else:
                st.success(
                    "No incorrect answers have been recorded in the selected "
                    "study material's completed quizzes yet. Keep practicing "
                    "to build a more informative picture."
                )


# ---------------------------------- FOOTER -------------------------------------
st.markdown("<div style='height:18px'></div><p class='muted' style='text-align:center;font-size:12px;'>AralMate AI • Learn smarter. Understand deeper. Prepare better.</p>", unsafe_allow_html=True)

