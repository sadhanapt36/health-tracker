# Health Log

I built this for my grandpa, who sends his weight, sugar and blood pressure readings to my mom over WhatsApp. Instead of scrolling back through the chat and copying numbers down by hand, the app pulls them out of the conversation and keeps them in one place, so he can keep doing exactly what he already does.

It's meant for record-keeping and isn't medical advice. It never says whether a number is good or bad, and the only thing it flags is a value that looks like a typo.

## What it does

- **Import** a WhatsApp chat export, either the `.txt` file or the `.zip` that includes photos. Importing again later only adds the messages that are new.
- **Read** weight from typed messages, sugar from FreeStyle Libre screenshots, and blood pressure and pulse from photos of his Omron monitor.
- **Review** every reading before it's saved, so nothing enters the record without a person confirming it.
- **History** shows charts and a summary, plus a version that can be printed or sent to his doctor.

All of the photo reading happens on your own computer, so there's no account, API key or cost involved.

## Setup (Mac)

```
brew install tesseract
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

Then open http://localhost:8501. You'll need Python 3.10 or newer, and Tesseract is only used for the sugar screenshots.

## How I use it

1. In WhatsApp, open the family chat and choose **Export Chat → Attach Media**.
2. In the **Import** tab, upload the file and import it.
3. Each photo is scanned once, and if the app can't read one, I type the numbers in myself.
4. In **Review**, I either save a reading, skip it, or mark it "Not a reading". Clean weight messages can be confirmed all at once.
5. The **History** tab has the charts and the summary for the doctor.

To look around without using real data, choose **Sample data (fictional)** in the sidebar.

## Where the data lives

Everything is stored in `data/health.db`, next to the app. That folder is ignored by git on purpose because it contains real health information, so to back it up, just copy that file.

## What's in here

```
app.py                  the Streamlit app
.streamlit/config.toml  colors and font
health_tracker/
  chat_export.py        reads WhatsApp exports (iPhone and Android)
  parsing.py            finds weight, BP and sugar in text
  vision.py, lcd7seg.py reads the Libre screenshots and the Omron display
  scan.py, media.py     photo scanning and lookup
  review.py             the confirm-all-at-once logic
  trends.py             summaries and chart data
  db.py                 SQLite storage
sample_data/            a fictional chat for trying it out
tests/                  pytest
```

Run the tests with `pytest`.

## Current limitations

- The blood pressure reader is strict, so if a photo is blurry or taken at an odd angle it skips it rather than guessing, and the numbers get typed in by hand.
- Units are never guessed, which means a reading without a clear unit stays a draft.
- The "WhatsApp Web" option is experimental and needs Playwright, but it isn't needed for normal use.
