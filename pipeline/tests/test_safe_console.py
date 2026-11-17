"""safe_console：cp950 終端機遇到無法編碼的符號時不能中斷（審查 Q1）。"""
import io
import subprocess
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import safe_console  # noqa: E402


def cp950_stream():
    return io.TextIOWrapper(io.BytesIO(), encoding="cp950")


class SafeConsoleTest(unittest.TestCase):
    def test_default_cp950_stream_raises(self):
        """先確認問題存在：沒處理的 cp950 串流印 ⚠ 會丟出例外。"""
        s = cp950_stream()
        with self.assertRaises(UnicodeEncodeError):
            s.write("⚠ 警告\n")

    def test_configured_stream_escapes_symbols_and_keeps_chinese(self):
        s = cp950_stream()
        self.assertEqual(safe_console.configure([s]), 1)
        s.write("⚠ 地磁 M≥3.5 ✗\n")
        s.flush()
        out = s.buffer.getvalue().decode("cp950")
        self.assertIn("\\u26a0", out)   # ⚠ 變成跳脫寫法
        self.assertIn("\\u2265", out)   # ≥
        self.assertIn("地磁", out)       # 中文照原編碼輸出，不是亂碼

    def test_streams_without_reconfigure_are_left_alone(self):
        class Plain:
            def write(self, text):
                return len(text)
        self.assertEqual(safe_console.configure([Plain()]), 0)

    def test_subprocess_with_cp950_pipe_does_not_crash(self):
        """子程序的輸出接到 cp950 的管線時（例如測試或排程用 subprocess 執行腳本），也不能中斷。"""
        code = f"import sys; sys.path.insert(0, {str(SRC)!r}); import safe_console; print('⚠ M≥3.5 ✗ 完成')"
        env = {"PYTHONIOENCODING": "cp950", "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")}
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr.decode("cp950", "replace"))
        self.assertIn("完成", r.stdout.decode("cp950"))


if __name__ == "__main__":
    unittest.main()
