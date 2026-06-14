import re
import threading
import queue
import time
import os
import bisect
import json
import subprocess
import platform
import sys
import logging
import argparse
import fnmatch
import ast
from pathlib import Path
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple, Any

try:
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog
    GUI_AVAILABLE = True
except ImportError:
    GUI_AVAILABLE = False

# Diagnostic background logging configuration for I/O errors
logging.basicConfig(filename='nexa_core_x.log', level=logging.WARNING, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("NexaCoreX")

@dataclass
class MatchData:
    """Represents a matched line inside the workspace index."""
    file_name: str
    line_num: int
    content: str

class WorkspaceFile:
    """Logical hierarchical node representing a file or directory structure in the workspace."""
    def __init__(self, name: str, path: Path, children: List['WorkspaceFile']):
        self.name = name
        self.path = path
        self.children = children

    def to_dict(self) -> Dict[str, Any]:
        """Converts the hierarchical structure into a dictionary for serialization."""
        return {
            'name': self.name,
            'path': str(self.path),
            'children': [child.to_dict() for child in self.children],
        }

class FileNode:
    """Encapsulates indexed file data, including precomputed character offsets for lines."""
    __slots__ = ['name', 'full_path', 'content', 'newline_offsets']
    
    def __init__(self, name: str, content: str, full_path: str):
        self.name = name
        self.full_path = full_path
        self.content = content
        self.reindex()

    def reindex(self):
        """Precomputes newline character offsets to enable fast O(log N) line lookups."""
        self.newline_offsets = [m.start() for m in re.finditer(r'\n', self.content)]

    def get_line_text(self, line_idx: int) -> str:
        """Efficiently extracts specific line text using character offset mapping."""
        start_char = self.newline_offsets[line_idx - 1] + 1 if line_idx > 0 else 0
        end_char = self.newline_offsets[line_idx] if line_idx < len(self.newline_offsets) else len(self.content)
        return self.content[start_char:end_char]

class NexaEngine:
    """Ultra-fast, thread-safe workspace file indexing and search engine."""
    MAX_FILE_SIZE = 10 * 1024 * 1024  # Standard safety limit for file loading
    MAX_MATCHES = 10000

    def __init__(self):
        self.files: List[FileNode] = []
        self.index: Dict[str, FileNode] = {}
        self.ignore_patterns: List[str] = [
            '.git', '__pycache__', '.venv', 'venv', 'node_modules', 
            '*.pyc', '*.pyo', '*.exe', '*.dll', '.DS_Store', 'build', 'dist'
        ]
        self.lock = threading.Lock()  # Prevents thread collisions during reload/save actions

    def clear_workspace(self):
        """Clears the active workspace index."""
        with self.lock:
            self.files.clear()
            self.index.clear()

    def _compile_ignore_patterns(self, root_dir: str):
        """Parses the local .gitignore file if present and appends rules to patterns."""
        gitignore_path = os.path.join(root_dir, '.gitignore')
        try:
            if os.path.exists(gitignore_path):
                with open(gitignore_path, 'r', encoding='utf-8', errors='replace') as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#') and not line.startswith('!'):
                            self.ignore_patterns.append(line.rstrip('/'))
        except Exception as e:
            logger.warning(f"Error parsing .gitignore: {e}")

    def _is_ignored(self, path: str, root_dir: str) -> bool:
        """Checks if a given path matches configured ignore list patterns."""
        rel_path = os.path.relpath(path, root_dir)
        parts = rel_path.split(os.sep)
        for part in parts:
            if any(fnmatch.fnmatch(part, p) for p in self.ignore_patterns): 
                return True
        if any(fnmatch.fnmatch(rel_path, p) for p in self.ignore_patterns): 
                return True
        return False

    def load_directory(self, dirpath: str, progress_queue: Optional[queue.Queue] = None) -> Optional[str]:
        """Recursively indexes a target directory asynchronously using parallel workers."""
        self.clear_workspace()
        self._compile_ignore_patterns(dirpath)
        
        def get_valid_paths(path: str):
            try:
                with os.scandir(path) as it:
                    for entry in it:
                        if self._is_ignored(entry.path, dirpath): 
                            continue
                        if entry.is_dir(follow_symlinks=False): 
                            yield from get_valid_paths(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            try:
                                if entry.stat().st_size <= self.MAX_FILE_SIZE: 
                                    yield entry.path
                            except OSError:
                                continue
            except (PermissionError, OSError) as e:
                logger.warning(f"I/O Access Denied: {path} - {e}")

        all_paths = list(get_valid_paths(dirpath))
        total_count = len(all_paths)
        if total_count == 0: 
            return "No valid source files found in the loaded workspace."

        def load_file(full_path: str) -> Optional[Tuple[str, str, str]]:
            try:
                # Binary files safety pre-checks
                with open(full_path, 'rb') as f:
                    chunk = f.read(1024)
                    if b'\x00' in chunk:
                        return None
                
                with open(full_path, 'r', encoding='utf-8', errors='replace') as f:
                    content = f.read()
                return (os.path.relpath(full_path, dirpath), content, full_path)
            except Exception as e:
                logger.debug(f"Failed to read file {full_path}: {e}")
            return None

        processed = 0
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=os.cpu_count() * 2) as executor:
            for result in executor.map(load_file, all_paths):
                processed += 1
                if result:
                    node = FileNode(*result)
                    with self.lock:
                        self.files.append(node)
                        self.index[node.name] = node
                if progress_queue and processed % 25 == 0:
                    progress_queue.put(("load_progress", (processed, total_count)))
        return None

    def update_file(self, name: str, new_content: str):
        """Updates file contents inside the in-memory engine index."""
        with self.lock:
            if name in self.index:
                self.index[name].content = new_content
                self.index[name].reindex()

    def search(self, query: str, match_case: bool, use_regex: bool) -> Tuple[List[MatchData], float, Optional[str]]:
        """Performs a fast literal or regex search pattern match across indexed files."""
        start_time = time.perf_counter()
        results: List[MatchData] = []
        flags = 0 if match_case else re.IGNORECASE
        
        with self.lock:
            try:
                for node in self.files:
                    if len(results) >= self.MAX_MATCHES: 
                        break
                    seen_lines = set()
                    
                    if use_regex:
                        try:
                            pattern = re.compile(query, flags)
                        except re.error as e:
                            return ([], 0.0, f"Regex Syntax Error: {e}")
                            
                        for m in pattern.finditer(node.content):
                            ln = bisect.bisect(node.newline_offsets, m.start())
                            if ln not in seen_lines:
                                seen_lines.add(ln)
                                results.append(MatchData(node.name, ln + 1, node.get_line_text(ln).strip()))
                                if len(results) >= self.MAX_MATCHES: 
                                    break
                    else:
                        search_content = node.content if match_case else node.content.lower()
                        search_query = query if match_case else query.lower()
                        idx, q_len = 0, len(search_query)
                        if not search_query:
                            continue
                        
                        while True:
                            idx = search_content.find(search_query, idx)
                            if idx == -1: 
                                break
                            ln = bisect.bisect(node.newline_offsets, idx)
                            if ln not in seen_lines:
                                seen_lines.add(ln)
                                results.append(MatchData(node.name, ln + 1, node.get_line_text(ln).strip()))
                                if len(results) >= self.MAX_MATCHES: 
                                    break
                            idx += q_len
            except Exception as e:
                logger.error(f"Search engine error: {e}")
                return ([], 0.0, f"Search engine malfunction: {e}")

        return (results, time.perf_counter() - start_time, None)

class ToolTip:
    """Minimalist popup helper class showing hints over interactive widgets."""
    def __init__(self, widget, text: str):
        self.widget = widget
        self.text = text
        self.tip_window = None
        self.widget.bind("<Enter>", self.show_tip)
        self.widget.bind("<Leave>", self.hide_tip)

    def show_tip(self, event=None):
        if self.tip_window or not self.text or not GUI_AVAILABLE: 
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 5
        self.tip_window = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tw.attributes("-topmost", True)
        tk.Label(tw, text=self.text, justify=tk.LEFT, background="#121218", 
                 foreground="#ffffff", relief=tk.SOLID, borderwidth=1,
                 font=("Segoe UI", "9", "normal"), padx=5, pady=2).pack()

    def hide_tip(self, event=None):
        if self.tip_window:
            self.tip_window.destroy()
            self.tip_window = None

class CustomTextEditor(ttk.Frame):
    """Text viewport feature class including synchronized line numbering and syntax styling."""
    def __init__(self, parent, mono_font, on_change_callback=None):
        super().__init__(parent)
        self.on_change_callback = on_change_callback
        
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)

        # Line numbers sidebar
        self.line_canvas = tk.Canvas(self, width=45, bg="#0d0d12", bd=0, highlightthickness=0)
        self.line_canvas.grid(row=0, column=0, sticky="nsew", padx=(0, 2))

        # Main Text Widget
        self.text_widget = tk.Text(self, bg="#16161e", fg="#a1a1aa", font=mono_font, 
                                   insertbackground="#00f2ff", selectbackground="#094771",
                                   bd=0, highlightthickness=0, wrap=tk.NONE, undo=True)
        self.text_widget.grid(row=0, column=1, sticky="nsew")

        # Layout scrollbars
        self.scroll_y = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self._on_scroll_y)
        self.scroll_y.grid(row=0, column=2, sticky="ns")
        self.text_widget.configure(yscrollcommand=self.scroll_y.set)

        self.scroll_x = ttk.Scrollbar(self, orient=tk.HORIZONTAL, command=self.text_widget.xview)
        self.scroll_x.grid(row=1, column=1, sticky="ew")
        self.text_widget.configure(xscrollcommand=self.scroll_x.set)

        self.text_widget.tag_configure("highlight", background="#2a2e3d")
        self.text_widget.tag_configure("kw", foreground="#ff7b72", font=(mono_font[0], mono_font[1], "bold"))
        self.text_widget.tag_configure("cls", foreground="#f2cc60", font=(mono_font[0], mono_font[1], "bold"))
        self.text_widget.tag_configure("fn", foreground="#d2a6ff")
        self.text_widget.tag_configure("str", foreground="#7ec7a2")  # Cozy eye-friendly green
        self.text_widget.tag_configure("comment", foreground="#8b949e", font=(mono_font[0], mono_font[1], "italic"))
        self.text_widget.tag_configure("builtin", foreground="#00f2ff")  # Vibrant cyan for globals
        self.text_widget.tag_configure("decorator", foreground="#ff9e64")  # Warm orange decorators
        self.text_widget.tag_configure("number", foreground="#ea4aaa")  # Neon Pink numbers
        self.text_widget.tag_configure("self_var", foreground="#e0af68", font=(mono_font[0], mono_font[1], "italic"))  # Stylized self/cls references

        self.text_widget.bind("<KeyRelease>", self._on_key_release)
        self.text_widget.bind("<MouseWheel>", self._update_lines_delay)
        self.text_widget.bind("<Configure>", self._update_lines_delay)
        
        self.update_lines()

    def _on_scroll_y(self, *args):
        self.text_widget.yview(*args)
        self.update_lines()

    def _on_key_release(self, event=None):
        self.update_lines()
        self.highlight_syntax()
        if self.on_change_callback:
            self.on_change_callback()

    def _update_lines_delay(self, event=None):
        self.update_lines()

    def update_lines(self):
        """Redraws the calculated lines matching current layout view offsets."""
        self.line_canvas.delete("all")
        i = self.text_widget.index("@0,0")
        while True:
            dline = self.text_widget.dlineinfo(i)
            if dline is None: 
                break
            y = dline[1]
            linenum = i.split(".")[0]
            self.line_canvas.create_text(35, y, anchor="ne", text=linenum, 
                                         fill="#5a5e66", font=("Segoe UI", 9))
            i = self.text_widget.index(f"{i}+1line")

    def highlight_syntax(self):
        """Evaluates content tags using high-performance search patterns."""
        for tag in ["kw", "cls", "fn", "str", "comment", "builtin", "decorator", "number", "self_var"]:
            self.text_widget.tag_remove(tag, "1.0", tk.END)

        content = self.text_widget.get("1.0", tk.END)

        # 1. Block and Inline comments
        for m in re.finditer(r'(#[^\n]*)', content):
            self.text_widget.tag_add("comment", f"1.0 + {m.start()} chars", f"1.0 + {m.end()} chars")

        # 2. Quotation strings
        triple_quotes = r'(\"\"\"[\s\S]*?\"\"\"|\'\'\'[\s\S]*?\'\'\')'
        for m in re.finditer(triple_quotes, content):
            self.text_widget.tag_add("str", f"1.0 + {m.start()} chars", f"1.0 + {m.end()} chars")
        
        single_quotes = r'(\"(?:\\\"|[^\"])*?\"|\'(?:\\\'|[^\'])*?\')'
        for m in re.finditer(single_quotes, content):
            self.text_widget.tag_add("str", f"1.0 + {m.start()} chars", f"1.0 + {m.end()} chars")

        # 3. Code Core Keywords
        keywords = r'\b(def|class|return|import|from|as|if|elif|else|for|while|try|except|with|pass|lambda|global|in|is|not|and|or|assert|break|continue|del|finally|yield|raise|async|await)\b'
        for m in re.finditer(keywords, content):
            self.text_widget.tag_add("kw", f"1.0 + {m.start()} chars", f"1.0 + {m.end()} chars")

        # 4. Standard Builtin functions
        builtins = r'\b(True|False|None|print|len|range|str|int|float|list|dict|set|tuple|bool|open|enumerate|zip|any|all|sum|max|min|abs|super|type|isinstance|dir|getattr|setattr|hasattr)\b'
        for m in re.finditer(builtins, content):
            self.text_widget.tag_add("builtin", f"1.0 + {m.start()} chars", f"1.0 + {m.end()} chars")

        # 5. Method references
        for m in re.finditer(r'\b(self|cls)\b', content):
            self.text_widget.tag_add("self_var", f"1.0 + {m.start()} chars", f"1.0 + {m.end()} chars")

        # 6. Object Classes definitions
        for m in re.finditer(r'\bclass\s+([A-Za-0-9_]+)\b', content):
            self.text_widget.tag_add("cls", f"1.0 + {m.start(1)} chars", f"1.0 + {m.end(1)} chars")

        # 7. Routine Functions definitions
        for m in re.finditer(r'\bdef\s+([A-Za-0-9_]+)\b', content):
            self.text_widget.tag_add("fn", f"1.0 + {m.start(1)} chars", f"1.0 + {m.end(1)} chars")

        # 8. Meta Decorators
        for m in re.finditer(r'(@[A-Za-0-9_\.]+)\b', content):
            self.text_widget.tag_add("decorator", f"1.0 + {m.start()} chars", f"1.0 + {m.end()} chars")

        # 9. Numeric values
        numbers = r'\b(0x[0-9a-fA-F]+|0b[01]+|0o[0-7]+|\d+\.\d+|\d+)\b'
        for m in re.finditer(numbers, content):
            self.text_widget.tag_add("number", f"1.0 + {m.start()} chars", f"1.0 + {m.end()} chars")

        self.text_widget.tag_raise("number")
        self.text_widget.tag_raise("self_var")
        self.text_widget.tag_raise("builtin")
        self.text_widget.tag_raise("decorator")
        self.text_widget.tag_raise("kw")
        self.text_widget.tag_raise("fn")
        self.text_widget.tag_raise("cls")
        self.text_widget.tag_raise("str")
        self.text_widget.tag_raise("comment")

