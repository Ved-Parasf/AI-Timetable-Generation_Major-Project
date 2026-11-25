# Timora — Analytics Dashboard and AI Assistant Button Update

This update is based on `AI_Timetable_Timora_V18_20_Adaptive_Fairness_Complete(4).zip`.

## What changed

1. The floating chat button now reads **Open Timora AI Assistant** (same chat workflow and button key).
2. Results has a new **Analytics dashboard** tab, next to Timetable explorer.
3. The dashboard uses the solver's **independently validated** `quality_summary.json` data to show:
   - Standard versus Fairness comparisons, with actual change values (when both are available)
   - Teacher gap periods and student-section avoidable gap **proxy**
   - Faculty daily overload, late periods, policy soft-avoid periods and weekday load spread
   - Top faculty by gap count, top student sections by avoidable gaps
   - Faculty weekly teaching periods and aggregate occupied teaching periods per weekday
   - Detailed faculty daily workload and section gap tables
   - Downloadable summary-metrics CSV
4. If only one timetable has been generated, charts show measured values for that timetable; the application **does not invent a Fairness result**.
5. An optional **Compare saved Standard and Fairness versions** expander lets you select archived versions for the same workbook. Click **Load and validate saved comparison** to reconstruct both schedules and independently validate them before comparison. Compare only runs with matching mentoring/lab settings; different search settings or time limits can still make the difference non-causal.

The CP-SAT solver, UCB1 fairness module, university policies, coordinator approval, Timora chat and all existing downloads and data files have been preserved. Analytics are deterministic measurements, not LLM predictions.

## How to use

1. **Back up** the currently working Timora folder (especially the `data/` directory and database).
2. Extract the updated ZIP into a **new folder**, not over your running project.
3. In PowerShell, open the extracted `AI_Timetable_Timora_V18_20_Adaptive_Fairness_Complete` folder.
4. If dependencies aren't installed: `python -m pip install -r requirements.txt`
5. Run: `python -m streamlit run timetable_app.py`
6. Upload/open the university workbook and generate a Standard timetable.
7. In **Results → Analytics dashboard**, review faculty workloads and section gaps.
8. Generate Fairness from the validated Standard using the existing Fairness action, then return to **Analytics dashboard** to see live Standard versus Fairness graphs. Or compare archived validated versions from the dashboard expander.
9. On the Results page, click **Open Timora AI Assistant** to use the existing chatbot.

## Validation notes

- All existing unit tests + new dashboard data-selection tests pass in the build environment.
- Python source code compiles successfully; a read-only dashboard smoke test using synthetic metric reports also passes.
- Full browser validation with Streamlit/OR-Tools must be done on the local laptop. Neither was installed in the packaging environment.
- Timetable/student gaps are a **proxy** based on modeled sections, not per-student attendance, since the workbook lacks individual enrollment IDs.
- This update does not imply that a FEASIBLE solution is mathematically optimal, or that Fairness always improves every metric.
