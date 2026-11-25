"""
build_pnl.py -- generates wednesday-nifty-pnl.xlsx, a 2-sheet P&L tracker
(Total PnL dashboard, Trade Log) for this Wednesday 3-Day Breakout Credit
Spread strategy, read from results/trade_history.json (one record per
closed live trade, appended by exit_executor.py right after a trade
fully closes).

Modeled on nifty-option-btst's results/build_pnl.py -- same layout, same
openpyxl styling, same "every computed cell is a formula string" and
"Google Drive push via rclone, never raises" conventions, so this stays
consistent with the other strategies already writing to the same
gdrive:Strategy PnL/ folder. Formulas avoid XLOOKUP/XMATCH/SORT/FILTER/
UNIQUE/SEQUENCE so the workbook stays Google Sheets compatible once
pushed there.

Unlike BTST's single unconditional exit path, this strategy actually
distinguishes STOP_LOSS / PROFIT_TARGET / TIME_EXIT / MANUAL exits (see
exit_executor.py), so the dashboard adds a "BY EXIT REASON" breakdown
table (counts + P&L per reason, COUNTIFS/SUMIFS against Trade Log) on
top of BTST's Strategy Snapshot cards -- this is the piece that actually
answers "was it a stop loss or a target that closed this trade".

Google Drive sync: rclone copy to the SAME remote path every time
("gdrive:Strategy PnL/"), so it overwrites the existing
wednesday-nifty-pnl Google Sheet in place (same file id) rather than
creating a new file. Never pushed if the build itself failed (the push
line is only reached after wb.save() succeeds), and a push failure
(rclone not installed, remote not configured, network) only warns -- it
never raises past main().

There's deliberately no cron here -- this is called by
exit_executor.py right after a trade closes (same spot that already
archives to trade_history.json). You can also just run it manually any
time:

Usage:
    python3 results/build_pnl.py
"""

import subprocess
import sys
from datetime import date
from pathlib import Path

# Repo root on sys.path -- needed whether this runs standalone
# (`python3 results/build_pnl.py`) or imported as `results.build_pnl`
# from exit_executor.py (which already does this, but importing this
# module directly elsewhere shouldn't rely on that).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openpyxl import Workbook
from openpyxl.formatting.rule import CellIsRule, DataBarRule, FormulaRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from results.trade_history import load_trade_history

RESULTS_DIR = Path(__file__).resolve().parent
OUT_PATH = RESULTS_DIR / "wednesday-nifty-pnl.xlsx"
DRIVE_REMOTE_PATH = "gdrive:Strategy PnL/"

EXIT_REASONS = ("PROFIT_TARGET", "STOP_LOSS", "TIME_EXIT", "MANUAL")

# ── Number formats ───────────────────────────────────────────────────────────
INR = '₹#,##0.00;(₹#,##0.00);"-"'
PCT = '0.00%;(0.00%);"-"'
NUM = '#,##0;(#,##0);"-"'
XMULT = '0.00"x"'
DATE = "yyyy-mm-dd"
PRICE = "#,##0.00"

# ── Fonts / fills ─────────────────────────────────────────────────────────────
FONT_NAME = "Arial"
HEADER_FONT = Font(name=FONT_NAME, bold=True, color="FFFFFFFF")
HEADER_FILL = PatternFill(start_color="FF1F3864", end_color="FF1F3864", fill_type="solid")
FORMULA_FONT = Font(name=FONT_NAME, color="FF000000")
LABEL_FONT = Font(name=FONT_NAME, color="FF000000", bold=True)
TITLE_FONT = Font(name=FONT_NAME, bold=True, size=14, color="FF1F3864")
SUBTITLE_FONT = Font(name=FONT_NAME, italic=True, size=10, color="FF808080")
RED_FONT = Font(name=FONT_NAME, color="FFFF0000")
CENTER = Alignment(horizontal="center", vertical="center")
LEFT = Alignment(horizontal="left", vertical="center")


def set_title_subtitle(ws, title: str, subtitle: str, ncols: int) -> None:
    last_col = get_column_letter(ncols)
    ws.merge_cells(f"A1:{last_col}1")
    c = ws["A1"]; c.value = title; c.font = TITLE_FONT; c.alignment = LEFT
    ws.row_dimensions[1].height = 22

    ws.merge_cells(f"A2:{last_col}2")
    c = ws["A2"]; c.value = subtitle; c.font = SUBTITLE_FONT; c.alignment = LEFT
    ws.row_dimensions[2].height = 16