def get_folder_structure(workdir: Path) -> WorkspaceFile:
    """Recursively computes directories and files representing active filesystem hierarchy."""
    root = WorkspaceFile(name=workdir.name, path=workdir, children=[])
    try:
        items = sorted(workdir.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        for item in items:
            if item.name in ['.git', '__pycache__', '.venv', 'venv', 'node_modules', 'build', 'dist']:
                continue
            if item.is_dir():
                dir_structure = get_folder_structure(item)
                root.children.append(dir_structure)
            else:
                root.children.append(WorkspaceFile(name=item.name, path=item, children=[]))
    except PermissionError:
        pass
    return root

if GUI_AVAILABLE:
    class NexaApp(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title("Nexa Core X - Intelligent Workspace Environment")
            self.geometry("1550x950")
            self.configure(bg="#0d0d12")

            self.engine = NexaEngine()
            self.event_queue = queue.Queue()
            
            self.is_searching = False
            self.is_loading = False
            self.current_workspace_path = None
            self.current_results = []
            
            self.mono_font = ("Consolas", 11) if platform.system() == "Windows" else ("DejaVu Sans Mono", 11)
            if platform.system() == "Darwin": 
                self.mono_font = ("Menlo", 11)

            self.config_file = "nexa_core_x_config.json"
            self.app_config = self._load_config()
            
            self._setup_styles()
            self._build_top_bar()
            self._build_bento_workspace()
            self._create_context_menu()
            
            self.bind("<Control-o>", lambda e: self._open_folder())
            self.bind("<Control-q>", lambda e: self.on_closing())
            self.protocol("WM_DELETE_WINDOW", self.on_closing)
            
            last_dir = self.app_config.get("last_directory", "")
            if last_dir and os.path.isdir(last_dir):
                self._start_async_load(last_dir)
            else:
                self.status_var.set("Active. Welcome to Nexa Core X. Press Ctrl+O to initialize.")
                
            self.after(100, self._process_queue)

        def _load_config(self) -> dict:
            default_config = {"last_directory": "", "recent_folders": [], "search_text": "", "match_case": False, "use_regex": False}
            try:
                if os.path.exists(self.config_file):
                    with open(self.config_file, 'r') as f: 
                        return {**default_config, **json.load(f)}
            except Exception as e:
                logger.warning(f"Error loading settings file: {e}")
            return default_config

        def _save_config(self):
            try:
                with open(self.config_file, 'w') as f: 
                    json.dump(self.app_config, f, indent=2)
            except Exception as e:
                logger.warning(f"Error saving settings file: {e}")

        def on_closing(self):
            self._save_config()
            self.destroy()
            sys.exit(0)

        def _setup_styles(self):
            style = ttk.Style(self)
            style.theme_use("clam")
            style.configure(".", background="#0d0d12", foreground="#a1a1aa")
            style.configure("TFrame", background="#0d0d12")
            style.configure("Bento.TFrame", background="#121218", borderwidth=1, relief="solid")
            style.configure("TButton", background="#8f00ff", foreground="#ffffff", borderwidth=0, padding=6, font=("Segoe UI", 9, "bold"))
            style.map("TButton", background=[("active", "#a12eff"), ("disabled", "#1a1a24")])
            style.configure("Search.TButton", background="#00f2ff", foreground="#0d0d12", borderwidth=0, padding=6, font=("Segoe UI", 9, "bold"))
            style.map("Search.TButton", background=[("active", "#4df7ff")])
            style.configure("TLabel", background="#121218", foreground="#a1a1aa", font=("Segoe UI", 10))
            style.configure("TCheckbutton", background="#121218", foreground="#a1a1aa")
            style.map("TCheckbutton", background=[("active", "#121218")], foreground=[("active", "#ffffff")])
            style.configure("Treeview", background="#121218", foreground="#a1a1aa", fieldbackground="#121218", borderwidth=0, font=self.mono_font, rowheight=24)
            style.map("Treeview", background=[("selected", "#8f00ff")], foreground=[("selected", "#ffffff")])
            style.configure("Treeview.Heading", background="#1a1a24", foreground="#ffffff", borderwidth=0, font=("Segoe UI", 10, "bold"))
            style.configure("TNotebook", background="#0d0d12", borderwidth=0)
            style.configure("TNotebook.Tab", background="#121218", foreground="#a1a1aa", padding=5, font=("Segoe UI", 9))
            style.map("TNotebook.Tab", background=[("selected", "#16161e")], foreground=[("selected", "#00f2ff")])

        def _build_top_bar(self):
            self.menubar = tk.Menu(self, bg="#121218", fg="#a1a1aa", activebackground="#8f00ff", activeforeground="#ffffff", borderwidth=0)
            file_menu = tk.Menu(self.menubar, tearoff=0, bg="#121218", fg="#a1a1aa", borderwidth=0)
            file_menu.add_command(label="Open Workspace... (Ctrl+O)", command=self._open_folder)
            
            self.recent_menu = tk.Menu(file_menu, tearoff=0, bg="#121218", fg="#a1a1aa", borderwidth=0)
            file_menu.add_cascade(label="Open Recent Workspaces", menu=self.recent_menu)
            self._update_recent_menu()
            
            file_menu.add_separator()
            file_menu.add_command(label="Exit (Ctrl+Q)", command=self.on_closing)
            self.menubar.add_cascade(label="File", menu=file_menu)
            self.config(menu=self.menubar)

        def _update_recent_menu(self):
            self.recent_menu.delete(0, tk.END)
            recent = self.app_config.get("recent_folders", [])
            if not recent: 
                self.recent_menu.add_command(label="No recent folders", state=tk.DISABLED)
            for path in recent: 
                self.recent_menu.add_command(label=path, command=lambda p=path: self._start_async_load(p))

        def _build_bento_workspace(self):
            main_container = ttk.Frame(self)
            main_container.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

            left_master = ttk.Frame(main_container, width=350)
            left_master.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 10))
            left_master.pack_propagate(False)

            # Panel 1: Folder Management Card
            folder_card = ttk.Frame(left_master, style="Bento.TFrame")
            folder_card.pack(fill=tk.X, pady=(0, 10), ipady=10, ipadx=10)
            
            tk.Label(folder_card, text="WORKSPACE CONTROL", font=("Segoe UI", 11, "bold"), bg="#121218", fg="#00f2ff").pack(anchor="w", padx=10, pady=5)
            self.btn_open = tk.Button(folder_card, text="📁 Initialize Folder", command=self._open_folder, 
                                      bg="#8f00ff", fg="#ffffff", activebackground="#a12eff", 
                                      borderwidth=0, font=("Segoe UI", 9, "bold"), cursor="hand2")
            self.btn_open.pack(fill=tk.X, padx=10, pady=(5, 5))
            
            self.btn_export_tree = tk.Button(folder_card, text="📝 Export Project Outline (.md)", command=self._export_to_markdown, 
                                             bg="#1a1a24", fg="#a1a1aa", activebackground="#27273a", 
                                             borderwidth=0, font=("Segoe UI", 9, "bold"), cursor="hand2")
            self.btn_export_tree.pack(fill=tk.X, padx=10, pady=5)

            # Panel 2: Engine Query Panel
            search_card = ttk.Frame(left_master, style="Bento.TFrame")
            search_card.pack(fill=tk.X, pady=(0, 10), ipady=10, ipadx=10)

            tk.Label(search_card, text="NEXA ENGINE QUERY", font=("Segoe UI", 11, "bold"), bg="#121218", fg="#00f2ff").pack(anchor="w", padx=10, pady=5)
            
            self.search_var = tk.StringVar(value=self.app_config.get("search_text", ""))
            self.search_entry = ttk.Entry(search_card, textvariable=self.search_var, font=self.mono_font)
            self.search_entry.pack(fill=tk.X, padx=10, pady=(0, 8))
            self.search_entry.bind('<Return>', lambda e: self._execute_search())

            chk_container = ttk.Frame(search_card)
            chk_container.configure(style="Bento.TFrame")
            chk_container.pack(fill=tk.X, padx=10, pady=5)
            
            self.var_regex = tk.BooleanVar(value=self.app_config.get("use_regex", False))
            ttk.Checkbutton(chk_container, text="Regex Mode", variable=self.var_regex).pack(side=tk.LEFT, padx=(5, 10))
            
            self.var_case = tk.BooleanVar(value=self.app_config.get("match_case", False))
            ttk.Checkbutton(chk_container, text="Match Case", variable=self.var_case).pack(side=tk.LEFT, padx=5)

            self.btn_search = tk.Button(search_card, text="⚡ Execute Async Search", command=self._execute_search, 
                                        bg="#00f2ff", fg="#0d0d12", activebackground="#4df7ff", 
                                        borderwidth=0, font=("Segoe UI", 9, "bold"), cursor="hand2")
            self.btn_search.pack(fill=tk.X, padx=10, pady=(8, 2))

            # Panel 3: Workspace Folder Hierarchy View
            tree_card = ttk.Frame(left_master, style="Bento.TFrame")
            tree_card.pack(fill=tk.BOTH, expand=True)

            title_row = ttk.Frame(tree_card)
            title_row.configure(style="Bento.TFrame")
            title_row.pack(fill=tk.X, padx=10, pady=5)
            tk.Label(title_row, text="WORKSPACE EXPLORER", font=("Segoe UI", 11, "bold"), bg="#121218", fg="#ffffff").pack(side=tk.LEFT)
            
            self.tree_filter_var = tk.StringVar()
            self.tree_filter_var.trace("w", self._filter_workspace_tree)
            self.filter_entry = ttk.Entry(title_row, textvariable=self.tree_filter_var, width=15, font=("Segoe UI", 9))
            self.filter_entry.pack(side=tk.RIGHT, padx=5)
            self.filter_entry.insert(0, "Filter...")
            self.filter_entry.bind("<FocusIn>", lambda e: self.filter_entry.delete(0, 'end') if self.filter_entry.get() == "Filter..." else None)
            self.filter_entry.bind("<FocusOut>", lambda e: self.filter_entry.insert(0, "Filter...") if not self.filter_entry.get() else None)

            tree_container = ttk.Frame(tree_card)
            tree_container.pack(fill=tk.BOTH, expand=True, padx=10, pady=(5, 10))
            
            self.tree_files = ttk.Treeview(tree_container, columns=("Type"), show="tree", selectmode="browse")
            scroll_files = ttk.Scrollbar(tree_container, orient="vertical", command=self.tree_files.yview)
            self.tree_files.configure(yscrollcommand=scroll_files.set)
            self.tree_files.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            scroll_files.pack(side=tk.RIGHT, fill=tk.Y)
            self.tree_files.bind('<<TreeviewSelect>>', lambda e: self._on_tree_select(self.tree_files))

            right_master = ttk.Frame(main_container)
            right_master.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

            self.right_paned_system = tk.PanedWindow(right_master, orient=tk.VERTICAL, bg="#0d0d12", sashwidth=5, sashrelief=tk.RAISED, bd=0)
            self.right_paned_system.pack(fill=tk.BOTH, expand=True)

            # Query results listing widget container
            results_bento = ttk.Frame(self.right_paned_system, style="Bento.TFrame")
            self.right_paned_system.add(results_bento, height=180)

            results_title_bar = ttk.Frame(results_bento)
            results_title_bar.configure(style="Bento.TFrame")
            results_title_bar.pack(fill=tk.X, padx=10, pady=(5, 0))
            tk.Label(results_title_bar, text="SEARCH RESULTS MATCH LISTING", font=("Segoe UI", 11, "bold"), bg="#121218", fg="#00f2ff").pack(side=tk.LEFT)
            
            self.status_var = tk.StringVar(value="Status: Ready.")
            self.status_label = ttk.Label(results_title_bar, textvariable=self.status_var, font=("Segoe UI", 9, "bold"), foreground="#4ec9b0")
            self.status_label.pack(side=tk.RIGHT, padx=10)
            
            self.progress = ttk.Progressbar(results_title_bar, mode='determinate', length=150, orient=tk.HORIZONTAL)

            results_list_frame = ttk.Frame(results_bento)
            results_list_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
            
            self.results_list = tk.Listbox(results_list_frame, bg="#16161e", fg="#a1a1aa", selectbackground="#8f00ff", 
                                           selectforeground="#ffffff", font=self.mono_font, borderwidth=0, highlightthickness=0)
            res_scroll_y = ttk.Scrollbar(results_list_frame, orient="vertical", command=self.results_list.yview)
            self.results_list.configure(yscrollcommand=res_scroll_y.set)
            res_scroll_y.pack(side=tk.RIGHT, fill=tk.Y)
            self.results_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            self.results_list.bind('<<ListboxSelect>>', self._on_result_select)

            # Core tab editor and structure analyzer
            workspace_bento = ttk.Frame(self.right_paned_system, style="Bento.TFrame")
            self.right_paned_system.add(workspace_bento, height=650)

            self.tab_notebook = ttk.Notebook(workspace_bento)
            self.tab_notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

            self.node_path_map = {}
            self.current_root_node = None
            self.open_files_map = {}

        def _create_context_menu(self):
            self.context_menu = tk.Menu(self, tearoff=0, bg="#121218", fg="#a1a1aa", activebackground="#8f00ff", activeforeground="#ffffff", borderwidth=0)
            self.context_menu.add_command(label="📂 Show in File Manager", command=self._open_file_folder_external)
            self.context_menu.add_command(label="🖥️ Launch Locally", command=self._open_external_system_editor)
            self.context_menu.add_separator()
            self.context_menu.add_command(label="📋 Copy Workspace Path", command=self._copy_absolute_path)

        def _open_folder(self):
            folder_path = filedialog.askdirectory(title="Select System Workspace Root")
            if folder_path: 
                self._start_async_load(folder_path)

        def _start_async_load(self, folder_path: str):
            if self.is_loading: 
                return
            
            self.current_workspace_path = Path(folder_path)
            self.app_config["last_directory"] = folder_path
            recent = self.app_config.get("recent_folders", [])
            if folder_path in recent: 
                recent.remove(folder_path)
            recent.insert(0, folder_path)
            self.app_config["recent_folders"] = recent[:8]
            self._save_config()
            self._update_recent_menu()
            
            self.is_loading = True
            self.status_var.set("Engine scanning directory tree...")
            self.progress.pack(side=tk.RIGHT, padx=10)
            self.progress['value'] = 0
            
            threading.Thread(target=self._async_load_thread, args=(folder_path,), daemon=True).start()

        def _async_load_thread(self, folder_path: str):
            start_time = time.perf_counter()
            err = self.engine.load_directory(folder_path, self.event_queue)
            self.event_queue.put(("load_done", (err, time.perf_counter() - start_time)))

        def _execute_search(self):
            if self.is_searching or self.is_loading: 
                return
            query = self.search_var.get().strip()
            if not query: 
                return
            
            self.is_searching = True
            self.btn_search.config(state=tk.DISABLED)
            self.results_list.delete(0, tk.END)
            
            self.app_config.update({"search_text": query, "match_case": self.var_case.get(), "use_regex": self.var_regex.get()})
            self._save_config()
            self.status_var.set("Scanning index blocks...")
            
            threading.Thread(target=self._async_search_thread, 
                             args=(query, self.var_case.get(), self.var_regex.get()), 
                             daemon=True).start()

        def _async_search_thread(self, query: str, match_case: bool, use_regex: bool):
            results, elapsed, err = self.engine.search(query, match_case, use_regex)
            if err:
                self.event_queue.put(("error", err))
            else:
                self.event_queue.put(("search_done", (results, elapsed)))

        def _draw_ast_graph(self, canvas: tk.Canvas, path: Path, text_widget_to_bind=None):
            """Computes syntax graphs for files matching search patterns using standard ast."""
            canvas.delete("all")
            
            if path.suffix != '.py':
                canvas.create_text(400, 200, 
                                   text="Spatial logic maps (AST Graph view) are computed recursively\nfor active Python source files (.py) only.\n\nChanges are synced directly between the editor and graph.", 
                                   fill="#8b949e", font=("Segoe UI", 11), justify=tk.CENTER)
                return

            try:
                with open(path, 'r', encoding='utf-8', errors='replace') as f:
                    source_code = f.read()
                    tree = ast.parse(source_code)
            except Exception as e:
                canvas.create_text(400, 200, text=f"Syntax analyzer blocked: {e}", fill="#ff7b72", font=self.mono_font)
                return

            root_ast = {"name": f"Module: {path.name}", "type": "module", "line": 1, "children": []}
            node_count = 0

            for item in tree.body:
                if isinstance(item, ast.ClassDef):
                    cls_node = {"name": f"Class: {item.name}", "type": "class", "line": item.lineno, "children": []}
                    for sub in item.body:
                        if isinstance(sub, ast.FunctionDef):
                            cls_node["children"].append({"name": f"Method: {sub.name}", "line": sub.lineno, "type": "method", "children": []})
                            node_count += 1
                    root_ast["children"].append(cls_node)
                    node_count += 1
                elif isinstance(item, ast.FunctionDef):
                    root_ast["children"].append({"name": f"Function: {item.name}", "line": item.lineno, "type": "function", "children": []})
                    node_count += 1

            if node_count == 0:
                canvas.create_text(400, 200, text="No executable Class or Function blocks found.", fill="#8b949e", font=("Segoe UI", 11))
                return

            y_spacing = 90
            x_spacing = 300

            def calculate_positions(node, depth, row_counter):
                node['depth'] = depth
                if not node['children']:
                    node['row'] = row_counter[0]
                    row_counter[0] += 1
                    return node['row']
                else:
                    child_rows = []
                    for child in node['children']:
                        child_rows.append(calculate_positions(child, depth + 1, row_counter))
                    node['row'] = sum(child_rows) / len(child_rows)
                    return node['row']

            calculate_positions(root_ast, 0, [0])

            colors = {"module": "#00f2ff", "class": "#8f00ff", "function": "#ea4aaa", "method": "#f2cc60"}
            box_width = 180
            box_height = 45

            def render_node(node):
                x = 100 + node['depth'] * x_spacing
                y = 100 + node['row'] * y_spacing

                for child in node['children']:
                    cx = 100 + child['depth'] * x_spacing
                    cy = 100 + child['row'] * y_spacing
                    
                    canvas.create_line(x + box_width/2, y, cx - box_width/2, cy, 
                                       fill="#333344", width=2, arrow=tk.LAST, smooth=True)

                color = colors.get(node['type'], "#333")
                
                canvas.create_rectangle(x - box_width/2 + 4, y - box_height/2 + 4, 
                                        x + box_width/2 + 4, y + box_height/2 + 4, 
                                        fill="#050508", outline="")
                
                box_id = canvas.create_rectangle(x - box_width/2, y - box_height/2, 
                                                 x + box_width/2, y + box_height/2, 
                                                 fill="#121218", outline=color, width=2)
                
                label_text = node['name']
                if len(label_text) > 22: 
                    label_text = label_text[:19] + "..."
                
                text_id = canvas.create_text(x, y, text=label_text, fill="#ffffff", 
                                             font=("Segoe UI", 9, "bold"), justify=tk.CENTER,
                                             tags=("node_text",))

                def make_jump(line_num):
                    def handler(event):
                        self._teleport_editor_to_line(text_widget_to_bind, line_num)
                        try:
                            parent = canvas.nametowidget(canvas.winfo_parent())
                            sub_notebook = canvas.nametowidget(parent.winfo_parent())
                            if sub_notebook and isinstance(sub_notebook, ttk.Notebook):
                                sub_notebook.select(0)  # Selecting Live Editor tab
                                if text_widget_to_bind:
                                    text_widget_to_bind.focus_set()
                        except Exception as ex:
                            logger.debug(f"Error handling automatic tab navigation: {ex}")
                    return handler

                canvas.tag_bind(box_id, "<Button-1>", make_jump(node['line']))
                canvas.tag_bind(text_id, "<Button-1>", make_jump(node['line']))
                canvas.tag_bind(box_id, "<Double-Button-1>", make_jump(node['line']))
                canvas.tag_bind(text_id, "<Double-Button-1>", make_jump(node['line']))
                
                def on_enter(event, b_id=box_id):
                    canvas.itemconfig(b_id, fill="#1a1a24")
                def on_leave(event, b_id=box_id):
                    canvas.itemconfig(b_id, fill="#121218")

                canvas.tag_bind(box_id, "<Enter>", on_enter)
                canvas.tag_bind(box_id, "<Leave>", on_leave)
                canvas.tag_bind(text_id, "<Enter>", on_enter)
                canvas.tag_bind(text_id, "<Leave>", on_leave)

                for child in node['children']:
                    render_node(child)

            render_node(root_ast)
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _teleport_editor_to_line(self, text_widget, line_num: int):
            """Focuses and aligns layout scroll windows to targeted file lines."""
            if not text_widget or not line_num: 
                return
            try:
                # Force idle tasks to compute widget layout geometries properly
                text_widget.update_idletasks()
                text_widget.tag_remove("highlight", "1.0", tk.END)
                text_widget.tag_add("highlight", f"{line_num}.0", f"{line_num}.end")
                text_widget.mark_set("insert", f"{line_num}.0")
                text_widget.see(f"{line_num}.0")
                
                # Asynchronous secondary see operation to bypass slow rendering cycles
                def double_check():
                    try:
                        text_widget.see(f"{line_num}.0")
                    except Exception:
                        pass
                text_widget.after(10, double_check)
                
                self.status_var.set(f"Teleported to line {line_num}.")
            except Exception as e:
                logger.debug(f"Error handling line jump logic: {e}")

        def _export_ast_to_mermaid(self, path: Path):
            """Compiles and exports standard structure definitions into compatible Mermaid markdown."""
            if path.suffix != '.py':
                messagebox.showinfo("Exporter Notice", "AST Logic exporting is dedicated to Python files (.py) only.")
                return

            try:
                with open(path, 'r', encoding='utf-8', errors='replace') as f:
                    tree = ast.parse(f.read())
            except Exception as e:
                messagebox.showerror("Parse Block", f"Could not read AST nodes to export: {e}")
                return

            save_path = filedialog.asksaveasfilename(
                initialfile=f"Structure_Map_{path.stem}.md",
                defaultextension=".md",
                filetypes=[("Markdown Document", "*.md"), ("All Files", "*.*")],
                title="Export Spatial Structure Map"
            )
            if not save_path: 
                return

            md_buffer = f"# Nexa Logic Diagram: {path.name}\n\n"
            md_buffer += "Auto-generated using Nexa Core X Spatial Mapping.\n\n"
            md_buffer += "```mermaid\n"
            md_buffer += "graph TD\n"
            md_buffer += f"    module_root[\"📦 Module: {path.name}\"]\n"

            cls_index, func_index, m_index = 0, 0, 0
            for item in tree.body:
                if isinstance(item, ast.ClassDef):
                    cls_id = f"C_{cls_index}"
                    md_buffer += f"    {cls_id}[\"🧩 Class: {item.name}\"]\n"
                    md_buffer += f"    module_root --> {cls_id}\n"
                    cls_index += 1
                    for sub in item.body:
                        if isinstance(sub, ast.FunctionDef):
                            meth_id = f"M_{m_index}"
                            md_buffer += f"    {meth_id}((\"⚙️ Method: {sub.name}\"))\n"
                            md_buffer += f"    {cls_id} --> {meth_id}\n"
                            m_index += 1
                elif isinstance(item, ast.FunctionDef):
                    f_id = f"F_{func_index}"
                    md_buffer += f"    {f_id}((\"⚡ Function: {item.name}\"))\n"
                    md_buffer += f"    module_root --> {f_id}\n"
                    func_index += 1

            md_buffer += "```\n"

            try:
                with open(save_path, 'w', encoding='utf-8') as f:
                    f.write(md_buffer)
                self.status_var.set(f"Diagram Saved: {Path(save_path).name}")
                messagebox.showinfo("Exporter Success", "Architecture diagram successfully generated in Markdown with Mermaid.")
            except Exception as e:
                messagebox.showerror("Error Saving", f"Failed: {e}")

        def _export_to_markdown(self):
            if not self.current_root_node:
                messagebox.showwarning("Empty Workspace", "No loaded folder is active to export.")
                return
            save_path = filedialog.asksaveasfilename(defaultextension=".md", filetypes=[("Markdown Document", "*.md")])
            if not save_path: 
                return

            def make_md_tree(node: WorkspaceFile, depth: int = 0) -> str:
                indent = "  " * depth
                buf = ""
                if node.children or node.path.is_dir():
                    buf += f"{indent}- 📁 **{node.name}**/\n"
                    sorted_nodes = sorted(node.children, key=lambda x: (not (x.children or x.path.is_dir()), x.name.lower()))
                    for child in sorted_nodes:
                        buf += make_md_tree(child, depth + 1)
                else:
                    buf += f"{indent}- 📄 `{node.name}`\n"
                return buf

            try:
                with open(save_path, 'w', encoding='utf-8') as f:
                    f.write(f"# Workspace Structure: {self.current_root_node.name}\n\n" + make_md_tree(self.current_root_node))
                self.status_var.set("Workspace outline saved.")
            except Exception as e:
                messagebox.showerror("Failure", str(e))

        def _filter_workspace_tree(self, *args):
            term = self.tree_filter_var.get().lower()
            if term == "filter...": 
                return
            if not self.current_root_node: 
                return
            
            for item in self.tree_files.get_children():
                self.tree_files.delete(item)
                
            if not term:
                self._insert_to_view_tree("", self.current_root_node)
                if self.tree_files.get_children():
                    self.tree_files.item(self.tree_files.get_children()[0], open=True)
                return

            def recursive_filter(parent, node: WorkspaceFile):
                is_matched = False
                node_id = ""
                for child in node.children:
                    if recursive_filter(node_id if node_id else parent, child):
                        if not is_matched:
                            icon = "📁" if node.children or node.path.is_dir() else "📄"
                            node_id = self.tree_files.insert(parent, "end", text=f"{icon} {node.name}", open=True)
                            self.node_path_map[node_id] = node.path
                            is_matched = True
                
                if term in node.name.lower() and not is_matched:
                    icon = "📁" if node.children or node.path.is_dir() else "📄"
                    node_id = self.tree_files.insert(parent, "end", text=f"{icon} {node.name}", open=False)
                    self.node_path_map[node_id] = node.path
                    is_matched = True
                return is_matched

            recursive_filter("", self.current_root_node)

        def _insert_to_view_tree(self, parent_id, node: WorkspaceFile):
            if node.children or node.path.is_dir():
                item_id = self.tree_files.insert(parent_id, "end", text=f"📁 {node.name}", open=False)
                self.node_path_map[item_id] = node.path
                for child in node.children:
                    self._insert_to_view_tree(item_id, child)
            else:
                item_id = self.tree_files.insert(parent_id, "end", text=f"📄 {node.name}")
                self.node_path_map[item_id] = node.path

        def _on_tree_select(self, tree_widget):
            selected = tree_widget.selection()
            if not selected: 
                return
            path = self.node_path_map.get(selected[0])
            if path and path.is_file():
                self._open_file_in_workspace(path)

        def _open_file_in_workspace(self, path: Path):
            str_path = Path(path).resolve().as_posix().lower()
            
            for tab_id, meta in self.open_files_map.items():
                if Path(meta['path']).resolve().as_posix().lower() == str_path:
                    self.tab_notebook.select(tab_id)
                    return

            main_tab = ttk.Frame(self.tab_notebook)
            
            sub_notebook = ttk.Notebook(main_tab)
            sub_notebook.pack(fill=tk.BOTH, expand=True)

            # Tab 1: Live Code Editor Viewport
            editor_card = ttk.Frame(sub_notebook)
            editor_widget = CustomTextEditor(editor_card, self.mono_font)
            editor_widget.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
            sub_notebook.add(editor_card, text="📝 Live Editor")

            # Tab 2: Topological Graph Canvas
            graph_card = ttk.Frame(sub_notebook)
            map_bar = ttk.Frame(graph_card)
            map_bar.pack(fill=tk.X, side=tk.TOP, pady=2, padx=5)
            
            btn_mermaid = tk.Button(map_bar, text="💾 Save Mermaid Chart", 
                                    command=lambda p=path: self._export_ast_to_mermaid(p), 
                                    bg="#00f2ff", fg="#0d0d12", activebackground="#4df7ff", 
                                    borderwidth=0, font=("Segoe UI", 8, "bold"), cursor="hand2")
            btn_mermaid.pack(side=tk.RIGHT)
            
            tk.Label(map_bar, text="Double-click node to jump to code | Drag to pan | Mousewheel to zoom", 
                     bg="#0d0d12", fg="#8b949e", font=("Segoe UI", 8)).pack(side=tk.LEFT)

            canvas_map = tk.Canvas(graph_card, bg="#0d0d12", cursor="fleur", bd=0, highlightthickness=0)
            canvas_map.pack(fill=tk.BOTH, expand=True)
            
            canvas_map.zoom_scale = 1.0

            def zoom_canvas(event):
                if event.num == 4 or event.delta > 0:
                    factor = 1.1
                elif event.num == 5 or event.delta < 0:
                    factor = 0.9
                else:
                    factor = 1.0

                new_scale = canvas_map.zoom_scale * factor
                if 0.15 <= new_scale <= 4.0:
                    canvas_map.zoom_scale = new_scale
                    cx = canvas_map.canvasx(event.x)
                    cy = canvas_map.canvasy(event.y)
                    
                    canvas_map.scale("all", cx, cy, factor, factor)
                    
                    new_size = max(4, int(round(9 * canvas_map.zoom_scale)))
                    for item in canvas_map.find_withtag("node_text"):
                        canvas_map.itemconfig(item, font=("Segoe UI", new_size, "bold"))
                    
                    canvas_map.configure(scrollregion=canvas_map.bbox("all"))

            canvas_map.bind("<ButtonPress-1>", lambda event, c=canvas_map: c.scan_mark(event.x, event.y))
            canvas_map.bind("<B1-Motion>", lambda event, c=canvas_map: c.scan_dragto(event.x, event.y, gain=1))

            canvas_map.bind("<MouseWheel>", zoom_canvas)
            canvas_map.bind("<Button-4>", zoom_canvas)
            canvas_map.bind("<Button-5>", zoom_canvas)

            sub_notebook.add(graph_card, text="🕸️ Spatial AST Mapping")

            # Bottom quick action strip
            util_strip = ttk.Frame(main_tab)
            util_strip.pack(side=tk.BOTTOM, fill=tk.X, pady=(2, 0))
            
            btn_close = tk.Button(util_strip, text="❌ Close Module", command=lambda: self._close_workspace_tab(main_tab), 
                                  bg="#2a2e3d", fg="#a1a1aa", activebackground="#f44336", activeforeground="#ffffff",
                                  borderwidth=0, font=("Segoe UI", 8, "bold"), cursor="hand2")
            btn_close.pack(side=tk.RIGHT, padx=5)
            
            btn_save = tk.Button(util_strip, text="💾 Save changes (Ctrl+S)", command=lambda: self._save_active_editor(path, editor_widget, canvas_map), 
                                 bg="#8f00ff", fg="#ffffff", activebackground="#a12eff", 
                                 borderwidth=0, font=("Segoe UI", 8, "bold"), cursor="hand2")
            btn_save.pack(side=tk.RIGHT, padx=5)

            lbl_file_info = ttk.Label(util_strip, text=f"File: {path.name} ({file_size_formatted(path)})", font=("Segoe UI", 9, "italic"))
            lbl_file_info.pack(side=tk.LEFT, padx=10)

            self.tab_notebook.add(main_tab, text=path.name)
            self.tab_notebook.select(main_tab)
            
            tab_id = self.tab_notebook.select()
            self.open_files_map[tab_id] = {'path': str_path, 'editor': editor_widget}

            try:
                with open(path, 'r', encoding='utf-8', errors='replace') as f:
                    editor_widget.text_widget.insert(tk.END, f.read())
                editor_widget.highlight_syntax()
                editor_widget.update_lines()
            except Exception as e:
                editor_widget.text_widget.insert(tk.END, f"Load Error: {e}")

            self._draw_ast_graph(canvas_map, path, editor_widget.text_widget)

            editor_widget.text_widget.bind("<Control-s>", lambda e: self._save_active_editor(path, editor_widget, canvas_map))

        def _close_workspace_tab(self, tab_instance):
            for t_id, meta in list(self.open_files_map.items()):
                if t_id == self.tab_notebook.select():
                    del self.open_files_map[t_id]
            self.tab_notebook.forget(tab_instance)

        def _save_active_editor(self, path: Path, editor_frame: CustomTextEditor, canvas: tk.Canvas):
            """Writes user text state edits back to disk and triggers index engine re-indexing updates."""
            try:
                text_content = editor_frame.text_widget.get("1.0", tk.END + "-1c")
                with open(path, 'w', encoding='utf-8', errors='replace') as f:
                    f.write(text_content)
                
                self.engine.update_file(path.name, text_content)
                self.status_var.set(f"Module saved: {path.name}")
                
                self._draw_ast_graph(canvas, path, editor_frame.text_widget)
            except Exception as e:
                messagebox.showerror("Disk Write Error", f"Could not save data:\n{e}")

        def _on_result_select(self, event):
            selection = self.results_list.curselection()
            if not selection: 
                return
            idx = selection[0]
            if idx >= len(self.current_results): 
                return
            
            match = self.current_results[idx]
            file_node = self.engine.index.get(match.file_name)
            if file_node:
                self._open_file_in_workspace(Path(file_node.full_path))
                
                resolved_target_path = Path(file_node.full_path).resolve().as_posix().lower()
                for t_id, meta in self.open_files_map.items():
                    if Path(meta['path']).resolve().as_posix().lower() == resolved_target_path:
                        self.tab_notebook.select(t_id)
                        
                        try:
                            editor_widget = meta['editor']
                            editor_card = editor_widget.master
                            sub_notebook = editor_card.master
                            if isinstance(sub_notebook, ttk.Notebook):
                                sub_notebook.select(0)  # Forcing Live Editor tab (index 0)
                        except Exception as ex:
                            logger.debug(f"Failed to focus the editor notebook tab: {ex}")
                        
                        # Trigger teleport and alignment coordinates logic
                        self._teleport_editor_to_line(meta['editor'].text_widget, match.line_num)
                        
                        query = self.search_var.get()
                        if query:
                            editor_text = meta['editor'].text_widget
                            raw_line = editor_text.get(f"{match.line_num}.0", f"{match.line_num}.end")
                            flags = 0 if self.var_case.get() else re.IGNORECASE
                            pat = re.escape(query) if not self.var_regex.get() else query
                            try:
                                for m in re.finditer(pat, raw_line, flags):
                                    editor_text.tag_add("highlight", 
                                                        f"{match.line_num}.{m.start()}", 
                                                        f"{match.line_num}.{m.end()}")
                            except Exception as ex:
                                logger.debug(f"Highlighting error: {ex}")
                        
                        try:
                            meta['editor'].text_widget.focus_set()
                        except Exception:
                            pass
                        break

        def _open_file_folder_external(self):
            selected = self.tree_files.selection()
            if not selected: 
                return
            path = self.node_path_map.get(selected[0])
            if not path: 
                return
            
            p_str = os.path.normpath(str(path))
            sys_plat = platform.system()
            try:
                if sys_plat == "Windows": 
                    subprocess.Popen(['explorer', '/select,', p_str])
                elif sys_plat == "Darwin": 
                    subprocess.call(["open", "-R", p_str])
                else: 
                    subprocess.call(["xdg-open", os.path.dirname(p_str)])
            except Exception as e:
                logger.warning(f"Failed opening folder in external manager: {e}")

        def _open_external_system_editor(self):
            selected = self.tree_files.selection()
            if not selected: 
                return
            path = self.node_path_map.get(selected[0])
            if not path: 
                return
            
            p_str = os.path.normpath(str(path))
            try:
                if platform.system() == "Windows": 
                    os.startfile(p_str)
                else: 
                    subprocess.call(["xdg-open", p_str])
            except Exception as e:
                logger.warning(f"Failed opening file in external editor: {e}")

        def _copy_absolute_path(self):
            selected = self.tree_files.selection()
            if not selected: 
                return
            path = self.node_path_map.get(selected[0])
            if path:
                self.clipboard_clear()
                self.clipboard_append(str(path.absolute()))
                self.status_var.set("Absolute system path copied to clipboard.")

        def _process_queue(self):
            try:
                for _ in range(500):
                    msg_type, data = self.event_queue.get_nowait()
                    
                    if msg_type == "error":
                        self.status_var.set("Index Fault")
                        messagebox.showerror("Execution Aborted", data)
                        self.is_searching = False
                        self.btn_search.config(state=tk.NORMAL)
                        
                    elif msg_type == "search_done":
                        res, elap = data
                        self.current_results = res
                        fmt = [f"{m.file_name}:{m.line_num} | {m.content[:100]}" for m in res]
                        if fmt: 
                            self.results_list.insert(tk.END, *fmt)
                        
                        warning = " (Hit Max Limit)" if len(res) == self.engine.MAX_MATCHES else ""
                        self.status_var.set(f"Loaded {len(res)} matches in {elap:.4f}s{warning}")
                        self.is_searching = False
                        self.btn_search.config(state=tk.NORMAL)
                        
                    elif msg_type == "load_progress":
                        c, t = data
                        self.progress['value'] = (c / t) * 100 if t > 0 else 0
                        self.status_var.set(f"Workspace loading... {c}/{t}")
                        
                    elif msg_type == "load_done":
                        err, elap = data
                        self.is_loading = False
                        self.progress.pack_forget()
                        if err:
                            messagebox.showwarning("Sync Warning", err)
                        
                        self.tree_files.delete(*self.tree_files.get_children())
                        
                        if self.current_workspace_path:
                            self.current_root_node = get_folder_structure(self.current_workspace_path)
                            self._insert_to_view_tree("", self.current_root_node)
                            if self.tree_files.get_children():
                                self.tree_files.item(self.tree_files.get_children()[0], open=True)

                        for tab in self.tab_notebook.tabs():
                            self.tab_notebook.forget(tab)
                        self.open_files_map.clear()
                        
                        self.results_list.delete(0, tk.END)
                        self.status_var.set(f"Loaded workspace: {len(self.engine.files)} indexed components.")
            except queue.Empty: 
                pass
            finally:
                delay = 15 if (self.is_searching or self.is_loading) else 200
                self.after(delay, self._process_queue)

