# Kia PV5: discrepancy between battery depletion and vehicle-reported energy consumption

**Vehicle:** Kia PV5, registration NX26ZTD (right-hand drive, UK; heat pump fitted)
**Study period:** manual charging log from 9 April 2026 (complete SOC records from 25 April) to 13 November 2026; detailed energy comparison 17 August – 13 November 2026
**Report date:** 13 November 2026
**Prepared by:** vehicle owner, from owner-collected data
**Data:** Kia Connect API daily driving statistics, a manual charging log kept since new, and trip-computer readings
**Data availability:** the underlying daily records and charging log can be supplied on request

---

## 1. Executive summary

This report documents a systematic difference between two independent measures of
the energy leaving the vehicle's traction battery:

1. **Battery-side energy**, calculated from the change in state of charge (SOC)
   and the usable battery capacity; and
2. **Vehicle-reported energy**, taken from the vehicle's daily driving statistics
   as reported to the Kia Connect service.

Over the most recent 90 days the battery delivered **610.9 kWh**, while the
vehicle reported **473.9 kWh** — **77.6%** of the battery-side figure, a ratio of
**1.29**. The most recent 30 days give the same result (239.2 vs 186.2 kWh,
77.8%, ratio 1.28). Twelve clean charge-to-charge periods give a median ratio of
**1.33**.

The difference is:

- **not seasonal** — it is the same in August and September as in November, so
  cabin or battery heating is not the driver;
- **not time-based** — a regression over 52 charge-to-charge intervals (7,693 km,
  176 days) finds a distance term of 18.78 kWh/100 km and a time term of
  **−0.07 kWh/day** (R² = 0.96); a time-only model has no explanatory power;
- **not explained by battery capacity or the SOC scale** — the charger-side
  energy validates both to within a few percent.

The vehicle's own trip computer is closer to the battery-side figure but is also
optimistic: it reported 3.3 mi/kWh against a calculated 2.86 mi/kWh on
18 October, and 3.6 against 3.11 on 22 October (about 13% low in energy terms).
The two vehicle meters therefore disagree with each other by roughly 16–17%.

The pattern is consistent with the reported energy being measured at the motor
(or motor/inverter) rather than at the battery terminals, so that drivetrain
losses are not included. Part of the difference may also be un-metered auxiliary
loads. **A factor of approximately 1.26 on the reported daily kWh would be
required for a kWh-based range estimate to agree with the battery-SOC-based
estimate** (depending on the aggregation, the factor lies between 1.26 and 1.33).

Section 7 lists specific questions for Kia, chiefly the measurement point of the
reported energy and the fact that the battery-management energy channel is
reported as zero on every day in the dataset.

---

## 2. Vehicle and data sources

### 2.1 Vehicle

| Item | Detail |
|---|---|
| Model | Kia PV5 |
| Registration | NX26ZTD |
| Odometer at end of study | 9,363 km |
| Climate | UK, maritime; study spans spring, summer and autumn |
| Heating | Heat pump |
| Usage | Private; no cabin pre-heating or idling; no towing reported |
| Charging | Predominantly home AC; a small number of public charges |

Ambient temperature was not logged. Month is used as a proxy in the seasonal
comparison (Section 4.3).

### 2.2 Data sources

1. **Manual charging log (since new).** One row per charge event: date and time,
   odometer (recorded in miles, converted to km), SOC at the start and end of
   charging, charger-side energy in kWh, location and notes. The log contains
   55 charge events with complete SOC records, forming 54 driving periods.
   Charger-side energy is metered by the charge point and therefore includes
   charging losses.

2. **Kia Connect API.** Vehicle state (SOC, odometer, capacity) and the daily
   driving statistics. The relevant fields, using the API's own names, are:

   | API field | Meaning (as documented/labelled) |
   |---|---|
   | `totalPwrCsp` | total energy consumption for the day |
   | `motorPwrCsp` | traction/motor energy |
   | `climatePwrCsp` | climate-control energy |
   | `eDPwrCsp` | onboard electronics energy |
   | `batteryMgPwrCsp` | battery-management energy |
   | `regenPwr` | regenerated energy |
   | `calculativeOdo` | distance driven that day |

   The API retains about 90 days of daily detail; the full 90-day window was
   extracted and checked against the API's 30-day aggregate. The two agree.

3. **Trip-computer readings.** The owner recorded the vehicle's displayed
   efficiency (mi/kWh) on two occasions, alongside the calculated value for the
   same charge-to-charge period.

### 2.3 Units and conventions

