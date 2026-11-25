# Timetable Assistant — Verified Groq Conflict Explanations (V18.4)

This release retains the working timetable generator, Excel policies, validator, fairness module, assistant, Groq connectivity, SQLite version history, and coordinator-approved rescheduling. It specifically addresses unsupported claims and malformed AI explanations seen during **Check Conflicts**.

## Correct behavior

When the validator reports:

`BCA-III-WDA: insufficient lunch periods on MON (policy window (4, 5, 6))`

The assistant explains that the **proposed placement would leave too few free lunch periods**. It does **not** say that all classes are banned in Periods 4–6. The required number of free periods comes from the uploaded workbook's `Institution_Policies` sheet; for the bundled NorthCap workbook this is **one** free period among Periods 4, 5, and 6.

Teacher, student-group, and room conflicts appear only if the independent rescheduler actually reports them for the **requested slot**. Obstacles found while searching **other** slots are never presented as conflicts at the requested slot.

The full verified alternatives count remains visible, and no explanation performs a class move.

## How Groq is used safely

1. **Check Conflicts** runs the existing independent move validator.
2. The Groq model may select the *index* of one reported reason to put first (strict structured JSON); it cannot return an explanation or extra facts to be displayed.
3. Python displays **all** the verified reasons, explains the lunch rule from the workbook, and states the verified alternatives count.
4. If Groq is unavailable or its JSON is invalid, the exact verified explanation is still available locally.
5. The UI states whether Groq participated. It never labels a local fallback as a Groq-generated explanation.

The generic free-text cloud analyzer does **not** handle class-move and conflict-analysis questions, avoiding a second speculative explanation. The general cloud analyzer also rejects malformed `canvascanvas` or visibly broken Markdown output. Fairness analysis remains a separate optional AI feature; it is not proof of scheduling feasibility.

## Install in Windows VS Code

**Preferred:** stop Streamlit, back up your project (especially `data\coordinator_workflows.sqlite3`), extract the full project ZIP, and open the inner `Timetable_Assistant_Groq_Cloud` folder containing `timetable_app.py`. Run:

```powershell
py -m pip install -r requirements.txt
py -m unittest discover -s tests -v
py -m streamlit run timetable_app.py
```

**If you prefer to keep the existing project:** the small patch ZIP contains the updated files `timetable_cloud_ai.py`, `timetable_assistant_features.py`, and `timetable_ai_upgrade.py`, plus new `timetable_conflict_explanations.py`. Copy the four Python files next to your existing `timetable_app.py` (after backing up those files). Restart Streamlit.

## Test the reported issue

With a selected, independently validated timetable:

1. Open **Timetable Assistant → Check Conflicts**.
2. Select the exact class and proposed Monday slot.
3. Click **Check conflicts**, followed by **Explain verified conflict reasons**.
4. Confirm the lunch rule is described as a *free-period requirement*, not as a complete prohibition of Periods 4–6.
5. Confirm **no unrelated teachers/courses** are named unless they appear in `requested_reasons`.
6. The suggested alternatives are previews. Use **Shift a Class** and approve an independently validated alternative to publish a change.

## Test limits

The included Python tests use mocked Groq responses and a small scheduling fixture, including the reported lunch diagnostic. Groq itself, the full 618-session solve, and visual behavior of Streamlit must be verified on the user's Windows machine. Never send API keys with bug reports.
