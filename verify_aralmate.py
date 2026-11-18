"""
AralMate AI automated verification
Run from the folder containing app.py:
    python verify_aralmate.py

This verifies the real helper functions in app.py without executing Streamlit's
interactive UI or making paid/live Groq API requests.
"""
from __future__ import annotations

import ast
import io
import json
import os
import re
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz
from openai import OpenAI
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity


APP_FILE = Path(__file__).resolve().with_name("app.py")
if not APP_FILE.exists():
    raise SystemExit(f"ERROR: Could not find {APP_FILE}. Put this script beside app.py.")

# Load the actual function definitions from app.py without running its Streamlit
# page-building code. This keeps the tests isolated and avoids needing a browser.
source = APP_FILE.read_text(encoding="utf-8")
tree = ast.parse(source, filename=str(APP_FILE))
wanted = {
    "get_client",
    "call_model",
    "extract_pdf",
    "split_into_sentences",
    "make_chunks",
    "retrieve",
    "document_context",
    "parse_json_response",
    "validate_quiz",
    "validate_flashcards",
}
function_nodes = [
    node for node in tree.body
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    and node.name in wanted
]
found = {node.name for node in function_nodes}
missing = sorted(wanted - found)
if missing:
    raise SystemExit("ERROR: app.py is missing expected functions: " + ", ".join(missing))

namespace = {
    "Any": object,
    "OpenAI": OpenAI,
    "fitz": fitz,
    "json": json,
    "os": os,
    "re": re,
    "TfidfVectorizer": TfidfVectorizer,
    "cosine_similarity": cosine_similarity,
    "GROQ_API_KEY": None,
    "GROQ_MODEL": "openai/gpt-oss-20b",
    "API_BASE_URL": "https://api.groq.com/openai/v1",
}
module = ast.Module(body=function_nodes, type_ignores=[])
exec(compile(module, str(APP_FILE), "exec"), namespace)


class UploadedPDF:
    def __init__(self, data: bytes):
        self._data = data

    def getvalue(self):
        return self._data


def make_pdf(pages: list[str]) -> bytes:
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