def style_header_row(ws, row: int, ncols: int) -> None:
    for col in range(1, ncols + 1):
        cell = ws.cell(row=row, column=col)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = CENTER


def style_formula(cell, number_format: str | None = None) -> None:
    cell.font = FORMULA_FONT
    if number_format:
        cell.number_format = number_format


# ── Trade Log ─────────────────────────────────────────────────────────────────
TL_FIRST_ROW = 4
TL_HEADERS = [
    "Entry Date", "Trigger", "Option Type", "Expiry",
    "Short Symbol (ATM)", "Short Qty", "Short Entry Price", "Short Exit Price",
    "Hedge Symbol", "Hedge Qty", "Hedge Entry Price", "Hedge Exit Price",
    "Entry Credit", "Exit Date", "Exit Reason", "Gross P&L",
]


def _trade_row_values(trade: dict) -> dict:
    legs = trade.get("legs") or {}
    short_leg = legs.get("SHORT_ATM") or {}
    hedge_leg = legs.get("LONG_HEDGE") or {}
    return {
        "A": trade.get("entry_date", ""), "B": trade.get("trigger", ""),
        "C": trade.get("option_type", ""), "D": trade.get("expiry", ""),
        "E": short_leg.get("tradingsymbol", ""), "F": short_leg.get("quantity", ""),
        "G": short_leg.get("avg_price", ""), "H": short_leg.get("exit_price", ""),
        "I": hedge_leg.get("tradingsymbol", ""), "J": hedge_leg.get("quantity", ""),
        "K": hedge_leg.get("avg_price", ""), "L": hedge_leg.get("exit_price", ""),
        "M": trade.get("entry_credit", ""), "N": trade.get("exit_date", ""),
        "O": trade.get("exit_reason", ""),
    }


def build_trade_log(ws, trades: list[dict]) -> int:
    """Returns the last row written (TL_FIRST_ROW - 1 if no trades)."""
    set_title_subtitle(
        ws, "Trade Log",
        "One row per closed wednesday_nifty credit spread (both legs) -- "
        "regenerated from results/trade_history.json by results/build_pnl.py.",
        len(TL_HEADERS),
    )

    for col, header in enumerate(TL_HEADERS, start=1):
        ws.cell(row=3, column=col, value=header)
    style_header_row(ws, 3, len(TL_HEADERS))

    widths = [12, 16, 11, 12, 20, 9, 14, 14, 20, 9, 14, 14, 12, 12, 13, 12]
    for col, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(col)].width = w

    manual_fmts = {"A": DATE, "G": PRICE, "H": PRICE, "F": NUM,
                   "K": PRICE, "L": PRICE, "J": NUM, "M": PRICE, "N": DATE}

    last_row = TL_FIRST_ROW - 1
    for offset, trade in enumerate(trades):
        r = TL_FIRST_ROW + offset
        last_row = r
        row_vals = _trade_row_values(trade)
        for col_letter, val in row_vals.items():
            cell = ws[f"{col_letter}{r}"]
            cell.value = val
            if col_letter in manual_fmts:
                cell.number_format = manual_fmts[col_letter]

        # Short leg: sold at entry, bought back at exit -> profit = entry - exit.
        # Hedge leg: bought at entry, sold back at exit -> profit = exit - entry.
        p_formula = (
            f'=IF(OR($H{r}="",$L{r}=""),"",'
            f'(($G{r}-$H{r})*$F{r})+(($L{r}-$K{r})*$J{r}))'
        )
        cell_p = ws[f"P{r}"]; cell_p.value = p_formula; style_formula(cell_p, INR)

    if trades:
        ws.conditional_formatting.add(
            f"P{TL_FIRST_ROW}:P{last_row}",
            CellIsRule(operator="lessThan", formula=["0"], font=RED_FONT),
        )
        ws.auto_filter.ref = f"A3:P{last_row}"

    ws.freeze_panes = "E4"
    return last_row


