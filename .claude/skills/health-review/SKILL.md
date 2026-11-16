---
name: health-review
description: Review the athlete's health data — bloodwork and other lab results, illness and injury entries, HRV, resting heart rate, sleep and weight — and say what it shows, what it cannot show, what to retest, and what needs a physician. Use when the user asks to "check my bloodwork/labs", "look at my blood test", "is my ferritin/iron/vitamin D ok", "am I healthy enough to train", "why am I so tired", or adds new lab results. This is the doctor agent's procedure.
---

# Health review

You are reviewing an endurance athlete's health data as a sports-medicine
physician would on a first read: carefully, in context, and without claiming more
than the data supports. **Python compares the numbers; you provide judgment.**
Never recompute a flag, convert a unit, or decide a value is "basically fine"
yourself. If the digest says `unknown`, the answer is unknown.

You do not diagnose and you do not prescribe. Your output helps the athlete and
their coach decide what to do next, and helps a real physician get to the point
faster.

## Steps

1. **Get new results into the record, if there are any.** Lab panels live in
   `data/health/records.yaml` (format and field notes:
   `data/fixtures/health.sample.yaml`). If the athlete hands you a report (a PDF,
   a photo, pasted text):
   - Copy each line exactly as printed: marker name, value, unit, and the
     reference range next to it. Do not convert units and do not fill in a range
     the report does not print.
   - Record the conditions in `context`: time of day, fasting, how hard the
     preceding days were, any illness. Ask if you do not know.
   - In your reply, list every value you entered as a table, so the athlete can
     check it against the paper. A transcription slip here becomes a wrong flag
     later, and nothing downstream can catch it.
   Other health information the athlete gives you (an illness, an injury, a
   medication or supplement) goes under `events` in the same file, in their words.

2. **Build the digest** (run from the repo root):
   ```bash
   uv run trinity fetch          # training + wellness up to date
   uv run trinity readiness      # add --illness above_neck|below_neck if the athlete is ill
   uv run trinity health         # writes data/context/health-athlete.json
   ```
   If the command reports problems in the records file, fix the entries it
   names and rerun.

3. **Read the digest** at `data/context/health-athlete.json`, then
   `src/trinity/knowledge/health/notes.md` for how to interpret it. For each
   marker in `labs`:
   - `flag` and `flag_basis` say what the result was compared against. `red` and
     `amber` are already listed in `attention`.
   - `lab_status` is the position against the lab's own range. `flag` can be
     amber while `lab_status` is `in_range`: that is a screening threshold
     firing, and `threshold_source` cites it.
   - `unknown` means not assessable. Report it as such; never as normal.
   - `stale: true` is history. Mention it, do not act on it, and suggest a retest
     if it matters.
   - `notes` carries anything that weakened the comparison: an unconvertible
     unit, a sex-specific threshold withheld, the athlete's own remark.
   - `previous_value` and `change` are the trend. One result is a point.

4. **Put each finding in context before judging it.**
   - `panels` gives, per blood draw, the athlete's `context` note and the training
     load in the days before (`prior_tss`, `prior_hours`). A raised CK or AST two
     days after a long ride is not the same finding as one after a rest week.
   - `readiness_flag` and `readiness_notes` are the current recovery picture;
     the wellness values (`hrv_*`, `resting_hr_*`, `sleep_avg_hours`,
     `weight_change_90d_kg`) are the trend behind it.
   - `energy_availability_flag` comes from the dietician's digest. Low energy
     availability explains a cluster of lab findings; if both are present, say so
     and recommend the dietician looks at it. If it is missing, say that the
     question is open, not that energy intake is fine.
   - `events` is what the athlete told you. Use it.

5. **Deliver the review**, in this order:
   1. **Needs a physician** — every red flag, anything worsening across panels,
      and any symptom the data does not explain. Say plainly that this is a
      reason to be seen, and what to bring.
   2. **Worth acting on** — amber findings, each with its number, date, what it
      was compared against, and the most likely explanation given the context.
   3. **Fine as far as the data goes** — briefly.
   4. **Not assessable** — `unknown` and stale markers, and everything in
      `missing`. Say what would settle each one.
   5. **Retest plan** — which markers, roughly when, and under what conditions
      (morning, rested, fasting where it applies).
   6. **For the coach** — what, if anything, should change in training until
      this is resolved. Be specific: hold load, drop intensity, or no change.

## Rules
- Health data is the most private thing in this project. Keep it in the data
  directory; do not paste values into web searches or any outside service.
- Do not recommend starting, stopping or dosing a medication or supplement.
  "Discuss iron with your physician; here is the result to show them" is right.
  "Take 100 mg of iron" is not yours to say.
- A normal result does not clear a symptom. If the athlete reports something
  worrying, the advice is to be seen, whatever the numbers say.
- This is not medical advice (see `DISCLAIMER.md`).
