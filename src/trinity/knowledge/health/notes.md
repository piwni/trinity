# Health notes for the doctor agent

How to read an endurance athlete's lab results and wellness data. The numbers
and flags come from Python (`trinity health`, thresholds in
[`rules.yaml`](rules.yaml)); this file is the context for judging them.

**This is decision support, not medical advice.** The doctor agent does not
diagnose and does not prescribe. Its job is to say what the data shows, what it
cannot show, what to retest, and when a physician needs to see it.

## How results are flagged

Every result is compared with the reference range printed on the lab report.
Ranges differ between labs and methods, so the lab's own range is the default
yardstick and "outside it" is reported as amber without grading how far.

A few markers also carry a screening threshold, because an athlete can sit
inside a general-population range and still be short:

| Marker | Amber | Red | Source |
| --- | --- | --- | --- |
| Ferritin | < 35 µg/L | < 20 µg/L | Sim et al. 2019 |
| Transferrin saturation | < 16 % | | Sim et al. 2019 |
| Haemoglobin | | < 130 g/L (men), < 120 g/L (women) | WHO 2011 |
| 25-OH vitamin D | < 50 nmol/L | < 30 nmol/L | IOM 2011; Owens et al. 2018 |

A flag of `unknown` means there was nothing to compare against: no range on the
report and no threshold here. It is not a pass.

## Read the conditions before the number

A blood test is a snapshot, and training moves several markers. The digest
reports the training load in the days before each panel for this reason.

- **Standardise the draw.** Iron studies are best taken in the morning, rested,
  hydrated, with no muscle-damaging exercise beforehand and no current illness
  (Sim et al. 2019). A panel taken the morning after a race is a different test.
- **Ferritin is also an acute-phase protein.** Inflammation, infection and hard
  exercise raise it, so a normal ferritin alongside a raised CRP can hide a
  deficiency. Read the two together, and prefer a retest over a conclusion.
- **Plasma volume expands with endurance training.** That dilutes haemoglobin
  and haematocrit, so a slightly low concentration in a well-trained athlete can
  be dilution, not a loss of red cell mass. It still needs a physician if it is
  below the anaemia threshold or falling.
- **Hard or eccentric exercise raises CK, and with it AST and often ALT**, for
  days. Raised "liver" enzymes after a heavy block usually trace to muscle; a
  rested retest tells the two apart.
- **Creatinine tracks muscle mass and recent meat or creatine intake**, so
  creatinine-based eGFR can read low in a muscular athlete.

## Patterns worth naming

- **Iron.** Stage it from ferritin, haemoglobin and transferrin saturation
  together (Sim et al. 2019): low ferritin alone is depleted stores; low
  ferritin with low transferrin saturation is deficiency without anaemia; add
  low haemoglobin and it is iron-deficiency anaemia. Any of these goes to a
  physician before supplementing. Iron is not harmless, and the cause matters.
- **Low energy availability.** Persistently low energy availability can show up
  as low testosterone, low T3, disturbed lipids, poor iron status, falling
  resting heart rate with poor recovery, and recurring illness or bone injury
  (IOC consensus on REDs: Mountjoy et al. 2023). The dietician's digest carries
  the energy availability estimate. Put the two side by side; neither is proof
  alone.
- **Overreaching and illness.** The readiness module already tracks HRV against
  its baseline, resting heart rate, sleep debt and load spikes. Sustained HRV
  suppression with a raised resting heart rate, alongside a raised CRP or white
  cell count, is a reason to back off and get seen, not to push through.

## Trends beat single values

One result is a point; the digest gives the previous result and the change
where it has them. A ferritin of 40 µg/L that was 90 a year ago is a different
finding from one that has been 40 for three years. Say which it is. Results
older than a year are marked stale and should be treated as history.

## When to send the athlete to a physician

Always, for any red flag. Also for: a result well outside the lab's range,
anything that is getting worse across panels, symptoms the data does not explain
(chest pain, fainting, palpitations, breathlessness out of proportion, unexplained
weight loss, blood in urine or stool), and before starting iron, vitamin D in
high doses, or any other supplement meant to correct a lab value.

## Sources

- Sim M, Garvican-Lewis LA, Cox GR, et al. Iron considerations for the athlete:
  a narrative review. *Eur J Appl Physiol* 2019;119:1463-1478.
- Peeling P, Sim M, Badenhorst CE, et al. Iron status and the acute
  post-exercise hepcidin response in athletes. *PLoS One* 2014;9:e93002.
- World Health Organization. Haemoglobin concentrations for the diagnosis of
  anaemia and assessment of severity. 2011 (WHO/NMH/NHD/MNM/11.1).
- Institute of Medicine. *Dietary Reference Intakes for Calcium and Vitamin D*.
  National Academies Press, 2011.
- Owens DJ, Allison R, Close GL. Vitamin D and the athlete: current perspectives
  and new challenges. *Sports Med* 2018;48(Suppl 1):3-16.
- Mountjoy M, Ackerman KE, Bailey DM, et al. 2023 International Olympic
  Committee's (IOC) consensus statement on Relative Energy Deficiency in Sport
  (REDs). *Br J Sports Med* 2023;57:1073-1097.
