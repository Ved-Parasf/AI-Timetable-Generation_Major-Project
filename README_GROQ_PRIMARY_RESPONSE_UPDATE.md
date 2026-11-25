# Timora Groq: Single, Grounded AI Answers

This update addresses the complaint that Timora appears to answer mostly from Python even when Groq is configured.

## Changed behavior

- For ordinary **Chat & Tasks** questions, Python first retrieves verified facts from the selected timetable. When **Use GroqCloud AI understanding** and **Groq answers from verified timetable data** are enabled and a valid key is configured, Groq writes **one final response** using those facts.
- The reply is labeled `Groq response · timetable facts verified by Python` only when a model response was accepted.
- If the model is unavailable, fails quality checks, or the question needs clarification, the original Python answer is used; a fallback label is shown. This is intentional and prevents invented faculty names, course codes, time slots, collisions and approvals.
- **Rescheduling, move confirmation, and feasibility checks stay validator-controlled.** Groq does not edit or approve timetables.
- Fixed the **uneven weekday workload** query to rank teacher workload *spread* rather than simply reporting the busiest teacher.
- Fixed **source session selection** when a user says they want to reschedule a class and then supplies an exact current day and period in a separate message.

## Install

1. Stop Streamlit and back up your project folder.
2. Copy these changed Python files into the main Timora project directory (where `timetable_app.py` is located):
   - `timetable_app.py`
   - `timetable_cloud_ai.py`
   - `timetable_response_composer.py`
   - `timetable_chat_intents.py`
   - `timetable_chat_moves.py`
3. Optionally copy `tests/test_groq_primary_answer_mode.py` into `tests/`.
4. Start: `python -m streamlit run timetable_app.py`.
5. Open **Timora → Cloud AI settings (Groq)**. Paste your Groq key, select a supported model, click **Connect Groq / Test key** and enable both Groq toggles.
6. Ask **Who teaches CSL315?**. The reply should show one answer and the Groq status label. If it shows local fallback, check the settings / connection. Repeat with **Which teachers have the most uneven teaching workload across weekdays?**.

## Limits and safety

- Groq is a language model, not the timetable database or CP-SAT validator. It cannot know the accurate faculty/room/slot assignment without the application's verified Python evidence.
- The test environment has no live Groq key, Streamlit or OR-Tools; tests use mocked Groq replies. Run an end-to-end check on your own machine.
- This patch is based on `Timora_AI_Assistant_Complete_Updated.zip` from the previous conversation and preserves unrelated CP-SAT, UCB1, dashboard and storage code.