# ── Total PnL (dashboard) ────────────────────────────────────────────────────
BAND_FONT = Font(name=FONT_NAME, bold=True, size=11, color="FF1F3864")
BAND_FILL = PatternFill(start_color="FFD9E2F3", end_color="FFD9E2F3", fill_type="solid")
CARD_LABEL_FONT = Font(name=FONT_NAME, size=9, color="FFFFFFFF")
CARD_NUM_FONT = Font(name=FONT_NAME, size=26, bold=True, color="FFFFFFFF")
GREEN_FILL = PatternFill(start_color="FF2E7D32", end_color="FF2E7D32", fill_type="solid")
RED_FILL = PatternFill(start_color="FFC62828", end_color="FFC62828", fill_type="solid")
CARD_NAVY = "FF1F3864"
CARD_TEAL = "FF1B7A75"
CARD_AMBER = "FFC77D02"
CARD_PURPLE = "FF5B2C83"
DASHBOARD_COLS = 16

CARD_LABEL_ROW = 5
CARD_NUM_TOP = 6
CARD_NUM_BOTTOM = 8
BAND_STATS = 10
BAND_REASON = 19


def _band(ws, row: int, text: str) -> None:
    last_col = get_column_letter(DASHBOARD_COLS)
    ws.merge_cells(f"A{row}:{last_col}{row}")
    cell = ws[f"A{row}"]
    cell.value = text
    cell.font = BAND_FONT
    cell.fill = BAND_FILL
    cell.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws.row_dimensions[row].height = 18
    for col in range(1, DASHBOARD_COLS + 1):
        ws.cell(row=row, column=col).fill = BAND_FILL


def _kpi_card(ws, col_start, col_end, label, formula, number_format, fill_hex, conditional) -> None:
    first_col_letter = get_column_letter(col_start)
    last_col_letter = get_column_letter(col_end)
    label_range = f"{first_col_letter}{CARD_LABEL_ROW}:{last_col_letter}{CARD_LABEL_ROW}"
    num_range = f"{first_col_letter}{CARD_NUM_TOP}:{last_col_letter}{CARD_NUM_BOTTOM}"

    ws.merge_cells(label_range)
    ws.merge_cells(num_range)

    fill = PatternFill(start_color=fill_hex, end_color=fill_hex, fill_type="solid")
    for row in (CARD_LABEL_ROW, *range(CARD_NUM_TOP, CARD_NUM_BOTTOM + 1)):
        for col in range(col_start, col_end + 1):
            ws.cell(row=row, column=col).fill = fill

    label_cell = ws[f"{first_col_letter}{CARD_LABEL_ROW}"]
    label_cell.value = label
    label_cell.font = CARD_LABEL_FONT
    label_cell.alignment = CENTER

    num_cell = ws[f"{first_col_letter}{CARD_NUM_TOP}"]
    num_cell.value = formula
    num_cell.font = CARD_NUM_FONT
    num_cell.alignment = CENTER
    num_cell.number_format = number_format

    if conditional:
        anchor = f"${first_col_letter}${CARD_NUM_TOP}"
        for rng in (label_range, num_range):
            ws.conditional_formatting.add(rng, FormulaRule(formula=[f"{anchor}>=0"], fill=GREEN_FILL))
            ws.conditional_formatting.add(rng, FormulaRule(formula=[f"{anchor}<0"], fill=RED_FILL))


