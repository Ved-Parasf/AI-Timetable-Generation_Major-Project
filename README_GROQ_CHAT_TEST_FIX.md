# Groq V18 — fix false HTTP 403 during connection test

A PowerShell POST to `https://api.groq.com/openai/v1/chat/completions` succeeded, while the older application tested the *different* `GET /models` API and displayed HTTP 403. A successful chat-completions call is the meaningful check for the model used by Timetable Assistant.

This update changes **only the connection-test endpoint** and adds a regression test. It does not change CP-SAT generation, fairness, rescheduling, approval, SQLite history, Qwen/Groq tool routing, or timetable display.

## Run on Windows (PowerShell)

1. Stop the old app (`Ctrl+C`). Keep your old project and a backup of `data\coordinator_workflows.sqlite3`.
2. Extract the ZIP, open the inner `Timetable_Assistant_Groq_Cloud` folder in VS Code.
3. Run `py -m pip install -r requirements.txt`, then `py -m unittest discover -s tests -v`, then `py -m streamlit run timetable_app.py`.
4. Under Results -> Timetable Assistant -> Cloud AI settings, enter your working Groq key, choose `openai/gpt-oss-120b` and click **Connect Groq / Test key**.
5. A successful response is **Groq connected. Chat completions work with openai/gpt-oss-120b.**

The connection test sends only `Reply OK.`; it consumes a small amount of free-tier quota. API keys are not stored in the ZIP. Do not post keys in screenshots or chat.

This has only been tested with mocks here. The user's prior PowerShell test successfully called this same endpoint; the user should confirm the Streamlit connection after installation.
