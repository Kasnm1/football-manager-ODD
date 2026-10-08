from __future__ import annotations

import gzip
import json
import math
import os
import shutil
import tempfile
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any


SUPPORTED_SCHEMA_VERSION = 1
MINOR_SCALE = Decimal("100")


class FmoddEditorError(RuntimeError):
    """Raised when an account container cannot be edited safely."""


def _validated_payload(path: Path, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("documents"), dict):
        raise FmoddEditorError("文件不是有效的 FMODD 账户容器")
    raw_version = payload.get("schema_version", 1)
    if isinstance(raw_version, bool):
        raise FmoddEditorError("账户容器版本无效")
    try:
        version = int(raw_version)
    except (TypeError, ValueError) as error:
        raise FmoddEditorError("账户容器版本无效") from error
    if version > SUPPORTED_SCHEMA_VERSION:
        raise FmoddEditorError(
            f"该文件由更新版本创建（{version}），当前编辑器只支持到 "
            f"{SUPPORTED_SCHEMA_VERSION}"
        )
    stored_scope = str(payload.get("scope_id") or "").strip()
    if stored_scope and stored_scope != path.stem:
        raise FmoddEditorError(
            f"账户作用域与文件名不一致：{stored_scope} != {path.stem}"
        )
    return payload


def load_fmodd(path: str | Path) -> dict[str, Any]:
    resolved = Path(path)
    try:
        decoded = gzip.decompress(resolved.read_bytes()).decode("utf-8")
        payload = json.loads(decoded)
    except (OSError, EOFError, gzip.BadGzipFile, UnicodeDecodeError) as error:
        raise FmoddEditorError("无法解压该文件；它可能不是 .fmodd 存档") from error
    except json.JSONDecodeError as error:
        raise FmoddEditorError("文件中的 JSON 数据已损坏") from error
    return _validated_payload(resolved, payload)


def _encode_payload(path: Path, payload: dict[str, Any]) -> bytes:
    validated = _validated_payload(path, payload)
    validated["schema_version"] = SUPPORTED_SCHEMA_VERSION
    validated["scope_id"] = path.stem
    validated["updated_at"] = datetime.now().isoformat(timespec="seconds")
    raw = json.dumps(
        validated, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return gzip.compress(raw, compresslevel=6, mtime=0)


def save_fmodd(path: str | Path, payload: dict[str, Any]) -> Path:
    """Atomically save a container and return its timestamped backup path."""
    resolved = Path(path)
    if not resolved.is_file():
        raise FmoddEditorError("原始 .fmodd 文件不存在")
    encoded = _encode_payload(resolved, payload)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = resolved.with_name(f"{resolved.name}.editor-backup-{timestamp}")
    try:
        shutil.copy2(resolved, backup)
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{resolved.name}.", suffix=".tmp",
            dir=resolved.parent, delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, resolved)
    except PermissionError as error:
        raise FmoddEditorError("文件正被占用；请完全关闭 FMODD 后再保存") from error
    except OSError as error:
        raise FmoddEditorError(f"保存失败：{error}") from error
    finally:
        temporary_path = locals().get("temporary")
        if isinstance(temporary_path, Path):
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
    return backup


def _parse_major_amount(value: str | int | float | Decimal) -> tuple[Decimal, int]:
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as error:
        raise FmoddEditorError("金额必须是有效数字") from error
    if not amount.is_finite() or amount < 0:
        raise FmoddEditorError("金额必须是大于或等于 0 的有限数字")
    minor = int((amount * MINOR_SCALE).to_integral_value(rounding=ROUND_HALF_UP))
    return amount, minor


def _minor_balance(document: dict[str, Any], field: str) -> int:
    minor_key = f"{field}_minor"
    if minor_key in document:
        try:
            return int(document[minor_key])
        except (TypeError, ValueError) as error:
            raise FmoddEditorError(f"{minor_key} 不是整数") from error
    value = document.get(field, 0)
    if isinstance(value, float) and not math.isfinite(value):
        raise FmoddEditorError(f"{field} 不是有限数字")
    return _parse_major_amount(value)[1]


def read_balance(payload: dict[str, Any], account: str) -> Decimal | None:
    documents = payload.get("documents")
    if not isinstance(documents, dict):
        return None
    if account == "wallet":
        key, field = "wallet", "balance"
    elif account == "bank":
        key, field = "economy", "general_balance"
    else:
        raise FmoddEditorError("未知资金账户")
    document = documents.get(key)
    if not isinstance(document, dict):
        return None
    return Decimal(_minor_balance(document, field)) / MINOR_SCALE