def file_size_formatted(path: Path) -> str:
    try:
        size = path.stat().st_size
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} GB"
    except OSError:
        return "0 B"

def execute_cli(args):
    engine = NexaEngine()

    if not sys.stdin.isatty():
        piped_data = sys.stdin.read(engine.MAX_FILE_SIZE)
        node = FileNode("<stdin>", piped_data, "<stdin>")
        engine.files.append(node)
        engine.index["<stdin>"] = node
    else:
        err = engine.load_directory(args.dir)
        if err:
            if args.json:
                print(json.dumps({"error": err}))
            else:
                print(f"Error: {err}", file=sys.stderr)
            sys.exit(1)
        
    results, elapsed, search_err = engine.search(args.query, args.case, args.regex)

    if args.json:
        if search_err:
            print(json.dumps({"error": search_err}))
            sys.exit(1)
            
        out = {
            "query": args.query,
            "metrics": {"elapsed_sec": round(elapsed, 4), "total_matches": len(results), "limit_hit": len(results) == engine.MAX_MATCHES},
            "matches": [{"file": r.file_name, "line": r.line_num, "content": r.content} for r in results]
        }
        print(json.dumps(out, indent=2))
        return
        
    if search_err:
        print(f"Search Error: {search_err}", file=sys.stderr)
        sys.exit(1)
        
    for res in results:
        print(f"{res.file_name}:{res.line_num} | {res.content}")
        
    limit_warn = " (Hit Max Limit)" if len(results) == engine.MAX_MATCHES else ""
    print(f"\nYield: {len(results)} matches in {elapsed:.4f}s{limit_warn}", file=sys.stderr)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Nexa Core X - Code Intelligence Environment")
    parser.add_argument('query', nargs='?', help='Search query')
    parser.add_argument('-d', '--dir', default='.', help='Directory to search (default: .)')
    parser.add_argument('-r', '--regex', action='store_true', help='Use regex search')
    parser.add_argument('-c', '--case', action='store_true', help='Case sensitive search')
    parser.add_argument('-j', '--json', action='store_true', help='Output results in JSON format')
    parser.add_argument('--cli', action='store_true', help='Force CLI mode')

    try:
        args, unknown = parser.parse_known_args()
        has_query = bool(args.query)
        force_cli = args.cli
    except Exception:
        has_query = False
        force_cli = False

    if GUI_AVAILABLE and not force_cli and not has_query:
        app = NexaApp()
        app.mainloop()
    else:
        args = parser.parse_args()
        if not args.query:
            if GUI_AVAILABLE and not force_cli:
                app = NexaApp()
                app.mainloop()
            else:
                parser.error("A search query is required in CLI mode.")
        else:
            execute_cli(args)