- Distances are analysed in km; the trip computer is reported in miles/kWh.
- Energies are in kWh. The API reports Wh; values are divided by 1,000.
- Usable battery capacity is taken as **71.2 kWh**. This is the capacity value
  reported by the vehicle through the API (`ev_battery_capacity` = 256,320,
  equivalent to 71.2 kWh) and matches the published specification. The manual
  log uses 72.0 kWh; the 1.1% difference does not affect any conclusion.
- One mile = 1.609344 km.

---

## 3. Method

### 3.1 Battery-side energy

For any interval, the energy leaving the battery is calculated from the SOC
change:

```
E_battery = (SOC_start − SOC_end) / 100 × C
```

where `C` = 71.2 kWh. This uses only the vehicle's own SOC readings and the
capacity; it is independent of the vehicle's energy meter and of the charger.

### 3.2 Energy balance over a window

Over a window that contains charging events, the correct balance is:

```
E_battery = (SOC_start − SOC_end) / 100 × C  +  Σ (ΔSOC_charge / 100 × C)
```

In words: the battery energy consumed is the net SOC change across the window
plus every measured SOC gain from charging inside it. This is the most robust
estimator in the report: it needs only the SOC at the two ends and the SOC gain
of each charge, not the number of days, the distance, or the charger efficiency.

The reported share is then `E_reported / E_battery`, and the gap per 100 km is
`(E_battery − E_reported) / distance × 100`.

### 3.3 Charge-to-charge periods

For the per-period analysis, consecutive home charges define intervals. A period
is used only when:

- both bounding events are home charges (no public top-up inside the period);
- the odometer and SOC readings are complete and the SOC fell;
- the API's distance for the days in the period is at least 94% of the measured
  odometer distance (a completeness check, catching days missing from the
  vehicle's daily statistics).

This leaves 12 clean periods covering 1,872 km between 23 August and 8 November.
One further period (5–7 September) was excluded because the API distance covered
only 67% of the odometer distance, indicating a missing day.

### 3.4 Validation of capacity and SOC scale

The charging log provides an independent check. If the SOC scale and capacity
are correct, charger-side energy should exceed battery-side energy only by the
charging losses:

```
charger energy / (ΔSOC/100 × 71.2 kWh)
```

Across **55 charge events**, the median ratio is **1.048** (interquartile range
1.034–1.068; mean 1.050; range 0.84–1.12). This implies a charging efficiency of
about **95%**, typical for an AC charge, and it is stable across the whole
period. The 90-day window gives the same result: 619.8 kWh charger-side for
594.5 kWh of SOC gain (95.9%).

A materially smaller true capacity would require an implausibly poor charging
efficiency: a 67.3 kWh capacity would imply 90% efficiency, and 63.6 kWh would
imply 85%. Even at 67.3 kWh, the reported energy would still be only 82% of
battery-side energy (an 18% shortfall), so the conclusion is robust to the
capacity assumption.

---

## 4. Results

### 4.1 Aggregate energy balance

| Window | Calendar days | Driving days | Distance (km) | Battery energy (kWh) | Reported energy (kWh) | Reported ÷ battery | Gap (kWh/100 km) |
|---|---:|---:|---:|---:|---:|---:|---:|
| 14 Oct – 13 Nov 2026 | 31 | 26 | 1,061 | 239.2 | 186.2 | 77.8% | 5.0 |
| 17 Aug – 13 Nov 2026 | 89 | 73 | 3,024 | 610.9 | 473.9 | 77.6% | 4.5 |

The API's distance is 98.8% of the measured odometer distance over the 90-day
window (3,024 vs 3,061 km; the difference is consistent with the API's whole-km
rounding), so the distance basis is complete.

The API's 30-day aggregate and the sum of its daily entries over the same
window agree to within 0.1% (186.2 kWh over 1,061 km, i.e. 175.5 Wh/km), and the
90-day aggregate (473.9 kWh, 3,024 km) equals the sum of its 73 daily entries
exactly. The discrepancy is therefore present in the per-day values themselves,
not introduced by an aggregation step.

In efficiency terms, the vehicle reports 17.6 kWh/100 km over the 30-day window;
the battery-side figure is 22.5 kWh/100 km. The resulting gap of about
5.0 kWh/100 km is the energy the vehicle's statistics do not account for.

### 4.2 Charge-to-charge periods

The 12 clean periods are summarised below; the full table is in Appendix A.

| Metric | Value |
|---|---|
| Periods | 12 |
| Distance | 1,872 km |
| Battery energy | 362.4 kWh |
| Reported energy | 273.5 kWh |
| Median per-period ratio | 1.329 |
| Pooled ratio | 1.325 |
| Pooled gap | 88.9 kWh (4.8 kWh/100 km) |
| Per-period gap range | 4.0 – 6.0 kWh/100 km |

The ratio is tightly clustered: all twelve periods lie between 1.298 and 1.403,
and the gap per 100 km is between 4.0 and 6.0 in every period.

### 4.3 Seasonal dependence

| Month | SOC ÷ reported energy | Gap (kWh/100 km) |
|---|---|---:|
| August (from 17th) | 1.298 – 1.344 | 4.0 – 4.2 |
| September | 1.310 – 1.403 | 4.5 – 4.9 |
| October | 1.302 – 1.340 | 4.7 – 5.5 |
| November (to 13th) | 1.319 – 1.345 | 4.9 – 6.0 |

September — mild, with little or no cabin heating — is not better than November.
The source of the battery energy is therefore not thermal: a heat-pump cabin
load or uncommanded battery heating would produce a clear summer/winter
difference. The `climatePwrCsp` channel accounts for only 5.6–9.4% of the
reported total, and `batteryMgPwrCsp` is zero on every day (Section 4.5), so the
API cannot be under-reporting a large thermal load through those channels alone.

### 4.4 Distance rather than time

Fifty-two charge-to-charge intervals across the whole log (7,693 km, 1,438 kWh,
176 days of elapsed time in total) were fitted with an ordinary least-squares
model:

| Model | Fit | R² |
|---|---|---:|
| Energy = a × distance + b × time | a = 18.78 kWh/100 km; b = **−0.07 kWh/day** | 0.960 |
| Energy = a × distance | a = 18.64 kWh/100 km | 0.960 |
| Energy = b × time | b = 6.74 kWh/day | −0.785 |

Distance alone explains 96% of the variance. The fitted parked/standby rate is
indistinguishable from zero (and slightly negative), so there is no measurable
time-based drain: no parked consumption, no standby load, no soak heating.
Adding the time term does not improve the fit. The residual standard deviation
is 2.59 kWh per interval, reflecting trip-to-trip variation in consumption
(motorway vs town, load, temperature).

This also argues against a regen-accounting explanation: across the 12 clean
periods, regenerated energy ranged from 35% to 66% of the reported total, yet
the gap per 100 km stayed within 4.0–6.0. An error proportional to recovered
energy would vary with that share.

### 4.5 Composition of the reported energy

| Component | 30-day kWh | share | 90-day kWh | share |
|---|---:|---:|---:|---:|
| Traction (`motorPwrCsp`) | 157.1 | 84.4% | 414.2 | 87.4% |
| Climate (`climatePwrCsp`) | 17.4 | 9.4% | 26.6 | 5.6% |
| Electronics (`eDPwrCsp`) | 11.7 | 6.3% | 33.1 | 7.0% |
| Battery management (`batteryMgPwrCsp`) | **0.0** | 0.0% | **0.0** | 0.0% |
| **Total (`totalPwrCsp`)** | **186.2** | 100% | **473.9** | 100% |
| Regenerated (`regenPwr`, separate) | 67.2 | — | 233.1 | — |

The four components sum exactly to the total on every day. The battery-management
channel is zero throughout, including on cold days and during charging, so any
battery heating or thermal conditioning is either absent from this channel or
folded into another one. That makes the reported total an incomplete record of
battery-terminal energy even before the drivetrain question is considered.

### 4.6 Comparison with the trip computer

| Date | Period | Distance | ΔSOC | Battery energy (72 kWh basis) | Calculated | Trip computer | Trip computer ÷ calculated |
|---|---|---:|---:|---:|---:|---:|---:|
| 18 Oct 2026 | 16 Oct 07:30 → 18 Oct 23:30 | 169.0 km (105.0 mi) | 51% | 36.7 kWh | 2.86 mi/kWh | **3.3 mi/kWh** | 1.154 |
| 22 Oct 2026 | 19 Oct 07:30 → 22 Oct 23:30 | 75.7 km (47.0 mi) | 21% | 15.1 kWh | 3.11 mi/kWh | **3.6 mi/kWh** | 1.158 |

In energy terms the trip computer reports 31.8 kWh where the battery delivered
36.7 kWh (18 October) and 13.1 kWh where it delivered 15.1 kWh (22 October) —
about 13% low (both quantities on the log's 72 kWh basis). The trip computer is
therefore closer to the battery than the daily statistics, but the two vehicle
meters still disagree with each other by roughly 16–17% on the same journeys.

### 4.7 Reconciliation factor

Multiplying the reported daily kWh by **1.26** brings the kWh-based range
estimate into agreement with the battery-SOC-based estimate (same capacity and
28-day rolling window). For example, over the most recent 28 days the two
estimates then read 198 and 201 miles respectively; without the factor the
kWh-based estimate reads about 240 miles against 201.

The factor is not a single exact number: depending on how the periods are
aggregated it ranges from 1.26 (day-level accounting, 69 qualifying days) to
1.28–1.29 (the window balances above) to 1.33 (the median charge-to-charge
period). A value of about 1.26–1.29 is appropriate for correcting day-level
figures.

---

## 5. Interpretation

The observations are consistent with the following explanation, which the data
support but cannot prove without information from Kia:

1. **The reported energy is not battery-terminal energy.** The traction channel
   is named `motorPwrCsp` ("motor power consumption"), and the shortfall is
   proportional to distance/traction work rather than to time, heating or
   regeneration. If the motor channel is measured at the motor or inverter
   output rather than at the battery terminals, inverter, motor and reduction-gear
   losses would be missing from the total. On this dataset the implied
   battery-to-motor efficiency would be about 75%, which is lower than typical
   for an EV drivetrain; some of the shortfall may therefore also be un-metered
   auxiliary or thermal load, or a metering calibration offset.
2. **The trip computer and the daily statistics use different measurement
   points.** They differ by about 16–17% on the same journeys. The trip computer is
   closer to battery-side energy but still about 13% low.
3. **Neither the capacity nor the SOC scale is the cause.** The charger-side
   validation constrains the capacity to roughly 67–71 kWh for plausible AC
   charging efficiencies, and even the lowest value leaves an 18% shortfall.

The practical consequence is that a range estimate based on the vehicle's
reported kWh overstates the vehicle's real-world range by roughly 20–25%,
whereas a battery-SOC-based estimate agrees with the manual charging log and
with the vehicle's observed range. The discrepancy is large enough to matter for
route planning and for customers comparing the vehicle's displayed efficiency
with the energy they pay for at the charger.

---

## 6. Data quality and limitations

- **SOC resolution.** The vehicle reports integer percentages, so each reading
  carries ±0.5% (±0.36 kWh). On short periods this is the dominant noise term;
  it cannot produce a systematic 25% bias over 90 days.
- **Charge timing.** Home charges typically start in the evening and finish the
  following morning (occasionally around midday). Period boundaries are therefore
  taken at the charge start/end times recorded in the log, not at calendar-day
  boundaries. Charge-to-charge periods avoid the boundary problem entirely.
- **Window boundaries.** The 90-day balance starts at the end of the 17 August
  charge (12:00), whereas the API's daily entry covers the whole calendar day
  (8.6 kWh on 17 August). The resulting uncertainty is at most 1.4% of the
  window's battery energy and does not affect the charge-to-charge result.
- **API distance.** `calculativeOdo` is a whole number of km, which understates
  distance by up to 0.5 km/day. Over the 90-day window the API distance is 98.8%
  of the odometer distance. Energy comparisons do not depend on this.
- **Public charging.** A small number of public charges are included in the log;
  their metered energy is charger-side, like the home charges. Periods containing
  a public top-up are excluded from the charge-to-charge analysis.
- **Temperature.** Not logged. Month is used as a proxy; the conclusion rests on
  the absence of a seasonal difference, not on a temperature model.
- **Vehicle software.** The software/BMS version was not recorded and may have
  changed during the study.
- **Data provenance.** All data are owner-collected, not laboratory measurements.
  The charger-side energy comes from the charge point's own metering. A
  controlled test (Section 7) would remove the remaining uncertainty.

---

## 7. Questions for Kia

1. **Measurement point.** At what point is `motorPwrCsp` measured — battery
   terminals, inverter DC input, motor electrical input, or motor mechanical
   output? The same question applies to `totalPwrCsp`. This determines whether
   the ~23% difference is expected by design.
2. **Battery-management channel.** `batteryMgPwrCsp` is 0 on every day in this
   dataset. Is battery thermal management supported on this vehicle/software
   version, and if so, which channel carries its energy?
3. **Definition of displayed consumption.** Is the vehicle's displayed
   kWh/100 km (and the trip computer's mi/kWh) intended to represent
   battery-terminal energy, wall energy, or motor energy? Should customers
   expect it to agree with metered AC energy minus charging losses?
4. **Expected magnitude.** Is a ~1.26–1.33 ratio between SOC-derived battery
   energy and reported energy within the expected range for a PV5? If so, what
   is the documented drivetrain/auxiliary loss budget?
5. **Controlled verification.** Would Kia be willing to compare, on a test
   vehicle, metered AC energy, battery SOC change, the daily statistics and the
   trip computer over a fixed route at a known ambient temperature? That would
   separate drivetrain losses from metering offsets and from any un-metered
   thermal load.
6. **Trip computer vs daily statistics.** Why do the two displays differ by
   about 16–17% on the same journeys, and which one should a customer treat as
   authoritative?

---

## Appendix A — Charge-to-charge periods used

All periods are bounded by home charges, have complete SOC and odometer
readings, and have API distance covering at least 94% of the odometer distance.
"Battery" is ΔSOC/100 × 71.2 kWh; "Reported" is the sum of the API's daily
`totalPwrCsp` for the days in the period.

| Period | Days | Distance (km) | Battery (kWh) | Reported (kWh) | Ratio | Gap (kWh/100 km) | Regen (kWh) | Climate (kWh) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 23–27 Aug | 4.7 | 108 | 17.8 | 13.2 | 1.344 | 4.2 | 6.8 | 0.3 |
| 28–31 Aug | 3.7 | 198 | 34.9 | 26.9 | 1.298 | 4.0 | 17.7 | 0.8 |
| 8–11 Sep | 3.7 | 55 | 9.3 | 6.6 | 1.403 | 4.9 | 4.4 | 0.1 |
| 12–17 Sep | 5.7 | 161 | 29.9 | 22.6 | 1.321 | 4.5 | 11.3 | 0.9 |
| 18–20 Sep | 2.7 | 238 | 44.9 | 34.2 | 1.310 | 4.5 | 20.6 | 0.8 |
| 28 Sep – 1 Oct | 3.7 | 143 | 27.1 | 20.3 | 1.333 | 4.7 | 10.9 | 0.7 |
| 2–9 Oct | 7.7 | 296 | 56.2 | 42.3 | 1.330 | 4.7 | 26.4 | 1.8 |
| 10–15 Oct | 5.7 | 135 | 27.8 | 21.3 | 1.302 | 4.8 | 9.7 | 0.5 |
| 16–18 Oct | 2.7 | 169 | 36.3 | 27.1 | 1.340 | 5.5 | 13.7 | 2.2 |
| 19–22 Oct | 3.7 | 76 | 15.0 | 11.3 | 1.329 | 4.9 | 4.7 | 1.6 |
| 31 Oct – 5 Nov | 5.7 | 121 | 28.5 | 21.2 | 1.345 | 6.0 | 7.4 | 3.2 |
| 6–8 Nov | 2.7 | 172 | 34.9 | 26.5 | 1.319 | 4.9 | 13.4 | 2.2 |
| **Total / pooled** | **52.0** | **1,872** | **362.4** | **273.5** | **1.325** | **4.8** | — | — |

Excluded: 5–7 September (API distance covered only 67% of the odometer distance,
indicating a day missing from the vehicle's statistics) and periods containing a
public charge.

## Appendix B — Worked example (18 October 2026)

- Previous home charge ended 16 October at 07:30, SOC 80%; next home charge
  started 18 October at 23:30, SOC 29%.
- Odometer: 8,273.6 → 8,442.6 km, i.e. 169.0 km (105.0 miles).
- ΔSOC = 51%.
- Battery energy = 51/100 × 71.2 = 36.3 kWh (36.7 kWh on the log's 72 kWh basis).
- Calculated efficiency = 105.0 / 36.7 = 2.86 mi/kWh.
- Trip computer: 3.3 mi/kWh → implied energy 105.0 / 3.3 = 31.8 kWh (87% of
  battery energy).
- API daily statistics, 16 and 18 October: 13.23 + 13.87 = 27.1 kWh (75% of
  battery energy; ratio 1.34).
- All three measurements therefore differ on the same journey: 36.3 kWh
  (battery), 31.8 kWh (trip computer), 27.1 kWh (daily statistics).

## Appendix C — Regression detail

Ordinary least squares over 52 charge-to-charge intervals (any interval with
complete SOC/odometer readings, a positive SOC drop and a positive duration):

```
n = 52          total distance = 7,693 km
total energy    = 1,438 kWh (SOC-derived)
total time      = 176 days of elapsed time
```

Two-term fit: `E (kWh) = 0.1878 × distance (km) − 0.073 × time (days)`;
R² = 0.960; residual standard deviation = 2.59 kWh.

Distance-only: `E = 0.1864 × distance`; R² = 0.960.

Time-only: `E = 6.74 × time`; R² = −0.785 (worse than the mean).

Interpretation: all measurable battery consumption is associated with distance
driven. There is no evidence of a parked or standby drain, and no evidence of
soak heating.