def set_balance(payload: dict[str, Any], account: str, value: Any) -> None:
    documents = payload.get("documents")
    if not isinstance(documents, dict):
        raise FmoddEditorError("账户容器缺少 documents")
    if account == "wallet":
        key, field, balance_after = "wallet", "balance", "balance_after"
    elif account == "bank":
        key, field, balance_after = (
            "economy", "general_balance", "general_balance_after"
        )
    else:
        raise FmoddEditorError("未知资金账户")
    document = documents.get(key)
    if not isinstance(document, dict):
        raise FmoddEditorError(f"当前存档没有 {key} 文档")
    amount, new_minor = _parse_major_amount(value)
    old_minor = _minor_balance(document, field)
    delta_minor = new_minor - old_minor
    document[field] = float(amount)
    document[f"{field}_minor"] = new_minor
    transaction = {
        "id": str(uuid.uuid4()),
        "at": datetime.now().isoformat(timespec="seconds"),
        "type": "manual_editor",
        "amount": float(Decimal(delta_minor) / MINOR_SCALE),
        "amount_minor": delta_minor,
        balance_after: float(amount),
        f"{balance_after}_minor": new_minor,
    }
    transactions = document.setdefault("transactions", [])
    if not isinstance(transactions, list):
        raise FmoddEditorError(f"{key}.transactions 不是列表")
    transactions.append(transaction)
    if account == "bank":
        document["transactions"] = transactions[-1000:]


def _default_open_directory() -> Path:
    candidates = (
        Path(__file__).resolve().parents[1] / "data" / "saves",
        Path.home() / "Documents" / "FMODD" / "saves",
    )
    return next((path for path in candidates if path.is_dir()), Path.home())