def build_total_pnl_dashboard(ws, tl_last_row: int) -> None:
    set_title_subtitle(
        ws, "Total PnL",
        "Wednesday 3-Day Breakout Credit Spread -- everything below "
        "calculates automatically from Trade Log.",
        DASHBOARD_COLS,
    )

    ws.column_dimensions["A"].width = 4
    for group_start in (2, 6, 10, 14):
        for offset in range(3):
            ws.column_dimensions[get_column_letter(group_start + offset)].width = 15
    for gap_col in (5, 9, 13):
        ws.column_dimensions[get_column_letter(gap_col)].width = 8

    p_range = f"'Trade Log'!$P$4:$P${max(tl_last_row, 4)}"
    reason_range = f"'Trade Log'!$O$4:$O${max(tl_last_row, 4)}"

    _band(ws, 4, "STRATEGY SNAPSHOT")
    card_defs = [
        (2, 4, "GROSS P&L (₹)", f"=SUM({p_range})", INR, CARD_NAVY, True),
        (6, 8, "WIN RATE", f'=IFERROR(COUNTIF({p_range},">0")/COUNT({p_range}),"")', PCT, CARD_AMBER, False),
        (10, 12, "TOTAL TRADES", f"=COUNT({p_range})", NUM, CARD_TEAL, False),
        (14, 16, "PROFIT FACTOR",
         f'=IFERROR(SUMIF({p_range},">0")/ABS(SUMIF({p_range},"<0")),"")', XMULT, CARD_PURPLE, False),
    ]
    for col_start, col_end, label, formula, fmt, fill_hex, conditional in card_defs:
        _kpi_card(ws, col_start, col_end, label, formula, fmt, fill_hex, conditional)

    _band(ws, BAND_STATS, "DETAILED STATS")
    ws["B11"].value = "Metric"
    ws["C11"].value = "Value"
    for addr in ("B11", "C11"):
        ws[addr].font = HEADER_FONT
        ws[addr].fill = HEADER_FILL
        ws[addr].alignment = CENTER

    stats_rows = [
        (12, "Winning Trades", f'=COUNTIF({p_range},">0")', NUM),
        (13, "Losing Trades", f'=COUNTIF({p_range},"<0")', NUM),
        (14, "Average Win (₹)", f'=IFERROR(AVERAGEIF({p_range},">0"),"")', INR),
        (15, "Average Loss (₹)", f'=IFERROR(AVERAGEIF({p_range},"<0"),"")', INR),
        (16, "Best Trade (₹)", f'=IFERROR(MAX({p_range}),"")', INR),
        (17, "Worst Trade (₹)", f'=IFERROR(MIN({p_range}),"")', INR),
    ]
    for row, label, formula, fmt in stats_rows:
        ws[f"B{row}"].value = label
        ws[f"B{row}"].font = LABEL_FONT
        cell_c = ws[f"C{row}"]
        cell_c.value = formula
        style_formula(cell_c, fmt)

    ws.conditional_formatting.add(
        "C14:C17",
        DataBarRule(start_type="min", start_value=None, end_type="max", end_value=None, color="638EC6"),
    )

    # -- BY EXIT REASON: did a stop loss or a target close this trade? --
    _band(ws, BAND_REASON, "BY EXIT REASON")
    headers = ["Exit Reason", "Trades", "Total P&L (₹)"]
    for col, header in enumerate(headers, start=2):
        cell = ws.cell(row=BAND_REASON + 1, column=col, value=header)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = CENTER

    for offset, reason in enumerate(EXIT_REASONS):
        row = BAND_REASON + 2 + offset
        ws[f"B{row}"].value = reason
        ws[f"B{row}"].font = LABEL_FONT
        count_formula = f'=COUNTIFS({reason_range},"{reason}")'
        cell_c = ws[f"C{row}"]; cell_c.value = count_formula; style_formula(cell_c, NUM)
        pnl_formula = f'=SUMIFS({p_range},{reason_range},"{reason}")'
        cell_d = ws[f"D{row}"]; cell_d.value = pnl_formula; style_formula(cell_d, INR)

    last_reason_row = BAND_REASON + 1 + len(EXIT_REASONS)
    ws.conditional_formatting.add(
        f"D{BAND_REASON + 2}:D{last_reason_row}",
        CellIsRule(operator="lessThan", formula=["0"], font=RED_FONT),
    )

    ws.freeze_panes = "A9"


def _push_to_drive(out_path: Path) -> None:
    """rclone copy to the same remote path every time -- an in-place
    overwrite of the existing wednesday-nifty-pnl Google Sheet (same
    file id), not a new upload. Only called after a successful save (see
    main()). Never raises -- a missing rclone binary/unconfigured remote/
    network hiccup should warn, not break report generation."""
    try:
        result = subprocess.run(
            ["rclone", "copy", str(out_path), DRIVE_REMOTE_PATH],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            print(f"    Pushed to {DRIVE_REMOTE_PATH}")
        else:
            print(f"    WARNING: rclone push failed: {result.stderr.strip() or result.stdout.strip()}")
    except Exception as exc:  # noqa: BLE001 -- e.g. rclone not installed
        print(f"    WARNING: rclone push failed ({exc})")


def main(out_path: Path = OUT_PATH) -> None:
    trades = load_trade_history()

    wb = Workbook()
    ws_total = wb.active
    ws_total.title = "Total PnL"
    ws_log = wb.create_sheet("Trade Log")

    tl_last_row = build_trade_log(ws_log, trades)
    build_total_pnl_dashboard(ws_total, tl_last_row)

    for ws in wb.worksheets:
        ws.sheet_view.showGridLines = False

    wb.save(out_path)
    print(f"Wrote {out_path} ({len(trades)} trade(s))" if trades
          else f"Wrote {out_path} (no closed trades yet)")

    _push_to_drive(out_path)


if __name__ == "__main__":
    main()
