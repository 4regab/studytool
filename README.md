# PDF to Flashcards

Turns study PDFs into standalone question-and-answer cards and exports them as a
`front,back` CSV for Anki. Runs on your own computer with Python and SQLite.

## Setup

1. Install [uv](https://docs.astral.sh/uv/).
2. In this folder, run `uv sync` to install the pinned dependencies.
3. Set your AI key. The app sends the text of the pages you select to DeepSeek.

   ```powershell
   $env:DEEPSEEK_API_KEY = "your-key"   # optional: $env:STUDYTOOL_DEEPSEEK_MODEL = "deepseek-flash"
   ```

## Run

```powershell
uv run python run.py
```

Open http://127.0.0.1:8501. The launcher starts the app and one background worker.
Closing the browser does not stop processing. Press Ctrl+C in the terminal to stop both.
Unfinished work resumes on the next launch.

Data lives in `data/` (set `STUDYTOOL_DATA_DIR` to move it). Use `STUDYTOOL_PORT` to change the port.

## Use

1. **Documents:** upload a PDF. Pick the content pages and, if the answers are elsewhere,
   the answer-key pages. Start with a small pilot range (2-5 pages) to check quality and cost.
2. **Processing:** watch progress. Cancel or retry failed steps here.
3. **Review:** edit, approve, or reject each card next to its source page.
   Cards with flags need a short note before you can approve them.
4. **Export:** download an Anki deck file (`.apkg`) or a CSV. Both contain approved cards only.

## Import into Anki

**Anki deck file (recommended):** double-click the `.apkg` file, or use File > Import in Anki.
The cards arrive as Basic notes in a deck named after your deck title. Importing a newer
export of the same deck updates those cards instead of adding duplicates.

**CSV:**

1. File > Import, and choose the CSV.
2. Note type: **Basic**. Field separator: **Comma**.
3. Map field 1 to **Front** and field 2 to **Back**.
4. Skip the first row. It is the `front,back` header.

## Back up the database

Do not copy `studytool.db` while the app is running. Use the built-in backup instead,
which is safe while the worker is writing:

```powershell
uv run python -c "from studytool import db; db.backup(db.connect(), 'backup.db')"
```

## Notes

- The app listens on 127.0.0.1 only and has no login. Do not expose the port to a network.
- AI usage costs money and is rate-limited by the provider. If the account runs out of
  balance or quota, processing stops with a message saying so. Your finished cards stay
  reviewable and exportable offline.
- Token counts and costs shown in the app are estimates.
- Run the tests with `uv run pytest -q`.
