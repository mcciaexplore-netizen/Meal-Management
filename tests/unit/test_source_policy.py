import ast
import io
import tokenize
import unittest
from pathlib import Path


class SourcePolicyTests(unittest.TestCase):
    def test_python_sources_have_no_comments_or_docstrings(self):
        root = Path(__file__).resolve().parents[2]
        sources = list((root / "backend").rglob("*.py")) + list((root / "tests").rglob("*.py")) + list(root.glob("*.py"))
        self.assertTrue(sources)
        for path in sources:
            with self.subTest(path=str(path.relative_to(root))):
                source = path.read_text(encoding="utf-8")
                tokens = tokenize.generate_tokens(io.StringIO(source).readline)
                self.assertFalse(any(token.type == tokenize.COMMENT for token in tokens))
                tree = ast.parse(source)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                        self.assertIsNone(ast.get_docstring(node))


if __name__ == "__main__":
    unittest.main()
