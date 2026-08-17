"""Точка входа для запуска и для сборки PyInstaller.

Пакет proof_recorder использует относительные импорты, поэтому запускать его
надо как пакет (`python -m proof_recorder`) или через этот тонкий лаунчер
(`python run_proof_recorder.py`). PyInstaller тоже собирает именно этот файл —
см. ProofRecorder.spec.
"""

import tkinter as tk

from proof_recorder.gui import ProofRecorderApp

if __name__ == "__main__":
    root = tk.Tk()
    app = ProofRecorderApp(root)
    root.mainloop()
