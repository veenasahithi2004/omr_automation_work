import tkinter as tk
from tkinter import ttk
from .designer import Designer
from .scanner import Scanner


def main():
    root = tk.Tk()
    root.title("OMR Reader  -  design template, scan sheets, store in MDB")
    root.geometry("1350x860")
    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True)
    designer = Designer(nb)
    scanner = Scanner(nb, designer_source=lambda: designer.current())
    nb.add(designer, text="  1. Template designer  ")
    nb.add(scanner, text="  2. Scan && save to MDB  ")
    root.mainloop()