class AralMateVerification(unittest.TestCase):
    def test_pdf_text_extraction_and_page_numbers(self):
        pdf = make_pdf([
            "The water cycle describes how water moves around Earth.",
            "Evaporation changes liquid water into water vapor.",
        ])
        pages = namespace["extract_pdf"](UploadedPDF(pdf))
        self.assertEqual([p["page"] for p in pages], [1, 2])
        self.assertIn("water cycle", pages[0]["text"].lower())
        self.assertIn("evaporation", pages[1]["text"].lower())

    def test_empty_or_image_only_pdf_returns_no_text_pages(self):
        pdf = make_pdf(["", "   "])
        pages = namespace["extract_pdf"](UploadedPDF(pdf))
        self.assertEqual(pages, [])

    def test_sentence_splitter_handles_empty_and_normal_text(self):
        split = namespace["split_into_sentences"]
        self.assertEqual(split("  "), [])
        sentences = split("Water evaporates. Clouds form. Rain falls!")
        self.assertEqual(len(sentences), 3)
        self.assertTrue(sentences[0].endswith("."))

    def test_chunking_preserves_page_references(self):
        pages = [
            {"page": 2, "text": "First sentence. Second sentence. Third sentence."},
            {"page": 5, "text": "A separate page begins. It has another sentence."},
        ]
        chunks = namespace["make_chunks"](pages, max_chars=35, overlap_sentences=0)
        self.assertTrue(chunks)
        self.assertTrue(all("page" in c and "text" in c for c in chunks))
        self.assertEqual({c["page"] for c in chunks}, {2, 5})

    def test_retrieval_returns_relevant_passage_and_page(self):
        chunks = [
            {"page": 1, "text": "The water cycle includes evaporation and condensation."},
            {"page": 2, "text": "Photosynthesis allows plants to convert light into chemical energy."},
        ]
        results = namespace["retrieve"]("What is evaporation?", chunks, top_k=2)
        self.assertTrue(results)
        self.assertEqual(results[0]["page"], 1)
        self.assertGreater(results[0]["score"], 0)

    def test_retrieval_handles_empty_question_and_empty_chunks(self):
        retrieve = namespace["retrieve"]
        self.assertEqual(retrieve("", [{"page": 1, "text": "Some text"}]), [])
        self.assertEqual(retrieve("question", []), [])

    def test_retrieval_handles_vocabulary_without_features(self):
        chunks = [{"page": 1, "text": "!!! ???"}]
        self.assertEqual(namespace["retrieve"]("???", chunks), [])

    def test_document_context_includes_page_citations_and_respects_limit(self):
        chunks = [
            {"page": 1, "text": "Alpha topic."},
            {"page": 3, "text": "Beta topic."},
        ]
        context = namespace["document_context"](chunks, limit=1)
        self.assertIn("[Page 1]", context)
        self.assertNotIn("[Page 3]", context)

    def test_document_context_obeys_character_budget(self):
        chunks = [{"page": 1, "text": "A" * 500}]
        context = namespace["document_context"](chunks, max_chars=200)
        self.assertLessEqual(len(context), 200)

    def test_json_parser_accepts_plain_and_fenced_json(self):
        parse = namespace["parse_json_response"]
        self.assertEqual(parse('[{"a": 1}]'), [{"a": 1}])
        self.assertEqual(parse('```json\n{"ok": true}\n```'), {"ok": True})

    def test_json_parser_can_find_json_after_commentary(self):
        result = namespace["parse_json_response"]('Here is the result:\n{"score": 4}')
        self.assertEqual(result, {"score": 4})

    def test_json_parser_rejects_invalid_json(self):
        with self.assertRaises(ValueError):
            namespace["parse_json_response"]("not json at all")

    def test_quiz_validator_accepts_valid_quiz(self):
        questions = []
        for i in range(3):
            options = ["A", "B", "C", "D"]
            questions.append({
                "question": f"Question {i + 1}?",
                "options": options,
                "answer": "B",
                "explanation": "Because the source says so.",
                "topic": "Water cycle",
            })
        result = namespace["validate_quiz"]({"questions": questions})
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["answer"], "B")
        self.assertEqual(result[0]["topic"], "Water cycle")

    def test_quiz_validator_rejects_too_few_questions(self):
        with self.assertRaises(ValueError):
            namespace["validate_quiz"]([])

    def test_quiz_validator_discards_invalid_question_shapes(self):
        bad = [{"question": "Incomplete?", "options": ["A", "B"], "answer": "A"}] * 3
        with self.assertRaises(ValueError):
            namespace["validate_quiz"](bad)

    def test_flashcard_validator_accepts_valid_cards_and_caps_at_ten(self):
        cards = [{"front": f"Term {i}", "back": f"Definition {i}"} for i in range(12)]
        result = namespace["validate_flashcards"]({"flashcards": cards})
        self.assertEqual(len(result), 10)

    def test_flashcard_validator_rejects_empty_or_invalid_cards(self):
        with self.assertRaises(ValueError):
            namespace["validate_flashcards"]([])
        with self.assertRaises(ValueError):
            namespace["validate_flashcards"]([{"front": "Question", "back": ""}])

    def test_get_client_returns_none_without_api_key(self):
        namespace["GROQ_API_KEY"] = None
        self.assertIsNone(namespace["get_client"]())

    def test_get_client_uses_configured_key_and_base_url(self):
        namespace["GROQ_API_KEY"] = "unit-test-key"
        with patch.dict(os.environ, {}, clear=False):
            with patch.object(OpenAI, "__init__", return_value=None) as mocked_init:
                client = namespace["get_client"]()
        self.assertIsInstance(client, OpenAI)
        mocked_init.assert_called_once()
        args = mocked_init.call_args.kwargs
        self.assertEqual(args["api_key"], "unit-test-key")
        self.assertEqual(args["base_url"], namespace["API_BASE_URL"])
        namespace["GROQ_API_KEY"] = None

    def test_call_model_fails_clearly_without_api_key(self):
        namespace["GROQ_API_KEY"] = None
        with self.assertRaisesRegex(RuntimeError, "API key is not configured"):
            namespace["call_model"]("Test prompt")

    def test_call_model_uses_chat_completion_and_returns_text(self):
        class FakeMessage:
            content = "  A grounded answer.  "

        class FakeChoice:
            message = FakeMessage()

        class FakeResponse:
            choices = [FakeChoice()]

        class FakeCompletions:
            def create(self, **kwargs):
                self.kwargs = kwargs
                return FakeResponse()

        class FakeClient:
            def __init__(self):
                self.chat = type("Chat", (), {})()
                self.chat.completions = FakeCompletions()

        fake_client = FakeClient()
        namespace["GROQ_API_KEY"] = "test-key"
        with patch.dict(namespace, {"get_client": lambda: fake_client}):
            answer = namespace["call_model"]("Explain evaporation", max_tokens=100)
        self.assertEqual(answer, "A grounded answer.")
        namespace["GROQ_API_KEY"] = None

    def test_call_model_rejects_empty_provider_response(self):
        class FakeCompletions:
            def create(self, **kwargs):
                return type("Response", (), {
                    "choices": [type("Choice", (), {
                        "message": type("Message", (), {"content": " "})()
                    })()]
                })()

        fake_client = type("Client", (), {})()
        fake_client.chat = type("Chat", (), {})()
        fake_client.chat.completions = FakeCompletions()
        with patch.dict(namespace, {"get_client": lambda: fake_client}):
            with self.assertRaisesRegex(RuntimeError, "empty response"):
                namespace["call_model"]("Test")


if __name__ == "__main__":
    print(f"Checking application: {APP_FILE}")
    print("Test scope: real app.py helper functions; no live Groq calls.")
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(AralMateVerification)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print("\n" + "=" * 64)
    print(f"RESULT: {'PASS' if result.wasSuccessful() else 'FAIL'}")
    print(f"Tests run: {result.testsRun}")
    print(f"Failures: {len(result.failures)}")
    print(f"Errors: {len(result.errors)}")
    print("=" * 64)
    raise SystemExit(0 if result.wasSuccessful() else 1)