class FmoddSaveEditor:
    def __init__(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = tk.Tk()
        self.root.title("FMODD 存档编辑器")
        self.root.geometry("1080x700")
        self.root.minsize(820, 520)
        self.path: Path | None = None
        self.payload: dict[str, Any] | None = None
        self.current_key: str | None = None
        self.editor_snapshot = ""
        self.dirty = False
        self.status = tk.StringVar(value="请打开一个 .fmodd 文件")
        self.account_kind = tk.StringVar(value="wallet")
        self.balance_value = tk.StringVar()
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Control-o>", lambda _event: self.open_file())
        self.root.bind("<Control-s>", lambda _event: self.save_file())

    def _build_ui(self) -> None:
        from tkinter import ttk

        toolbar = ttk.Frame(self.root, padding=8)
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="打开", command=self.open_file).pack(side="left")
        ttk.Button(toolbar, text="保存", command=self.save_file).pack(side="left", padx=6)
        ttk.Button(toolbar, text="格式化当前文档", command=self.format_document).pack(
            side="left"
        )
        ttk.Label(
            toolbar, text="保存前请关闭 FMODD；每次保存会自动创建独立备份。",
            foreground="#9a4b00",
        ).pack(side="right")

        quick = ttk.LabelFrame(self.root, text="余额快捷修改", padding=8)
        quick.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Combobox(
            quick, textvariable=self.account_kind, state="readonly", width=12,
            values=("wallet", "bank"),
        ).pack(side="left")
        ttk.Label(quick, text="wallet=投注钱包，bank=FMODD 银行").pack(
            side="left", padx=(8, 16)
        )
        ttk.Entry(quick, textvariable=self.balance_value, width=22).pack(side="left")
        ttk.Button(quick, text="写入余额", command=self.apply_balance).pack(
            side="left", padx=8
        )
        ttk.Button(quick, text="读取当前余额", command=self.refresh_balance).pack(
            side="left"
        )

        pane = ttk.PanedWindow(self.root, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=8)
        left = ttk.Frame(pane)
        right = ttk.Frame(pane)
        pane.add(left, weight=1)
        pane.add(right, weight=4)
        ttk.Label(left, text="文档区块").pack(anchor="w")
        self.documents = self.tk.Listbox(left, exportselection=False)
        self.documents.pack(fill="both", expand=True, pady=(4, 0))
        self.documents.bind("<<ListboxSelect>>", self._select_document)

        ttk.Label(right, text="当前区块 JSON（修改后点击保存）").pack(anchor="w")
        editor_frame = ttk.Frame(right)
        editor_frame.pack(fill="both", expand=True, pady=(4, 0))
        self.editor = self.tk.Text(
            editor_frame, wrap="none", undo=True, font=("Consolas", 10)
        )
        y_scroll = ttk.Scrollbar(editor_frame, orient="vertical", command=self.editor.yview)
        x_scroll = ttk.Scrollbar(editor_frame, orient="horizontal", command=self.editor.xview)
        self.editor.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.editor.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        editor_frame.rowconfigure(0, weight=1)
        editor_frame.columnconfigure(0, weight=1)
        self.editor.bind("<<Modified>>", self._mark_text_modified)

        ttk.Label(self.root, textvariable=self.status, relief="sunken", anchor="w").pack(
            fill="x", padx=8, pady=8
        )

    def _mark_text_modified(self, _event: Any = None) -> None:
        if self.editor.edit_modified():
            self.dirty = True
            self.editor.edit_modified(False)

    def _current_text(self) -> str:
        return self.editor.get("1.0", "end-1c")

    def _apply_current_document(self) -> bool:
        if self.payload is None or self.current_key is None:
            return True
        text = self._current_text()
        if text == self.editor_snapshot:
            return True
        try:
            document = json.loads(text)
        except json.JSONDecodeError as error:
            from tkinter import messagebox

            messagebox.showerror(
                "JSON 格式错误",
                f"第 {error.lineno} 行，第 {error.colno} 列：{error.msg}",
            )
            return False
        self.payload["documents"][self.current_key] = document
        self.editor_snapshot = json.dumps(document, ensure_ascii=False, indent=2)
        self.dirty = True
        return True

    def _select_document(self, _event: Any = None) -> None:
        selection = self.documents.curselection()
        if not selection or self.payload is None:
            return
        selected_key = str(self.documents.get(selection[0]))
        if selected_key == self.current_key:
            return
        if not self._apply_current_document():
            if self.current_key is not None:
                keys = list(self.payload["documents"])
                self.documents.selection_clear(0, "end")
                self.documents.selection_set(keys.index(self.current_key))
            return
        self.current_key = selected_key
        document = self.payload["documents"][selected_key]
        self.editor_snapshot = json.dumps(document, ensure_ascii=False, indent=2)
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", self.editor_snapshot)
        self.editor.edit_modified(False)
        self.status.set(f"正在编辑：{selected_key}")

    def _confirm_discard(self) -> bool:
        if not self.dirty:
            return True
        from tkinter import messagebox

        return bool(messagebox.askyesno("放弃修改", "有尚未保存的修改，确定放弃吗？"))

    def open_file(self) -> None:
        if not self._confirm_discard():
            return
        from tkinter import filedialog, messagebox

        selected = filedialog.askopenfilename(
            title="打开 FMODD 存档",
            initialdir=_default_open_directory(),
            filetypes=(("FMODD 存档", "*.fmodd"), ("所有文件", "*.*")),
        )
        if not selected:
            return
        try:
            payload = load_fmodd(selected)
        except FmoddEditorError as error:
            messagebox.showerror("无法打开", str(error))
            return
        self.path = Path(selected)
        self.payload = payload
        self.current_key = None
        self.dirty = False
        self.documents.delete(0, "end")
        keys = sorted(payload["documents"])
        for key in keys:
            self.documents.insert("end", key)
        self.root.title(f"FMODD 存档编辑器 — {self.path.name}")
        if keys:
            self.documents.selection_set(0)
            self._select_document()
        self.status.set(f"已打开：{self.path}")

    def format_document(self) -> None:
        if self.current_key is None:
            return
        try:
            document = json.loads(self._current_text())
        except json.JSONDecodeError as error:
            from tkinter import messagebox

            messagebox.showerror("JSON 格式错误", f"第 {error.lineno} 行：{error.msg}")
            return
        formatted = json.dumps(document, ensure_ascii=False, indent=2)
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", formatted)
        self.dirty = formatted != self.editor_snapshot or self.dirty

    def refresh_balance(self) -> None:
        if self.payload is None:
            return
        try:
            value = read_balance(self.payload, self.account_kind.get())
        except FmoddEditorError as error:
            from tkinter import messagebox

            messagebox.showerror("无法读取余额", str(error))
            return
        self.balance_value.set("" if value is None else format(value, "f"))

    def apply_balance(self) -> None:
        if self.payload is None:
            return
        from tkinter import messagebox

        if not self._apply_current_document():
            return
        try:
            set_balance(self.payload, self.account_kind.get(), self.balance_value.get())
        except FmoddEditorError as error:
            messagebox.showerror("无法修改余额", str(error))
            return
        self.dirty = True
        if self.current_key in {"wallet", "economy"}:
            document = self.payload["documents"][self.current_key]
            self.editor_snapshot = json.dumps(document, ensure_ascii=False, indent=2)
            self.editor.delete("1.0", "end")
            self.editor.insert("1.0", self.editor_snapshot)
            self.editor.edit_modified(False)
        self.status.set("余额已写入内存；点击“保存”才会写回文件")

    def save_file(self) -> None:
        if self.path is None or self.payload is None:
            return
        from tkinter import messagebox

        if not self._apply_current_document():
            return
        try:
            backup = save_fmodd(self.path, self.payload)
        except (FmoddEditorError, ValueError) as error:
            messagebox.showerror("保存失败", str(error))
            return
        self.dirty = False
        self.editor_snapshot = self._current_text()
        self.status.set(f"保存成功；备份：{backup.name}")
        messagebox.showinfo("保存成功", f"已保存。\n备份文件：\n{backup}")

    def _on_close(self) -> None:
        if self._confirm_discard():
            self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    try:
        FmoddSaveEditor().run()
    except ModuleNotFoundError as error:
        if error.name == "tkinter":
            raise SystemExit("当前 Python 未安装 Tkinter，无法启动图形界面") from error
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
